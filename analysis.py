"""多周期共振 指标与机会点分析引擎（纯算法，无外部依赖）。

输入：统一格式的 K 线列表（通达信/新浪等来源经 server 归一化后的 dict），
  每根字段：date/open/close/high/low/vol/amount/amp/pct/change/turnover。

所有指标本地计算，供 server.py 在扫描/详情时调用。
买点规则直接来自用户自制 skill《多周期共振》：
  - 板块四态：周K MA20 vs MA40（主升/上涨初期/调整/下降），屏蔽下跌反弹
  - 个股：日K 距 MA20 乖离 < 8%；日K RSI 买点区 50-55；
          60分 金叉 + 缩量回踩 MA20(±2%)；15分 RSI(14) ∈ [35,50]
  - 筹码 90% 集中度 < 15% 不碰（详情接口有则校验）
信号做成"模块"列表，便于后续扩展新的选股条件（同屏并列）。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from board import classify_board


# ----------------------------------------------------------------------------
# 基础解析
# ----------------------------------------------------------------------------
def parse_kline(rows: list) -> list:
    """rows: list[str] -> 升序 list[dict]。跳过非法行。"""
    out = []
    for r in rows or []:
        if not isinstance(r, str):
            continue
        p = r.split(",")
        if len(p) < 11:
            continue
        try:
            out.append({
                "date": p[0],
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "vol": float(p[5]), "amount": float(p[6]),
                "amp": float(p[7]), "pct": float(p[8]),
                "change": float(p[9]), "turnover": float(p[10]),
            })
        except ValueError:
            continue
    return out


# ----------------------------------------------------------------------------
# 技术指标
# ----------------------------------------------------------------------------
def ma(vals: list, n: int) -> Optional[float]:
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def ma_series(vals: list, n: int) -> list:
    """返回等长序列，前 n-1 个为 None。"""
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist) 等长序列，不足补 None。"""
    L = len(closes)
    dif = [None] * L
    dea = [None] * L
    hist = [None] * L
    if L < slow:
        return dif, dea, hist
    ef = closes[0]
    es = closes[0]
    dif_raw = [ef - es]
    for i in range(1, L):
        ef = ef + (closes[i] - ef) * 2 / (fast + 1)
        es = es + (closes[i] - es) * 2 / (slow + 1)
        dif_raw.append(ef - es)
    dea_raw = [dif_raw[0]]
    for i in range(1, L):
        dea_raw.append(dea_raw[-1] + (dif_raw[i] - dea_raw[-1]) * 2 / (signal + 1))
    for i in range(L):
        dif[i] = dif_raw[i]
        dea[i] = dea_raw[i]
        hist[i] = 2 * (dif_raw[i] - dea_raw[i])
    return dif, dea, hist


