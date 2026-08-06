# -*- coding: utf-8 -*-
"""
sell.py —— 持仓卖出信号（配套用户 2026-08-03 指令 + 选股买卖点SOP.md v1）

卖点规则（用户原话）：
  1. 高位十字星  → 当日或次日清仓
  2. 长下影      → 当日或次日清仓
  3. 主力盈利 30% → 减仓
  4. 止损清仓（2026-08-03 补）：分两种模式
       购入价 > 买入日5日线        → ma5 模式：收盘价跌破5日线即清仓
       购入价 <= 买入日5日线       → cost8 模式（横盘期）：严格成本-8%硬止损；
                                     当 [今日MA5>昨日MA5 且 前两日收盘价连续上行
                                     且 今日收盘上穿MA5] 触发时，切换为 ma5 模式
                                     （此后同前者：收盘跌破5日线清仓）
  5. 仓位管理 334：加仓/减仓提醒（仅对标记持仓生效，见 设计_仓位管理_334减仓加仓提醒.md §三/§四）
     加仓条件①→加至6成 / 加仓条件②→满10成 / 减仓带比例（浮盈30%减1/4、破MA20减1/3、乖离过大减1/3~1/2）。
     对外披露遵循「对外模糊/对内严控」铁律（见本文件 DISTRIBUTION_MODE 与 _ext）。
     模式状态持久化在 positions.json 的 stop_mode 字段（首次由购入价 vs 买入日MA5 推导）。

数据来源：positions.json（用户自维护的持仓，含成本价；前端做界面前先手填）。
  结构：[{"secid":"1.601939","name":"建设银行","entry_price":7.50,"entry_date":"2026-08-01","shares":1000}, ...]
  空文件 "[]" 即代表无持仓 → 卖出信号返回空列表。

约束（设计_盘中预警带价挂单工作流.md §二）：程序绝不自动下单/撤单，只给"清仓/减仓"建议，
  由用户在同花顺/券商 APP 手动执行。本模块仅做信号判定 + 文案，不碰交易。

性能：持仓数量少（个人持仓通常 <50），但仍走后台预计算缓存，避免与 picks 扫描抢 TDX 锁。
免责：所有产出仅供研究参考，不构成投资建议（SOP §10.10）。
"""
from __future__ import annotations
import os
import time
import json
import threading

import pandas as pd
import numpy as np

import paths
import server
# 注意：对 picks 的导入延迟到函数内（见 _eval_one）。原因：server.py 以 __main__ 方式运行时，
# picks 内部 `import server` 会触发第二份 server 模块拷贝；若 sell 在顶层 `from picks import`，
# 会在 picks 半初始化时卡住（_rows_to_df 尚未定义）。函数内导入时模块已缓存，无性能损耗。

# ===== 参数 =====
DOJI_BODY_RATIO = 0.10      # 十字星：实体/振幅 < 10% 算十字
HIGH_POS_RSI = 60           # 高位判定①：日线 RSI > 60
HIGH_POS_RANGE = 0.85       # 高位判定②：收盘价位于近 20 日区间上 85%
SHADOW_BODY_MULT = 2.0      # 长下影：下影 >= 2×实体
SHADOW_RANGE_RATIO = 0.50   # 长下影：下影 >= 50% 振幅
PROFIT_REDUCE = 0.30        # 主力盈利 30% → 减仓

# ===== 仓位管理 334（设计_仓位管理_334减仓加仓提醒.md）对外模糊/对内严控 =====
# 对内：严格 3→3→4 数值；对外（分发版）：只给 轻/小/中/重 含糊词，绝不泄露具体比例。
# 打包时置 DISTRIBUTION_MODE = True（或环境变量 APANEL_DIST=1），使所有比例输出含糊词。
DISTRIBUTION_MODE = os.environ.get("APANEL_DIST", "0") == "1"
_RATIO_EXTERNAL = {
    "加至6成": "中仓位",
    "满10成": "重仓位",
    "减1/4": "轻减",
    "减1/3": "中减",
    "减1/3~1/2": "中减",
    "清仓": "离场",
}
def _ext(ratio_label):
    """对外模式时把内部数值比例替换为含糊词。"""
    return _RATIO_EXTERNAL.get(ratio_label, ratio_label) if DISTRIBUTION_MODE else ratio_label

POSITIONS_PATH = paths.data_path("positions.json")

# ===== 全局缓存（后台预计算结果）=====
_SELL_CACHE = {"ts": 0, "data": None, "computing": False, "last_err": None}
_SELL_LOCK = threading.Lock()


def load_positions():
    """读 positions.json；缺失/格式错 → 返回空列表。"""
    try:
        with open(POSITIONS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def save_positions(arr):
    """覆盖式写 positions.json。任一字段留空(空串/None)都存为 null，前端可不填成本。"""
    try:
        with open(POSITIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(arr if isinstance(arr, list) else [], f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("[sell] save positions error:", e)
        return False


def _stop_loss(entry, ma5_at_entry, close, ma5_today, ma5_yest, close_y, close_2, prev_mode):
    """止损清仓分模式判定（用户 2026-08-03 规则），返回 (clear:bool, reason:str, new_mode:str)。

      - 购入价 > 买入日 MA5        → ma5 模式：收盘价跌破5日线即清仓。
      - 购入价 <= 买入日 MA5       → cost8 模式（横盘期）：严格成本-8%硬止损；
                                    当 [今日MA5>昨日MA5 且 前两日收盘价连续上行
                                    且 今日收盘上穿MA5] 触发时，切换为 ma5 模式
                                    （此后同前者：收盘跌破5日线清仓）。
    """
    STOP = -0.08  # 横盘期成本硬止损比例（-8%）
    above = (entry > 0 and ma5_at_entry is not None and entry > ma5_at_entry)
    mode = prev_mode if prev_mode in ("ma5", "cost8") else ("ma5" if above else "cost8")
    clear = False
    reason = ""
    if mode == "cost8":
        # 横盘期严格硬止损：成本损失 -8%
        if entry > 0 and close <= entry * (1 + STOP):
            clear = True
            loss = (close - entry) / entry * 100
            reason = f"止损清仓：浮亏 {loss:.1f}% 触及成本-8%硬止损（购入价低于5日线·横盘期）"
        # 转强切换：MA5 拐头向上 + 前两日连涨 + 今日收盘上穿 MA5
        if (ma5_today is not None and ma5_yest is not None
                and close_y is not None and close_2 is not None):
            if (ma5_today > ma5_yest) and (close_2 < close_y) \
                    and (close > ma5_today) and (close_y <= ma5_yest):
                mode = "ma5"
    if mode == "ma5":
        if ma5_today is not None and close < ma5_today:
            clear = True
            loss = (close - entry) / entry * 100 if entry > 0 else None
            tag = "（购入价高于5日线）" if above else "（横盘转强后切换）"
            reason = (f"止损清仓：收盘价 {close:.2f} 跌破5日线 {ma5_today:.2f}"
                      + (f"（浮亏 {loss:.1f}%）" if loss is not None else "") + tag)
    return clear, reason, mode


def _eval_one(pos):
    """对单只持仓做卖点判定。返回含 reasons/action 的 dict。"""
    from picks import _rows_to_df, daily_signals   # 延迟导入，避开顶层循环依赖
    secid = pos.get("secid")
    name = pos.get("name")
    entry = float(pos.get("entry_price") or 0)
    try:
        with server._TDX_LOCK:
            rows = server._klines(secid, 101, 60)
    except Exception as e:
        return {"secid": secid, "name": name, "err": f"行情拉取失败: {e}"}
    df = _rows_to_df(rows)
    if df is None or len(df) < 30:
        return {"secid": secid, "name": name, "err": "日K数据不足(<30根)"}

    df = daily_signals(df)
    df["ma5"] = df["close"].rolling(5).mean()   # 卖点止损用 5 日线
    last = df.iloc[-1]
    close = float(last["close"])
    o = float(last["open"])
    h = float(last["high"])
    l = float(last["low"])
    rng = (h - l) if (h - l) > 0 else 1e-9
    body = abs(close - o)
    lower_shadow = min(o, close) - l
    d_rsi = float(last["d_rsi"]) if pd.notna(last["d_rsi"]) else 0.0

    # 高位判定：RSI 偏高 或 收在近 20 日区间上沿
    hi20 = df["high"].iloc[-20:].max()
    lo20 = df["low"].iloc[-20:].min()
    pos_range = (close - lo20) / (hi20 - lo20) if (hi20 - lo20) > 0 else 0.5
    high_pos = (d_rsi > HIGH_POS_RSI) or (pos_range > HIGH_POS_RANGE)

    # ① 高位十字星
    doji = (body / rng) < DOJI_BODY_RATIO
    doji_exit = doji and high_pos

    # ② 长下影
    shadow_exit = (lower_shadow >= SHADOW_BODY_MULT * body) and \
                  (lower_shadow >= SHADOW_RANGE_RATIO * rng)

    # ③ 主力盈利 30% 减仓
    pnl = (close - entry) / entry if entry > 0 else None
    profit_reduce = (pnl is not None and pnl >= PROFIT_REDUCE)

    # ④ 止损清仓（分模式：ma5 浮动 / cost8 横盘硬止损，规则见 _stop_loss）
    closes = df["close"].astype(float).values
    ma5s = df["ma5"].astype(float).values
    ma5_today = float(ma5s[-1]) if pd.notna(ma5s[-1]) else None
    ma5_yest = float(ma5s[-2]) if (len(ma5s) >= 2 and pd.notna(ma5s[-2])) else None
    close_y = float(closes[-2]) if len(closes) >= 2 else None
    close_2 = float(closes[-3]) if len(closes) >= 3 else None
    # 买入日 MA5（用于判定购入价高于/低于5日线）
    ma5_at_entry = None
    ed = pos.get("entry_date")
    if ed:
        try:
            sub = df[df["date"] <= pd.Timestamp(ed)]
            if len(sub) and pd.notna(sub.iloc[-1]["ma5"]):
                ma5_at_entry = float(sub.iloc[-1]["ma5"])
        except Exception:
            pass
    if ma5_at_entry is None:
        ma5_at_entry = ma5_today
    stop_clear, stop_reason, stop_mode = _stop_loss(
        entry=entry, ma5_at_entry=ma5_at_entry, close=close,
        ma5_today=ma5_today, ma5_yest=ma5_yest,
        close_y=close_y, close_2=close_2, prev_mode=pos.get("stop_mode"))

    # ⑤ 仓位管理 334 加仓/减仓提醒（仅对标记持仓生效；见 设计_仓位管理_334减仓加仓提醒.md）
    ma20_today = float(last["ma20"]) if pd.notna(last.get("ma20")) else None
    ma40_today = float(last["ma40"]) if pd.notna(last.get("ma40")) else None
    batch = int(pos.get("batch") or 1)
    ma5_danger = bool(pos.get("ma5_danger"))
    trim_count = int(pos.get("trim_count") or 0)
    last_low = float(pos.get("last_low") or close)
    was_below = bool(pos.get("was_below"))
    recovered = False
    if ma5_today is not None and was_below and close >= ma5_today:
        recovered = True
        ma5_danger = False
        trim_count = 0
        last_low = close
        was_below = False

    # 减仓带比例（设计 §四）
    reduce_signal = None
    if profit_reduce:
        reduce_signal = {"ratio": "减1/4", "reason": f"浮盈 {pnl * 100:.0f}% ≥ 30%"}
    elif (ma20_today is not None and ma40_today is not None
          and close < ma20_today and ma20_today > ma40_today):
        reduce_signal = {"ratio": "减1/3", "reason": "破MA20但MA20仍>MA40（未真破位）"}
    elif ma20_today and close > ma20_today:
        dev = (close - ma20_today) / ma20_today * 100
        if dev > 15:
            reduce_signal = {"ratio": "减1/3~1/2", "reason": f"收盘高于MA20达 {dev:.0f}%（涨幅过大防回吐）"}

    # staged 分批减仓（seq8 持仓侧：第一次破5日先减 1/3，第二次再破新低再减 1/3，第三次清仓）
    staged_signal = None
    if ma5_today is not None and entry > 0:
        if close < ma5_today:
            if not ma5_danger:
                ma5_danger = True
                trim_count = 1
                last_low = close
                staged_signal = {"ratio": "减1/3", "reason": "首次收盘跌破MA5，进入濒危并先减1/3"}
            elif close < last_low:
                if trim_count < 2:
                    trim_count += 1
                    staged_signal = {"ratio": "减1/3", "reason": f"MA5 濒危周期第{trim_count}次再破新低"}
                    last_low = close
                else:
                    staged_signal = {"ratio": "清仓", "reason": "MA5 濒危周期已减满两次，继续破位"}
        else:
            if recovered:
                staged_signal = None

    # 加仓提醒（设计 §三，仅 batch<3 时给建议）
    add_signal = None
    if batch < 3:
        breakout = (close >= hi20 * 0.998)
        profit_ok = (pnl is not None and pnl >= 0.08)
        if breakout and profit_ok and (ma5_today is not None and close > ma5_today):
            add_signal = {"to_batch": 3, "ratio": "满10成", "reason": "突破前高且浮盈≥8%且站稳MA5"}
        else:
            armed = (ma5_today is not None and ma5_yest is not None
                     and close_2 is not None and close_y is not None
                     and ma5_today > ma5_yest and close_2 < close_y
                     and close > ma5_today and close_y <= ma5_yest)
            pullback_hold = (ma20_today is not None and ma40_today is not None
                             and l <= ma20_today * 1.005 and close > ma40_today)
            if armed:
                add_signal = {"to_batch": 2, "ratio": "加至6成", "reason": "三重确认武装（MA5拐头+前两日连涨+收盘上穿MA5）"}
            elif pullback_hold:
                add_signal = {"to_batch": 2, "ratio": "加至6成", "reason": "回踩守住MA20未破MA40"}
    if add_signal:
        add_signal["label"] = _ext(add_signal["ratio"])
    if reduce_signal:
        reduce_signal["label"] = _ext(reduce_signal["ratio"])
    if staged_signal:
        staged_signal["label"] = _ext(staged_signal["ratio"])

    # SOP 规定的机械层优先级：T+1 -> -8% -> RSI14>70 -> 20日强平 -> staged 形态层。
    # ★2026-08-06 口径统一：原按**自然日**相减（entry_date→最新K线日期），但 SOP/回测
    # (backtest_dualpath 的 seen_days) 规格都是「20 个**交易日**」。自然日 20 天 ≈ 14
    # 交易日，会提前 6 个交易日强平。改为数入场日之后的 K 线根数（交易日计数），
    # 入场当日 = 0（T+1 闸门 hold_days==0 语义不变）。
    hold_days = None
    try:
        if pos.get("entry_date"):
            _ed = pd.Timestamp(pos["entry_date"]).date()
            _bars = pd.to_datetime(df["date"]).dt.date
            hold_days = max(0, int((_bars >= _ed).sum()) - 1)
    except Exception:
        hold_days = None
    t1_block = hold_days == 0
    rsi_clear = d_rsi > 70
    time_clear = hold_days is not None and hold_days >= 20
    if not t1_block and not stop_clear and rsi_clear:
        stop_clear = True
        stop_reason = "止盈清仓：日线RSI14 > 70"
    if not t1_block and not stop_clear and time_clear:
        stop_clear = True
        stop_reason = "强平清仓：持有达到20个交易日"

    reasons = []
    if doji_exit:
        reasons.append("高位十字星：建议当日或次日清仓")
    if shadow_exit:
        reasons.append("长下影：建议当日或次日清仓")
    if reduce_signal:
        reasons.append(f"{_ext(reduce_signal['ratio'])}提醒：{reduce_signal['reason']}")
    if stop_clear:
        reasons.append(stop_reason)
    if staged_signal:
        reasons.append(f"{_ext(staged_signal['ratio'])}提醒：{staged_signal['reason']}")
    if add_signal:
        # 实盘提醒只说明动作和原因；334 是建议框架，不把批次写成账户硬限制。
        reasons.append(f"{_ext(add_signal['ratio'])}提醒：{add_signal['reason']}")

    if doji_exit or shadow_exit or stop_clear:
        action = "清仓"
    elif staged_signal:
        action = "减仓"
    elif reduce_signal:
        action = "减仓"
    else:
        action = "持有"

    return {
        "secid": secid,
        "name": name,
        "entry_price": entry,
        "entry_date": pos.get("entry_date"),
        "shares": pos.get("shares"),
        "last_close": round(close, 2),
        "pnl_pct": round(pnl * 100, 1) if pnl is not None else None,
        "d_rsi": round(d_rsi, 1),
        "doji_exit": doji_exit,
        "shadow_exit": shadow_exit,
        "profit_reduce": profit_reduce,
        "stop_mode": stop_mode,
        "stop_clear": stop_clear,
        "hold_days": hold_days,
        "t1_block": t1_block,
        "ma5_danger": ma5_danger,
        "trim_count": trim_count,
        "last_low": round(last_low, 4),
        "was_below": was_below,
        "reclaimed": bool(pos.get("reclaimed")) or recovered,
        "staged_signal": staged_signal,
        "batch": batch,
        "add_signal": add_signal,
        "reduce_signal": reduce_signal,
        "reasons": reasons,
        "action": action,
    }


def compute_sells():
    """后台线程执行：对所有持仓做卖点判定，结果写入 _SELL_CACHE。"""
    with _SELL_LOCK:
        if _SELL_CACHE["computing"]:
            return None
        _SELL_CACHE["computing"] = True
    t0 = time.time()
    try:
        positions = load_positions()
        results = []
        mode_changed = False
        pos_changed = False
        for p in positions:
            try:
                r = _eval_one(p)
            except Exception as e:
                print(f"[sell] eval err {p.get('secid')}: {e}")
                continue
            results.append(r)
            # 持久化止损模式状态切换（cost8→ma5 仅触发一次，避免每日回退）
            if r.get("stop_mode") and p.get("stop_mode") != r["stop_mode"]:
                p["stop_mode"] = r["stop_mode"]
                mode_changed = True
            # 持久化 staged / 334 批次状态
            for k in ("batch", "ma5_danger", "trim_count", "last_low", "was_below", "reclaimed"):
                if r.get(k) is not None and p.get(k) != r.get(k):
                    p[k] = r.get(k)
                    pos_changed = True
        if mode_changed or pos_changed:
            save_positions(positions)
        res = {
            "ts": int(time.time()),
            "count": len(positions),
            "signals": results,
            "stats": {
                "positions": len(positions),
                "clear": sum(1 for r in results if r.get("action") == "清仓"),
                "reduce": sum(1 for r in results if r.get("action") == "减仓"),
                "add": sum(1 for r in results if r.get("add_signal")),
                "elapsed_sec": round(time.time() - t0, 1),
            },
        }
        with _SELL_LOCK:
            _SELL_CACHE["data"] = res
            _SELL_CACHE["ts"] = res["ts"]
            _SELL_CACHE["last_err"] = None
        print(f"[sell] 完成：持仓 {len(positions)} 只，清仓 {res['stats']['clear']} / 减仓 {res['stats']['reduce']} / 加仓提醒 {res['stats']['add']}，用时 {res['stats']['elapsed_sec']}s")
        return res
    except Exception as e:
        print(f"[sell] compute err:", e)
        with _SELL_LOCK:
            _SELL_CACHE["last_err"] = str(e)
        return None
    finally:
        with _SELL_LOCK:
            _SELL_CACHE["computing"] = False


def _bg_run():
    compute_sells()


def trigger_refresh():
    with _SELL_LOCK:
        if _SELL_CACHE["computing"]:
            return False
    threading.Thread(target=_bg_run, daemon=True).start()
    return True


def get_cache():
    with _SELL_LOCK:
        return _SELL_CACHE["data"]


def cache_ts():
    with _SELL_LOCK:
        return _SELL_CACHE["ts"]


def is_computing():
    with _SELL_LOCK:
        return _SELL_CACHE["computing"]


def last_err():
    with _SELL_LOCK:
        return _SELL_CACHE["last_err"]


if __name__ == "__main__":
    r = compute_sells()
    print(json.dumps(r, ensure_ascii=False, indent=2) if r else "none")