def rsi(closes: list, n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def rsi_series(closes: list, n: int = 14) -> list:
    L = len(closes)
    out = [None] * L
    if L < n + 1:
        return out
    gains, losses = [], []
    for i in range(1, L):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # gains[k] 为第 k 步（close[k-1]->close[k]）的涨跌，索引 0..L-2
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for m in range(n + 1, L):
        # RSI(close[m]) 用第 m 步涨跌 = gains[m-1]
        ag = (ag * (n - 1) + gains[m - 1]) / n
        al = (al * (n - 1) + losses[m - 1]) / n
        out[m] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def kdj(highs: list, lows: list, closes: list, n: int = 9):
    """返回 (K, D, J) 最新值。"""
    L = len(closes)
    if L < n:
        return None, None, None
    k, d = 50.0, 50.0
    for i in range(n - 1, L):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100.0 if hh != ll else 0.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
    j = 3 * k - 2 * d
    return round(k, 2), round(d, 2), round(j, 2)


def bias_from_ma(close: float, ma_val: Optional[float]) -> Optional[float]:
    if ma_val is None or ma_val == 0:
        return None
    return (close - ma_val) / ma_val * 100.0


# ----------------------------------------------------------------------------
# 多周期共振框架
# ----------------------------------------------------------------------------
def classify_sector(weekly: list, daily: list) -> dict:
    """板块四态分类（基于周K MA20/MA40 + 日K 位置）。

    主升    = 周K MA20>MA40 且向上
    上涨初期 = 主升 + 日K 刚金叉
    调整    = 周K 走平/向下 + 日K<MA20
    下降    = 周K 向下 + 日K 死叉
    """
    res = {"state": "未知", "ma20_w": None, "ma40_w": None,
           "weekly_up": None, "daily_below_ma20": None, "note": ""}
    if not weekly or len(weekly) < 40:
        res["note"] = "周K数据不足(需>=40)"
        return res
    wc = [x["close"] for x in weekly]
    ma20w = ma(wc, 20)
    ma40w = ma(wc, 40)
    res["ma20_w"] = round(ma20w, 4) if ma20w else None
    res["ma40_w"] = round(ma40w, 4) if ma40w else None
    up = ma20w is not None and ma40w is not None and ma20w > ma40w
    res["weekly_up"] = up

    if daily and len(daily) >= 20:
        dc = [x["close"] for x in daily]
        ma20d = ma(dc, 20)
        below = ma20d is not None and dc[-1] < ma20d
        res["daily_below_ma20"] = below
        # 日K MACD 金叉判定
        dif, dea, _ = macd(dc)
        golden = dif[-1] is not None and dea[-1] is not None and \
            dif[-1] > dea[-1] and dif[-2] <= dea[-2]
        dead = dif[-1] is not None and dea[-1] is not None and \
            dif[-1] < dea[-1] and dif[-2] >= dea[-2]
        if up:
            res["state"] = "上涨初期" if golden else "主升"
        else:
            res["state"] = "下降" if dead else "调整"
    else:
        res["state"] = "主升" if up else "下降"
    return res


@dataclass
class SignalResult:
    key: str
    label: str
    passed: bool
    detail: str
    weight: int = 1
    subs: list = None   # 复合信号的子项拆分，形如 [{"name":str,"passed":bool,"value":str}]，用于部分符合着色


@dataclass
class StockEval:
    code: str
    name: str
    level: str                 # "触发" / "观察" / "无"
    score: int                 # 0-100
    close: float
    pct: float
    market: int = 1            # 1=沪 0=深（供前端回查详情用）
    signals: list = field(default_factory=list)   # List[SignalResult]
    extra: dict = field(default_factory=dict)
    entry: dict = field(default_factory=dict)      # 入场点位预测（确定性参考，非预测）


# 个股买点评估的"信号模块"定义（可插拔：后续加新选股条件只需往这里加）
def evaluate_stock(daily: list, weekly: list, m60: list = None,
                   m15: list = None, chip_90: Optional[float] = None,
                   code: str = "", name: str = "") -> StockEval:
    # 取基础信息（code/name 由调用方传入，K线接口本身不含代码/名称）
    last = daily[-1]
    close = last["close"]
    pct = last["pct"]

    signals: list = []

    # --- 模块1：周K 主升（MA20>MA40 向上）---
    weekly_up = False
    if weekly and len(weekly) >= 40:
        wc = [x["close"] for x in weekly]
        ma20w, ma40w = ma(wc, 20), ma(wc, 40)
        weekly_up = ma20w is not None and ma40w is not None and ma20w > ma40w
        signals.append(SignalResult(
            "weekly_up", "周K主升", weekly_up,
            f"MA20={ma20w:.2f} / MA40={ma40w:.2f}" if weekly_up else "周K未呈多头排列"))
    else:
        signals.append(SignalResult("weekly_up", "周K主升", False, "周K数据不足"))

    # --- 模块2：日K 距 MA20 乖离 < 8% ---
    dc = [x["close"] for x in daily]
    ma20d = ma(dc, 20)
    bias = bias_from_ma(close, ma20d)
    bias_ok = bias is not None and abs(bias) < 8.0
    signals.append(SignalResult(
        "bias_ok", "日K乖离MA20<8%", bias_ok,
        f"乖离={bias:.2f}%" if bias is not None else "MA20不足"))

    # --- 模块3：日K RSI(14) 买点区 50-55 ---
    r = rsi(dc, 14)
    rsi_ok = r is not None and 50 <= r <= 55
    signals.append(SignalResult(
        "rsi_ok", "日K RSI买点区(50-55)", rsi_ok,
        f"RSI={r:.1f}" if r is not None else "RSI不足"))

    # --- 模块4：日K MACD 金叉（含近 N 日刚金叉，作为上车信号）---
    dif, dea, hist = macd(dc)
    # 当前是否处于金叉状态（DIF 在 DEA 上方）
    in_golden = dif[-1] is not None and dea[-1] is not None and dif[-1] > dea[-1]
    # 严格金叉（本根发生）
    just_cross = in_golden and dif[-2] <= dea[-2]
    # 找最近一次金叉事件发生在多少根之前（窗口内即视为上车信号）
    GOLDEN_WINDOW = 5
    recent_cross_days = None
    for i in range(len(dif) - 1, max(0, len(dif) - GOLDEN_WINDOW - 1) - 1, -1):
        if i <= 0 or dif[i] is None or dea[i] is None or dif[i - 1] is None or dea[i - 1] is None:
            continue
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            recent_cross_days = len(dif) - 1 - i
            break
    # 判定：当前仍在金叉状态 且 最近一次金叉建立在窗口内（3-5 个交易日）
    daily_golden = in_golden and recent_cross_days is not None and \
        recent_cross_days <= GOLDEN_WINDOW
    if daily_golden:
        tag = "今日金叉" if recent_cross_days == 0 else f"金叉({recent_cross_days}日前)"
        daily_detail = f"{tag} DIF={dif[-1]:.3f}>DEA={dea[-1]:.3f}"
    else:
        daily_detail = "未金叉" if not in_golden else f"金叉超{GOLDEN_WINDOW}日"
    signals.append(SignalResult(
        "daily_macd", "日K MACD金叉", daily_golden, daily_detail))

    # --- 模块5：60分 金叉 + 缩量回踩 MA20(±2%) ---
    m60_subs = None
    m60_ok = False
    m60_detail = "未计算"
    m60_ma20 = None
    m60_ma40 = None
    m60_last = None
    if m60 and len(m60) >= 20:
        mc = [x["close"] for x in m60]
        m60_ma20 = ma(mc, 20)
        m60_ma40 = ma(mc, 40) if len(mc) >= 40 else None
        m60_last = mc[-1]
        m60_dif, m60_dea, _ = macd(mc)
        m60_golden = m60_dif[-1] is not None and m60_dea[-1] is not None and \
            m60_dif[-1] > m60_dea[-1] and m60_dif[-2] <= m60_dea[-2]
        # 缩量：近3根量 < 前20根均量
        recent_vol = sum(x["vol"] for x in m60[-3:]) / 3
        avg_vol = sum(x["vol"] for x in m60[-20:]) / 20
        shrink = recent_vol < avg_vol
        m60_bias = bias_from_ma(close, m60_ma20)
        backtest = m60_bias is not None and abs(m60_bias) <= 2.0
        m60_ok = m60_golden and shrink and backtest
        m60_detail = f"金叉={m60_golden} 缩量={shrink} 回踩±2%={backtest}"
        m60_subs = [
            {"name": "金叉", "passed": m60_golden, "value": str(m60_golden)},
            {"name": "缩量回踩", "passed": shrink, "value": str(shrink)},
            {"name": "回踩±2%", "passed": backtest, "value": str(backtest)},
        ]
    signals.append(SignalResult("m60_ok", "60分金叉+缩量回踩MA20", m60_ok, m60_detail, subs=m60_subs))

    # --- 模块6：15分 RSI(14) ∈ [35,50] ---
    r15 = None
    m15_ok = False
    m15_detail = "未计算"
    if m15 and len(m15) >= 15:
        mc15 = [x["close"] for x in m15]
        r15 = rsi(mc15, 14)
        m15_ok = r15 is not None and 35 <= r15 <= 50
        m15_detail = f"RSI15={r15:.1f}" if r15 is not None else "RSI不足"
    signals.append(SignalResult("m15_ok", "15分RSI∈[35,50]", m15_ok, m15_detail))

    # --- 模块7：筹码 90% 集中度（有则校验，<15% 不碰）---
    if chip_90 is not None:
        chip_ok = chip_90 >= 15.0
        signals.append(SignalResult(
            "chip_ok", "筹码90%集中度>=15%", chip_ok,
            f"集中度={chip_90:.1f}%" if chip_ok else "集中度过低，规避"))
    else:
        signals.append(SignalResult("chip_ok", "筹码集中度", True, "无数据，跳过"))

    # --- 综合分级（动态门槛：分钟级未算时不计入门槛）---
    hard = weekly_up and bias_ok  # 硬门槛：主升 + 乖离可控
    core_signals = [daily_golden, rsi_ok]
    if m60 is not None:
        core_signals.append(m60_ok)
    if m15 is not None:
        core_signals.append(m15_ok)
    n_core = len(core_signals)
    core_pass = sum(1 for s in core_signals if s)
    passed = [s for s in signals if s.passed]
    score = round(len(passed) / len(signals) * 100)

    if hard and n_core >= 2 and core_pass == n_core:
        level = "触发"
    elif hard or (weekly_up and bias_ok):
        level = "观察"
    else:
        level = "无"

    # --- 入场点位预测（确定性计算，非预测；a=支撑/阻力价位区，b=背离参考价）---
    entry = {"support_resist": [], "divergence": []}
    # a. 支撑/阻力买点区
    if ma20d is not None:
        entry["support_resist"].append({
            "type": "日K回踩MA20(±2%)买点区",
            "low": round(ma20d * 0.98, 2), "high": round(ma20d * 1.02, 2),
            "ref": round(ma20d, 2)})
    if len(dc) >= 20:
        prev_low = round(min(dc[-60:]), 2)
        entry["support_resist"].append({"type": "近60日最低支撑", "price": prev_low})
    # b. 背离参考价（v2 · 用户标准：绿柱收敛+圆弧底有界+近5日金叉，红柱+MA5向上=生成）
    if r is not None:
        entry["divergence"].append({
            "type": "RSI(14)超卖参考", "value": round(r, 1),
            "note": "RSI<35 进入超卖关注区" if r < 35 else "未进入超卖区"})
    if len(dif) >= 20 and len(hist) >= 20:
        DV_WINDOW = 5   # 用户拍板：日线底背离只找近5日买点；超5日多为强势期
        n = len(dc)
        # 1) 绿柱收敛：最近一段绿柱段（hist<0 连续段，可能很长），
        #    取 |HIST| 峰值之后的收敛段（递减趋0），收敛段末端须在近5日内。
        #    （绿柱段前半是下跌加速的放大段，不算收敛，只看峰值后）
        j = n - 1
        while j >= 0 and hist[j] is not None and hist[j] >= 0:
            j -= 1
        end_green = j                       # 绿柱段末端（可能 -1 = 无绿柱）
        seg_start = end_green
        while seg_start >= 0 and hist[seg_start] is not None and hist[seg_start] < 0:
            seg_start -= 1
        seg_start += 1                      # 绿柱段起点
        green_shrink = False
        if end_green >= n - DV_WINDOW and end_green >= 0:
            seg = [abs(hist[k]) for k in range(seg_start, end_green + 1)]
            peak_i = seg.index(max(seg))    # 峰值 = 放大→收敛分界
            tail = seg[peak_i:]             # 峰值之后 = 收敛段
            mon_dec = all(tail[k + 1] <= tail[k] for k in range(len(tail) - 1))
            last_v = tail[-1]
            green_shrink = (len(tail) >= 2 and mon_dec
                            and last_v <= max(seg) * 0.7)
        # 2) 圆弧底/有下界：近5日最低收盘价落在前半段，其后不破（下界明确）
        win5 = dc[-DV_WINDOW:]
        lo5 = min(win5)
        lo_idx = n - DV_WINDOW + win5.index(lo5)
        round_bottom = lo_idx <= n - 3      # 低点不晚于倒数第3根（前半段见底）
        if round_bottom:
            round_bottom = all(x >= lo5 * 0.995 for x in dc[lo_idx + 1:])
        # 3) 近5日内出现过 DIF 上穿 DEA（金叉事件）
        golden_near = False
        for j in range(n - DV_WINDOW, n):
            if j <= 0:
                continue
            if dif[j] is None or dea[j] is None or dif[j - 1] is None or dea[j - 1] is None:
                continue
            if dif[j] > dea[j] and dif[j - 1] <= dea[j - 1]:
                golden_near = True
                break
        # 4) 触发确认：当日日K红柱 + MA5 向上 → 底背离生成
        red_bar = hist[-1] is not None and hist[-1] > 0
        ma5s = ma_series(dc, 5)
        ma5_up = (ma5s[-1] is not None and ma5s[-2] is not None
                  and ma5s[-1] > ma5s[-2])
        bottom_div = green_shrink and round_bottom and golden_near and red_bar and ma5_up
        if bottom_div:
            div_note = (f"绿柱收敛{len(tail)}根+圆弧底有界+近5日金叉，"
                        f"红柱且MA5向上=底背离生成")
        else:
            fails = []
            if not green_shrink:
                fails.append("绿柱未收敛")
            if not round_bottom:
                fails.append("无圆弧底/破下界")
            if not golden_near:
                fails.append("近5日无金叉")
            if not red_bar:
                fails.append("未现红柱")
            if not ma5_up:
                fails.append("MA5未向上")
            div_note = "未生成：" + "、".join(fails[:2]) + ("…" if len(fails) > 2 else "")
        entry["divergence"].append({
            "type": "MACD底背离", "value": "生成" if bottom_div else "无",
            "note": div_note})

    # --- 卖侧辅助指标（供 judge_state 使用）---
    amp = last.get("amp")
    vol_ratio = None
    if len(dc) >= 6:
        avg5v = sum(x["vol"] for x in daily[-6:-1]) / 5
        if avg5v > 0:
            vol_ratio = last["vol"] / avg5v
    vol_burst = False
    if len(dc) >= 20:
        avg20v = sum(x["vol"] for x in daily[-20:]) / 20
        if avg20v > 0:
            vol_burst = last["vol"] >= 1.8 * avg20v

    # 破位性质判别辅助（真破位 vs 洗盘）：下影线 + 日线MA40
    ma40d = ma(dc, 40) if len(dc) >= 40 else None
    hi = last.get("high"); lo = last.get("low")
    lower_shadow_ratio = None
    if hi is not None and lo is not None and hi > lo and close is not None:
        lower_shadow_ratio = (close - lo) / (hi - lo)

    return StockEval(
        code=code, name=name, level=level, score=score,
        close=close, pct=pct, signals=signals,
        extra={
            "bias": round(bias, 2) if bias is not None else None,
            "rsi": round(r, 1) if r is not None else None,
            "rsi15": round(r15, 1) if r15 is not None else None,
            "ma20d": round(ma20d, 2) if ma20d is not None else None,
            "ma40d": round(ma40d, 2) if ma40d is not None else None,
            "lower_shadow_ratio": round(lower_shadow_ratio, 2) if lower_shadow_ratio is not None else None,
            "m60_last": round(m60_last, 2) if m60_last is not None else None,
            "m60_ma20": round(m60_ma20, 2) if m60_ma20 is not None else None,
            "m60_ma40": round(m60_ma40, 2) if m60_ma40 is not None else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "amp_today": round(amp, 2) if amp is not None else None,
            "vol_burst": bool(vol_burst),
        },
        entry=entry)


def kline_context(daily: list) -> dict:
    """从日K提取"形态/量价事实"（纯规则、无预测），供一句话总结引用。

    输出:
      desc      —— 可直接拼进文案的中文事实串（箱体/位置/均线排列/量能/近5日）
      box_low/box_high/pos_pct/vol_ratio/trend 等结构化字段
    """
    if not daily or len(daily) < 20:
        return {}
    closes = [x["close"] for x in daily]
    last = daily[-1]
    close = last["close"]

    # 1) 箱体判断：近60日高低区间 + 当前位置百分位；振幅<25% 视为箱体运行
    win = daily[-60:] if len(daily) >= 60 else daily
    box_high = max(x["high"] for x in win)
    box_low = min(x["low"] for x in win)
    box_range = (box_high - box_low) / box_low * 100 if box_low > 0 else None
    pos_pct = (close - box_low) / (box_high - box_low) * 100 if box_high > box_low else 50.0
    in_box = box_range is not None and box_range < 25

    # 2) 均线排列（MA5/10/20）
    ma5v, ma10v, ma20v = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    trend = "均线数据不足"
    if ma5v and ma10v and ma20v:
        if ma5v > ma10v > ma20v:
            trend = "短期均线多头排列"
        elif ma5v < ma10v < ma20v:
            trend = "短期均线空头排列"
        else:
            trend = "均线纠缠"

    # 3) 量能：今日量 vs 前5日均量
    vol_ratio = None
    vol_desc = ""
    if len(daily) >= 6:
        avg5 = sum(x["vol"] for x in daily[-6:-1]) / 5
        if avg5 > 0:
            vol_ratio = last["vol"] / avg5
            if vol_ratio >= 1.5:
                vol_desc = f"今日放量{'上攻' if last['pct'] > 0 else '下杀'}({vol_ratio:.1f}倍5日均量)"
            elif vol_ratio <= 0.7:
                vol_desc = f"量能萎缩({vol_ratio:.1f}倍5日均量)"
            else:
                vol_desc = "量能平稳"

    # 4) 近5日涨跌幅
    chg5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 else None

    facts = []
    if in_box:
        facts.append(f"近60日箱体运行({box_low:.2f}~{box_high:.2f})，现处箱体{pos_pct:.0f}%位置")
    else:
        facts.append(f"近60日区间{box_low:.2f}~{box_high:.2f}(振幅{box_range:.0f}%)，现处{pos_pct:.0f}%分位")
    facts.append(trend)
    if vol_desc:
        facts.append(vol_desc)
    if chg5 is not None:
        facts.append(f"近5日{chg5:+.1f}%")

    return {
        "box_low": round(box_low, 2), "box_high": round(box_high, 2),
        "box_range": round(box_range, 1) if box_range is not None else None,
        "in_box": in_box, "pos_pct": round(pos_pct, 1),
        "trend": trend, "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        "chg5": round(chg5, 1) if chg5 is not None else None,
        "desc": "；".join(facts),
    }


def _state(key, code, severity, advice, signals):
    return {"key": key, "code": code, "severity": severity,
            "advice": advice,
            "signals": [{"name": n, "key": k} for n, k in signals]}


def classify_breakdown(e: "StockEval", sector_state: str = None) -> dict:
    """破位性质概率判别：真破位(REAL) / 洗盘(WASH) / 模糊(AMBIGUOUS)。

    仅输出概率性线索，不替用户下结论（契合"建议非决策"铁则）。
    依据：①下影线长短 ②量能签名(放量下杀/缩量) ③MA40支撑
          ④均线结构(MA20 vs MA40) ⑤板块环境(可选)。
    注：T0 时刻无法预知次日是否收回MA20，故判别必然概率性。
    """
    extra = e.extra or {}
    vr = extra.get("vol_ratio")
    shadow = extra.get("lower_shadow_ratio")
    ma20d = extra.get("ma20d")
    ma40d = extra.get("ma40d")
    close = e.close
    pct = e.pct
    score = 0
    reasons = []
    if shadow is not None:
        if shadow >= 0.6:
            score += 2; reasons.append("长下影，下方有承接（洗盘特征）")
        elif shadow <= 0.3:
            score -= 1; reasons.append("无下影/光脚阴")
    if vr is not None:
        if vr <= 0.8:
            score += 2; reasons.append(f"缩量({vr:.1f}倍5日均量)，非恐慌出逃")
        elif vr >= 1.5 and pct is not None and pct < 0:
            score -= 2; reasons.append(f"放量下杀({vr:.1f}倍)，出逃迹象")
    if close is not None and ma40d is not None:
        if close > ma40d:
            score += 1; reasons.append("仍在MA40上方，主支撑未破")
        else:
            score -= 2; reasons.append("已跌破MA40，趋势转弱")
    if ma20d is not None and ma40d is not None:
        if ma20d > ma40d:
            score += 1; reasons.append("MA20>MA40 多头结构未破坏")
        else:
            score -= 1; reasons.append("MA20<MA40 已死叉")
    if sector_state in ("主升", "上涨初期"):
        score += 1; reasons.append("所属板块仍处强势")
    elif sector_state in ("下降",):
        score -= 1; reasons.append("所属板块已转弱")
    if score >= 3:
        verdict = "WASH"
    elif score <= -2:
        verdict = "REAL"
    else:
        verdict = "AMBIGUOUS"
    conf = min(0.9, 0.5 + abs(score) * 0.1)
    return {"verdict": verdict, "confidence": round(conf, 2),
            "score": score, "reasons": reasons}


def judge_state(e: "StockEval", holding: bool = False) -> dict:
    """个股全生命周期状态判断（右侧动量框架）。

    输出状态 dict：key / code / severity(normal|warn|danger) / advice / signals。
    - holding=False（未持仓）：观察 / 入场 / 回避 / 空仓
    - holding=True（持仓）：离场(铁律) / 持仓-减仓 / 持仓-加仓 / 持仓

    卖点规则（来自《多周期共振》框架 + 华勤技术 603296 实战实证）：
      1. 日K放量滞涨 + 15分RSI>75 + 长上影
      2. 60分跌破MA20 + 15分RSI<45
      3. 60分跌破MA40
      4. 日K跌破MA20
      5. 爆量滞涨（量创近20日新高且涨幅远小于振幅）—— 华勤6/2实证强离场信号
    设计原则：本程序输出"建议"而非"决策"，决策权永远在用户。
    """
    extra = e.extra or {}
    sig_pass = {s.key: s.passed for s in (e.signals or [])}
    rsi = extra.get("rsi")
    rsi15 = extra.get("rsi15")
    bias = extra.get("bias")
    ma20d = extra.get("ma20d")
    m60_last = extra.get("m60_last")
    m60_ma20 = extra.get("m60_ma20")
    m60_ma40 = extra.get("m60_ma40")
    vol_ratio = extra.get("vol_ratio")
    amp = extra.get("amp_today")
    vol_burst = extra.get("vol_burst")
    pct = e.pct
    close = e.close

    # —— 卖点信号收集 ——
    sell = []
    if rsi is not None and rsi > 70:
        sell.append(("RSI超买>70", "rsi_high"))
    if bias is not None and bias > 8.0:
        sell.append(("日K乖离>8%", "bias_high"))
    if vol_ratio is not None and vol_ratio >= 1.5 and amp is not None \
            and abs(pct) < amp * 0.4:
        sell.append(("放量滞涨", "vol_stall"))
    if m60_last is not None and m60_ma20 is not None and m60_last < m60_ma20:
        sell.append(("60分破MA20", "m60_ma20_break"))
    if m60_last is not None and m60_ma40 is not None and m60_last < m60_ma40:
        sell.append(("60分破MA40", "m60_ma40_break"))
    if close is not None and ma20d is not None and close < ma20d:
        sell.append(("日K破MA20", "daily_ma20_break"))
    if vol_burst and amp is not None and abs(pct) < amp * 0.35:
        sell.append(("爆量滞涨", "vol_burst_stall"))

    # 离场铁律 = 真顶信号（回测验证：RSI>70 胜率100%、放量/爆量滞涨70%+）
    # 减仓 = 洗盘/可观察信号（破MA20、60分破MA40：回测胜率偏低，留给用户定夺）
    EXIT_KEYS = {"rsi_high", "vol_stall", "vol_burst_stall"}
    has_exit = any(k in EXIT_KEYS for _, k in sell)

    if holding:
        # 持仓上下文：离场铁律最高优先级，减仓次之，加仓/持仓再次
        if has_exit:
            return _state("离场", "EXIT", "danger",
                          "⚠️ 离场铁律触发（RSI超买/放量滞涨）：持仓建议清仓，请立即执行"
                          "（决策权在你，但信号强烈建议离场）", sell)
        if sell:
            advice = "减仓信号出现，建议减仓（减多少由你决定：1/2、2/3 或全仓）。"
            daily_break = any(k == "daily_ma20_break" for _, k in sell)
            if daily_break:
                bd = classify_breakdown(e)
                if bd["verdict"] == "WASH":
                    vtxt = f"系统判别：疑似洗盘(置信{bd['confidence']:.0%})——{';'.join(bd['reasons'])}"
                elif bd["verdict"] == "REAL":
                    vtxt = f"系统判别：疑似真破位(置信{bd['confidence']:.0%})——{';'.join(bd['reasons'])}"
                else:
                    vtxt = f"系统判别：信号模糊(置信{bd['confidence']:.0%})——{';'.join(bd['reasons'])}"
                advice = (f"日K跌破MA20：{vtxt}。"
                          "你可决定止损离场，也可干熬持有；最终是否走，"
                          "请结合个股质地与板块是否同步破位再分析（决策权在你）。")
            return _state("持仓-减仓", "HOLD_REDUCE", "warn", advice, sell)
        add_ok = (sig_pass.get("m60_ok") or sig_pass.get("daily_macd")) \
            and (rsi is None or rsi < 70) and (bias is None or bias < 8)
        if add_ok:
            return _state("持仓-加仓", "HOLD_ADD", "normal",
                          "持仓未破位且仍处共振，可逢回踩加仓", [])
        return _state("持仓", "HOLDING", "normal", "持仓中，无明确加减仓信号", [])
    else:
        # 未持仓：破MA20/60分破MA20 仅作弱势降级，不强制回避
        if has_exit:
            return _state("回避", "AVOID", "danger",
                          "趋势走坏（主力出货/顶部信号），暂不宜介入", sell)
        weak = any(k in ("daily_ma20_break", "m60_ma20_break") for _, k in sell)
        if e.level == "触发":
            if weak:
                return _state("观察", "WATCH", "normal",
                              "异动出现但已跌破短期均线，先观察不急于入场", sell)
            return _state("入场", "ENTRY", "normal",
                          "多周期共振触发，可择机入场（决策权在你）", [])
        if e.level == "观察":
            return _state("观察", "WATCH", "normal",
                          "出现异动但尚未共振，列入观察池", [])
        return _state("空仓", "NONE", "normal", "暂无明确信号", [])


def eval_to_dict(e: StockEval, holding: bool = False) -> dict:
    return {
        "code": e.code, "name": e.name, "level": e.level,
        "score": e.score, "close": e.close, "pct": e.pct, "market": e.market,
        "board": classify_board(e.code),
        "state": judge_state(e, holding),
        "signals": [{"key": s.key, "label": s.label,
                     "passed": s.passed, "detail": s.detail,
                     "subs": s.subs} for s in e.signals],
        "extra": e.extra,
        "entry": e.entry,
    }


if __name__ == "__main__":
    # 轻量自测：用内置样例 K 线验证指标函数（不依赖外部网络）
    import math
    sample = []
    price = 100.0
    for i in range(120):
        price += math.sin(i / 5.0) * 1.5 + (1 if i % 7 == 0 else 0)
        sample.append({"date": f"2025-01-{i % 28 + 1:02d}", "open": price,
                       "close": price, "high": price + 1, "low": price - 1,
                       "vol": 1000 + i * 10, "amount": 0.0, "amp": 1.0,
                       "pct": 0.0, "change": 0.0, "turnover": 1.0})
    closes = [x["close"] for x in sample]
    print("bars:", len(sample))
    print("MA20:", round(ma(closes, 20), 2))
    print("RSI14:", rsi(closes, 14))
    dif, dea, hist = macd(closes)
    print("MACD last:", round(dif[-1], 3), round(dea[-1], 3), round(hist[-1], 3))
    ev = evaluate_stock(sample, sample)
    print("eval level:", ev.level, "score:", ev.score)
    for s in ev.signals:
        print("  ", s.label, s.passed, s.detail)
    print("state:", judge_state(ev))


# ----------------------------------------------------------------------------
# 指数级技术分析（大盘评述用）
# ----------------------------------------------------------------------------
def index_technical(daily: list) -> dict:
    """对单只指数的日K做技术面分析，输出结构化事实供大盘评述引用。

    返回:
      ma_arrange   —— 均线排列描述（多头/空头/纠缠）
      macd_state   —— MACD状态���金叉/死叉/多头/空头）
      vol_price    —— 量价形态关键词（量价齐升/放量滞涨/缩量阴跌/地量企稳/放量下跌）
      support      —— 近期下方支撑位
      resistance   —— 近期上方阻力位
      rsi_val      —— RSI(14)
      pct_today    —— 今日涨跌幅
      pattern      —— 一键文案（自然语言短语）
    """
    if not daily or len(daily) < 20:
        return {"pattern": "数据不足"}
    closes = [x["close"] for x in daily]
    volumes = [x["vol"] for x in daily]
    last = daily[-1]
    close = last["close"]
    pct_today = last.get("pct", 0)

    # ---- 均线排列 (MA5/10/20/60) ----
    ma5 = ma(closes, 5)
    ma10 = ma(closes, 10)
    ma20 = ma(closes, 20)
    ma60 = ma(closes, 60)
    if all([ma5, ma10, ma20, ma60]):
        if ma5 > ma10 > ma20 > ma60:
            ma_arrange = "均线多头排列(5>10>20>60)"
        elif ma5 < ma10 < ma20 < ma60:
            ma_arrange = "均线空头排列"
        elif ma5 > ma10 > ma20 and ma20 <= ma60:
            ma_arrange = "短期多头但受压于中期均线"
        elif ma5 < ma10 < ma20 and ma20 >= ma60:
            ma_arrange = "短期空头但中期有支撑"
        else:
            ma_arrange = "均线纠缠"
    elif all([ma5, ma10, ma20]):
        if ma5 > ma10 > ma20:
            ma_arrange = "短期均线多头排列(5>10>20)"
        elif ma5 < ma10 < ma20:
            ma_arrange = "短期均线空头排列"
        else:
            ma_arrange = "均线纠缠"
    else:
        ma_arrange = "均线数据不足"

    # ---- MACD 状态 ----
    dif, dea, hist = macd(closes)
    if dif and dea and len(dif) >= 2:
        if dif[-1] > dea[-1] and dif[-2] <= dea[-2]:
            macd_state = "金叉"
        elif dif[-1] < dea[-1] and dif[-2] >= dea[-2]:
            macd_state = "死叉"
        elif dif[-1] > dea[-1]:
            macd_state = "多头运行(DIF>DEA)"
        else:
            macd_state = "空头运行(DIF<DEA)"
    else:
        macd_state = "未知"

    # ---- 量价形态分类 ----
    vol_price = "量能平稳"
    if len(daily) >= 6:
        avg5_vol = sum(volumes[-6:-1]) / 5
        avg20_vol = sum(volumes[-20:-1]) / 19 if len(volumes) >= 21 else avg5_vol
        if avg5_vol > 0:
            vr = volumes[-1] / avg5_vol
            if pct_today > 0.5 and vr >= 1.3:
                vol_price = "量价齐升"
            elif pct_today > 0.5 and vr < 0.7:
                vol_price = "缩量上涨(动能存疑)"
            elif pct_today < -0.5 and vr >= 1.3:
                vol_price = "放量下跌"
            elif pct_today < -0.5 and vr < 0.7:
                vol_price = "缩量阴跌"
            elif abs(pct_today) <= 0.5 and vr >= 1.5:
                vol_price = "放量滞涨(多空分歧大)"
            elif abs(pct_today) <= 0.3 and vr <= 0.5:
                vol_price = "地量企稳(变盘前兆)"
            elif abs(pct_today) <= 0.5 and vr >= 1.2:
                vol_price = "横盘放量"

    # ---- 支撑/阻力位：近60日高低点 + 近20日局部极值 ----
    win = daily[-60:] if len(daily) >= 60 else daily
    win_high = max(x["high"] for x in win)
    win_low = min(x["low"] for x in win)

    recent = daily[-20:] if len(daily) >= 20 else daily
    resist_candidates = []
    support_candidates = []
    for i in range(1, len(recent) - 1):
        if recent[i]["high"] >= recent[i-1]["high"] and recent[i]["high"] >= recent[i+1]["high"]:
            resist_candidates.append((recent[i]["high"], recent[i]["date"]))
        if recent[i]["low"] <= recent[i-1]["low"] and recent[i]["low"] <= recent[i+1]["low"]:
            support_candidates.append((recent[i]["low"], recent[i]["date"]))
    resistance = None
    if resist_candidates:
        resist_candidates.sort(key=lambda x: x[0], reverse=True)
        for rval, _ in resist_candidates:
            if rval > close * 1.01:
                resistance = round(rval, 2)
                break
        if resistance is None:
            resistance = round(resist_candidates[0][0], 2)
    support = None
    if support_candidates:
        support_candidates.sort(key=lambda x: x[0])
        for sval, _ in support_candidates:
            if sval < close * 0.99:
                support = round(sval, 2)
                break
        if support is None:
            support = round(support_candidates[0][0], 2)

    rsi14 = rsi(closes, 14)

    parts = [ma_arrange, f"MACD{macd_state}", vol_price]
    if support:
        parts.append(f"支撑≈{support}")
    if resistance:
        parts.append(f"阻力≈{resistance}")

    return {
        "ma_arrange": ma_arrange,
        "macd_state": macd_state,
        "vol_price": vol_price,
        "support": support,
        "resistance": resistance,
        "rsi": round(rsi14, 1) if rsi14 else None,
        "pct_today": round(pct_today, 2),
        "close": round(close, 2),
        "pattern": "；".join(parts),
    }
