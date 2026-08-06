# -*- coding: utf-8 -*-
"""
picks.py —— 选股买卖点 · 实时扫描（web 版，配套 选股买卖点SOP.md v1）

设计（与 SOP 对齐）：
  · 板块门控(层A)：复用 server.sector_matrix() 产出的「主推/观察」板块标签，取标签最前的 N 个热点板块。
    （SOP §1：层A 找低位启动/良好上行的板块，避免追高；此处直接吃 server 已算好的四态+RS+资金流 label）
  · 量价筛选(层B)：v1 先不做三库并集硬闸（实时无筹码集中度/换手率代理），仅做主板+价4.3-40 粗过滤，
    其余严格留给五条件共振；后续可接 meets_A 等价。
  · 五条件(①②③④⑤)·双路径（2026-08-05 共识）：所有票先进周K总闸(周MA20>MA40且向上)
    再按周RSI>50 分「主升/初期」两路径，①②按路径取不同定义，③④⑤两路径共用：
    主升①=周RSI>50&周MA排列向上 / 初期①=仅周MA排列向上(无RSI要求)；
    主升②=close>MA20>MA40 / 初期②=日MA20刚金叉MA40+放量突破平台+RSI45-65+温和放大；
    ③60分金叉后缩量回踩MA20 / ④15分收敛(RSI35-50+缩量) / ⑤15分低位买点确认(RSI35-50拐头/企稳)。
  · 4-Tier 观察池（SOP §4.1）：仅作注意力分级，不参与上车判定；上车=v1 不改（§9.1 冻结）。
  · 实时数据：server._klines(secid,klt,lmt) 走 easy_tdx（单票三周期实测 ~0.4s）。

性能：threaded=False 单线程服务，故扫描在后台线程预计算，/api/picks 只秒回缓存。
  扫描规模上限（PICKS_*）按单票~0.4s 估算：12板块×40只=480只 → 约 3~4 分钟一轮（后台）。
免责：所有产出仅供研究参考，不构成投资建议（SOP §10.10）。
"""
from __future__ import annotations
import os
import time
import json
import threading
import faulthandler
import pandas as pd
import numpy as np
import requests
import paths

# 延迟导入 server（避免与 server 的 `import picks` 形成 import 期环；运行时 server 已完全加载）
import server
from llm_client import load_config   # 读 intradaily_alert_enabled 开关

# ===== 参数（与回测引擎 dynamic_pool_v3.py 定稿值严格一致）=====
# ★2026-08-04 对齐：原实盘 C4_TAIL_BARS=8 / C5_TAIL_BARS=3 比回测严得多，
#   等于用一套从未验证过的口径在跑（回测 48.2% 胜率那组用的是 0/0 全天任意）。
RSI_W = 14
C4_TAIL_BARS = 0        # ④ 0=全天任意15分K内找收敛段（对齐回测 2026-08-03 放宽）
C4_BAND = (35, 50)      # ④ 收敛段 RSI 区间
C4_MIN_BARS = 3         # ④ 收敛段最少根数
C4_SHRINK = 0.8         # ④ 缩量阈值（量比 <）
C5_RUN = 2              # ⑤ 连续递增根数
C5_EXPAND = 1.0         # ⑤ 放量阈值（量比 >）
C5_TAIL_BARS = 0        # ⑤ 0=尾盘闸已去掉（对齐回测 2026-08-03 放宽）
SEQ_WINDOW = 8          # ★序贯状态机：④ 武装后最多等 N 个交易日等 ⑤（用户 2026-08-03 拍板 N=8）
SEQ_SCAN_DAYS = 25      # 状态机回溯的交易日数（须 > SEQ_WINDOW，受 15分K 拉取根数限制）
SHRINK_RATIO = 0.8      # ③④⑤ 统一缩量比例
PULLBACK_DEV = 0.02     # ③ 回踩偏离绝对值上限（旧口径 |dev|<2%）
# ===== 双路径（主升/初期）判定（2026-08-05 共识）=====
EARLY_GOLDEN_WINDOW = 20   # 初期②：日MA20金叉MA40 须在近 N 个交易日内（"刚形成"）
EARLY_BREAKOUT_VOL = 1.0   # 初期②：放量突破平台（温和放大，量比>1.0；>1.5 为显著放量）
DAILY_BARS = 320           # ① 周MA40 需≈40周≈280交易日，日线拉足以算周MA排列/斜率
ENTRY_MAX_ABOVE_MA5 = 1.0  # ★入场过滤闸门（与全盘回测同口径）：信号日收盘不得高于当日 MA5，剔除高位追入
# 注：C5_RUN/C5_EXPAND 原用于"⑤放量启动"，2026-08-05 起 ⑤ 改为 RSI 低位买点确认，
#     不再依赖量比；常量保留供注释/回测根对照，运行期 ⑤ 由 _buy_confirm 判定。

# 盘中预警价格区间（设计_盘中预警带价挂单工作流.md §四）
K_ATR = 0.5              # ATR15 容差系数（回测后微调）
PCT_MIN = 0.005         # 容差带下限（0.5%）

# ===== 扫描规模上限（实时 easy_tdx，后台跑）=====
PICKS_MAX_SECTORS = 12      # 取标签最前的 N 个热点板块（主推优先，其次观察）
PICKS_MAX_PER_SECTOR = 40   # 每板块最多扫 N 只成分股
PICKS_MAX_STOCKS = 480      # 总上限（≈ 3~4 分钟一轮）
PICKS_SINA_MAX = 180        # ★TDX 死亡兜底：新浪动量宇宙扫描上限（Sina kline ~2s/票，控时长）

# ===== 全局缓存（后台预计算结果）=====
_PICKS_CACHE = {"ts": 0, "data": None, "computing": False, "last_err": None,
                # 扫描进度（前台/排障可见）：stage 阶段名，scanned/total 已扫/总数
                "progress": {"stage": "", "scanned": 0, "total": 0, "hit": 0}}
_PICKS_LOCK = threading.Lock()
_PICKS_CACHE_FILE = paths.data_path("_picks_cache.json")


def _today_str():
    return time.strftime("%Y-%m-%d", time.localtime())


def _load_picks_cache_file():
    """启动时从磁盘恢复缓存；仅当文件日期为当天有效，否则清空（每日 24:00 后失效）。"""
    global _PICKS_CACHE
    try:
        with open(_PICKS_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return
        if payload.get("date") != _today_str():
            # 跨天：删除旧缓存，返回空（下次调用触发刷新）
            try:
                os.remove(_PICKS_CACHE_FILE)
            except OSError:
                pass
            return
        with _PICKS_LOCK:
            _PICKS_CACHE["data"] = payload.get("data")
            _PICKS_CACHE["ts"] = payload.get("ts", 0)
            _PICKS_CACHE["last_err"] = payload.get("last_err")
            _PICKS_CACHE["computing"] = False
        _data = payload.get("data")
        if not isinstance(_data, dict):
            _data = {}
        _full = len(_data.get("full_match") or [])
        _pool = len(_data.get("pool") or [])
        print(f"[picks] 已从磁盘恢复缓存（{_PICKS_CACHE_FILE}），"
              f"full={_full} pool={_pool}")
    except (FileNotFoundError, ValueError, OSError):
        pass


def _save_picks_cache_file():
    """缓存更新后原子写入磁盘。"""
    global _PICKS_CACHE
    tmp = _PICKS_CACHE_FILE + ".tmp"
    try:
        with _PICKS_LOCK:
            payload = {
                "date": _today_str(),
                "ts": _PICKS_CACHE["ts"],
                "data": _PICKS_CACHE["data"],
                "last_err": _PICKS_CACHE["last_err"],
            }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _PICKS_CACHE_FILE)
    except OSError as e:
        print("[picks] cache save error:", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


# 启动时恢复
_load_picks_cache_file()


# ---------------------------------------------------------------------------
# 信号函数（移植自 dynamic_pool_v3.py，输入为 server._klines 产出的行 → DataFrame）
# ---------------------------------------------------------------------------
def rsi(series, w=RSI_W):
    s = series.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / w, min_periods=w, adjust=False).mean()
    al = loss.ewm(alpha=1 / w, min_periods=w, adjust=False).mean()
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _rows_to_df(rows):
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "vol", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # ★稳定排序：分钟K的 date 被 server 截成日期（str(datetime)[:10]），同日多根时间戳相同；
    #   默认快排不稳定会打乱盘中先后顺序，导致 ④收敛→⑤启动 的 bar 级次序判错。
    df = df.dropna(subset=["date"]).sort_values("date", kind="stable").reset_index(drop=True)
    return df if len(df) else None


def daily_signals(df):
    """①周K ②日K —— 主升/初期双路径前置指标（路径在 scan_stock 按周RSI 选定）。

    周K：wk_rsi / wk_ma20 / wk_ma40 / wk_ma20_up(斜率) / wk_ma_ok(周MA20>MA40且向上=总闸)
    日K：cond2_main(close>MA20>MA40) / ma20_up / d_golden(日MA20金叉MA40) /
         d_rsi / d_rsi_band(RSI∈[45,65]) / breakout(放量突破近20日平台)
    ① 周K 按路径：主升=wk_rsi>50 & wk_ma_ok；初期=wk_ma_ok（无RSI要求）
    """
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma40"] = df["close"].rolling(40).mean()
    df["ma5"] = df["close"].rolling(5).mean()     # ★入场过滤闸门用（对齐回测 ENTRY_MAX_ABOVE_MA5）
    df["ma20_up"] = df["ma20"] > df["ma20"].shift(1)
    df["d_golden"] = (df["ma20"] > df["ma40"]) & (df["ma20"].shift(1) <= df["ma40"].shift(1))
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["cond2_main"] = (df["close"] > df["ma20"]) & (df["ma20"] > df["ma40"])
    # 初期②：放量突破近20日平台（前20日收盘最高为平台顶，今日收盘突破+温和放量）
    roll_high = df["close"].shift(1).rolling(20).max()
    df["breakout"] = (df["close"] > roll_high) & (df["vol"] > df["vol_ma20"] * EARLY_BREAKOUT_VOL)
    # 周K 重采样（需≥40周才能算周MA40 → DAILY_BARS≈320）
    wk = df.set_index("date")["close"].resample("W").last()
    wk_rsi = rsi(wk)
    wk_ma20 = wk.rolling(20).mean()
    wk_ma40 = wk.rolling(40).mean()
    wk_ma20_up = wk_ma20 > wk_ma20.shift(1)
    wk_combined = pd.DataFrame({
        "wk_rsi": wk_rsi, "wk_ma20": wk_ma20,
        "wk_ma40": wk_ma40, "wk_ma20_up": wk_ma20_up,
    }).reindex(df["date"], method="ffill").ffill()
    df["wk_rsi"] = wk_combined["wk_rsi"].bfill().values
    df["wk_ma20"] = wk_combined["wk_ma20"].values
    df["wk_ma40"] = wk_combined["wk_ma40"].values
    df["wk_ma20_up"] = wk_combined["wk_ma20_up"].values
    df["wk_ma_ok"] = (df["wk_ma20"] > df["wk_ma40"]) & (df["wk_ma20_up"])
    df["d_rsi"] = rsi(df["close"])
    df["d_rsi_band"] = (df["d_rsi"] >= 45) & (df["d_rsi"] <= 65)
    # ② 初期路径：日MA20刚金叉MA40（近 EARLY_GOLDEN_WINDOW 日内）+ 放量突破平台 + RSI∈[45,65]
    df["d_golden_near"] = df["d_golden"].rolling(EARLY_GOLDEN_WINDOW, min_periods=1).max().astype(bool)
    df["cond2_early"] = df["d_golden_near"] & df["breakout"] & df["d_rsi_band"]
    # ① 周K（路径在 scan_stock 选定）：主升=周RSI>50 & 周MA排列向上；初期=仅周MA排列向上
    df["cond1_main"] = (df["wk_rsi"] > 50) & (df["wk_ma_ok"])
    df["cond1_early"] = df["wk_ma_ok"]
    return df


def min60_signals(df):
    """③ 60分 MA20>MA40 状态制 + 金叉后缩量回踩 MA20。

    ★2026-08-05：③b「缩量回踩」限定在 60分金叉(ma20上穿ma40)【之后】才计
    （golden_done=金叉事件累计；从未金叉→不回踩）。双路径共用。
    """
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma40"] = df["close"].rolling(40).mean()
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["golden"] = (df["ma20"] > df["ma40"]) & (df["ma20"].shift(1) <= df["ma40"].shift(1))
    df["golden_done"] = df["golden"].cummax()   # 金叉后恒真（"金叉后回踩"时序闸门）
    df["ma_up"] = df["ma20"] > df["ma40"]
    dev = df["close"] / df["ma20"] - 1
    in_zone = dev.abs() < PULLBACK_DEV
    df["pullback"] = in_zone & (df["vol"] < df["vol_ma20"] * SHRINK_RATIO) & df["golden_done"]
    return df


def min15_signals(df):
    """15分 原料：rsi14 / vol_ma20 / 量比。"""
    df = df.copy()
    df["rsi14"] = rsi(df["close"])
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["vr"] = df["vol"] / df["vol_ma20"]
    return df


def _buy_confirm(g, j):
    """⑤ 低位买点确认（双路径共用·2026-08-05）：15分 RSI(14) ∈ [35,50] 且
    「拐头上行」(末根>前根) 或 「企稳不破前低」(末根≥前根，未继续探底)。
    即 ④收敛区间内的低位买点提示，比「连2根递增+放量启动」更直观；
    上车规则不变：RSI 到达中低位即上车（不再要求放量启动）。
    """
    r = float(g.at[j, "rsi14"])
    if not (C4_BAND[0] <= r <= C4_BAND[1]):
        return False
    if j >= 1:
        prev = float(g.at[j - 1, "rsi14"])
        if r >= prev:                       # 拐头上行 或 企稳不破前低
            return True
    return False


def _entry_ok(close_px, ma5_px):
    """★入场过滤闸门（2026-08-05 晚·对齐回测 backtest_dualpath.py --entry-filter）：
    买点必须贴近 MA5（close ≤ MA5 × ENTRY_MAX_ABOVE_MA5），高于 MA5 视为追高/脱离收敛区，剔除。
    ma5 不可用（NaN）返回 False（对齐回测 NaN 跳过该信号）。"""
    if ma5_px is None or (isinstance(ma5_px, float) and pd.isna(ma5_px)):
        return False
    try:
        return float(close_px) <= float(ma5_px) * ENTRY_MAX_ABOVE_MA5
    except (TypeError, ValueError):
        return False


def min15_daily_raw(m15s):
    """按【日】返回 15分原始信号三元组 (④收敛, 同天⑤(低位买点确认), 全天⑤签名)。

    ★序贯状态机专用：不在此合成⑤，把 ④/买点确认 的原始事实交给状态机维护跨日次序。
        c4  : 当日出现收敛段（RSI 在带内 + 缩量，≥C4_MIN_BARS 根）
        sdc : same-day c5，收敛段最后一根【之后】出现低位买点确认（bar 级次序）
        la  : launch anywhere，全天任意位置出现低位买点确认（跨日⑤用）
    ⑤ 定义见 _buy_confirm（RSI35-50 拐头/企稳），不再要求放量启动。
    """
    out = {}
    for d, g in m15s.groupby("date"):
        g = g.reset_index(drop=True)
        n = len(g)
        c4 = False
        sdc = False
        la = False
        if n >= C4_MIN_BARS:
            seg = g.iloc[max(0, n - C4_TAIL_BARS):] if C4_TAIL_BARS > 0 else g
            conv = seg[(seg["rsi14"] >= C4_BAND[0]) & (seg["rsi14"] <= C4_BAND[1]) & (seg["vr"] < C4_SHRINK)]
            if len(conv) >= C4_MIN_BARS:
                c4 = True
                last_i = int(conv.index[-1])
                # 同天⑤：收敛段最后一根之后出现低位买点确认
                for j in range(last_i + 1, n):
                    if _buy_confirm(g, j):
                        sdc = True
                        break
                # 全天⑤：任意位置低位买点确认
                for j in range(n):
                    if _buy_confirm(g, j):
                        la = True
                        break
        out[d] = (c4, sdc, la)
    return out


def min15_seq8_events(m15s):
    """返回每个交易日的④/⑤事件时间，允许④与⑤同日但必须先后发生。"""
    out = {}
    for d, g in m15s.groupby("date"):
        g = g.reset_index(drop=True)
        n = len(g)
        c4 = c5 = False
        c4_time = c5_time = None
        if n >= C4_MIN_BARS + C5_RUN:
            seg = g.iloc[max(0, n - C4_TAIL_BARS):]
            conv = seg[(seg["rsi14"] >= C4_BAND[0]) & (seg["rsi14"] <= C4_BAND[1]) & (seg["vr"] < C4_SHRINK)]
            if len(conv) >= C4_MIN_BARS:
                c4 = True
                last_i = int(conv.index[-1])
                c4_time = g.at[last_i, "date"]
                # ★2026-08-05：⑤ 改用 _buy_confirm（RSI35-50 拐头/企稳，低位买点确认）
                for j in range(last_i + 1, n):
                    if _buy_confirm(g, j):
                        c5 = True
                        c5_time = g.at[j, "date"]
                        break
        out[d] = {"c4": c4, "c5": c5, "c4_time": c4_time, "c5_time": c5_time}
    return out


def _atr14(df):
    """15 分 K 的 ATR(14)（优先用 high/low，缺则退化为 close 差分）。"""
    close = df["close"].astype(float)
    if "high" in df.columns and "low" in df.columns:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([(high - low),
                        (high - prev_close).abs(),
                        (low - prev_close).abs()], axis=1).max(axis=1)
    else:
        tr = close.diff().abs()
    atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    return float(atr.iloc[-1]) if len(atr) else 0.0


def min15_alert_signals(m15s):
    """盘中预警用（设计文档 §七.1）：④收敛后 ⑤启动，但不要求落在最后 3 根
    （v1 放宽，盘中命中即弹）。返回该日触发信息：P(触发根close)/时间/ATR15。"""
    out = {}
    for d, g in m15s.groupby("date"):
        g = g.reset_index(drop=True)
        n = len(g)
        c4 = c5 = False
        p5_close = p5_time = atr15 = None
        if n >= C4_MIN_BARS + C5_RUN:
            seg = g.iloc[max(0, n - C4_TAIL_BARS):]
            conv = seg[(seg["rsi14"] >= C4_BAND[0]) & (seg["rsi14"] <= C4_BAND[1]) & (seg["vr"] < C4_SHRINK)]
            if len(conv) >= C4_MIN_BARS:
                c4 = True
                last_i = int(conv.index[-1])
                # ★2026-08-05：⑤ 改用 _buy_confirm（RSI35-50 拐头/企稳，低位买点确认）
                for j in range(last_i + 1, n):
                    if _buy_confirm(g, j):
                        c5 = True
                        p5_close = float(g.at[j, "close"])
                        p5_time = g.at[j, "date"]
                        atr15 = _atr14(g)
                        break
        out[d] = (c4, c5, p5_close, p5_time, atr15)
    return out


def _tier(c1, c2, c3a, c3b, c4, c5, armed):
    """新观察池分级（2026-08-05 共识 + 2026-08-06 收紧）：
    仅注意力分级，不参与上车判定、不参与展示门槛。
    T1=①②维持+已就绪(c4_armed，置顶) / T2=①②维持未就绪且⑤未触发 /
    T3=⑤触发但未就绪(前置不满足→隐藏) 或 ①②破+事件命中(隐藏) / T4=其余。
    注意：c5 不能单独升级 T1——置顶必须有前置条件（c4_armed 就绪）满足，
    「⑤触发但④从未就绪」的票归 T3 进不显示区（2026-08-06 用户纠偏）。
    展示层(T1/T2进观察池)额外要求 c3a=true（③a结构性不可脱钩，详见分流逻辑）。
    """
    if c1 and c2 and armed:
        return 1
    if c1 and c2 and not c5:
        return 2
    if c5 or ((not c1 or not c2) and (c3b or c4 or c5)):
        return 3
    return 4


# ---------------------------------------------------------------------------
# 单票扫描
# ---------------------------------------------------------------------------
def scan_stock(secid, name=None):
    """★序贯状态机扫描（逐行对齐回测 dynamic_pool_v3.run_one 的 seq_window 分支）。

    规则（用户 2026-08-03 拍板，SOP 补充节 §3/§4）：
        ①②③a 基座全程必须成立（任一破 → 本次④作废清零）
          → ④ 出现那天须 ①②③a③b 全中 → 武装，记洗盘段末日
          → 其后 SEQ_WINDOW(8) 个交易日内出现启动签名 → ⑤ 响 → 上车
          → ⑤ 兑现后立即卸载武装（一次洗盘只兑现一次机会）
    ③ 拆两半：
        ③a = 60分 MA20>MA40（状态型，等待期天天守）
        ③b = 缩量回踩（事件型，只在 ④ 那天查一次）
      理由：③b「缩量回落」与 ⑤「放量拉升」语义直接打架，在⑤那天并查会系统性
            误杀「洗完干脆利落走人」那批最好的票。
    上车口径：full = c1 and c2 and c3a and c5（不含 ③b，不含 ④）

    数据缺失/样本不足返回 None。
    """
    try:
        with server._TDX_LOCK:
            # ★2026-08-05：双路径需周MA40排列(≈40周≈280交易日)，日K必须拉满 DAILY_BARS
            #   旧 120 根会令 wk_ma_ok 长期 NaN→cond1_early 恒假，初期①永远不亮。
            d_rows = server._klines(secid, 101, DAILY_BARS)
            m60_rows = server._klines(secid, 60, 240)
            # 15分需覆盖 SEQ_SCAN_DAYS 个交易日（约16根/日）供状态机回溯
            m15_rows = server._klines(secid, 15, SEQ_SCAN_DAYS * 16 + 80)
    except Exception as e:
        print(f"[picks] klines err {secid}: {e}")
        return None
    df = _rows_to_df(d_rows)
    if df is None or len(df) < 60:
        return None
    df = daily_signals(df)
    last = df.iloc[-1]

    # --- 60分 预聚合到日：ma_up 取当日末根（收盘态）/ pullback 取当日任一根 ---
    #     缩量位置修正：作者的「缩量回踩」指回踩【过程】中缩量，非收盘那一刻缩量。
    m60 = _rows_to_df(m60_rows)
    m60_daily = None
    m60_pb = {}
    if m60 is not None and len(m60) > 60:
        m60 = min60_signals(m60)
        m60_daily = m60.groupby("date").last()
        m60_pb = m60.groupby("date")["pullback"].max().to_dict()

    # --- 15分 预聚合到日：(④收敛, 同天⑤, 全天启动签名) ---
    m15 = _rows_to_df(m15_rows)
    m15s = None
    m15_raw = {}
    if m15 is not None and len(m15) > 20:
        m15s = min15_signals(m15)
        m15_raw = min15_daily_raw(m15s)

    # ===================== 序贯状态机主循环 =====================
    dates = df["date"].tolist()
    n = len(dates)
    start = max(0, n - SEQ_SCAN_DAYS)

    armed = False             # ★c4_armed 锁存：④&③b 同天武装；破位/窗口到期才清（①②暂跌不清）
    wash_end_i = None         # 已武装的④段最后一个交易日索引
    fired_i = None            # ⑤ 最近一次触发的日索引
    fired_date = None
    last_c4_date = None       # ★C5 复盘：最近一次④武装日，永不清
    hidden = False            # ★C3 ①②暂跌隐藏标记（others）
    seq_stat = {"arm": 0, "same": 0, "cross": 0, "expire": 0, "break": 0, "b_trig": 0}
    cur = {"c1": False, "c2": False, "c3a": False, "c3b": False,
           "c4": False, "c5": False}
    cur_path = "none"       # 双路径分类（主升/初期/不过闸）

    for i in range(start, n):
        T = dates[i]
        r = df.iloc[i]
        # ★双路径分类（2026-08-05 共识）：先卡周MA总闸(wk_ma_ok)，再按周RSI>50 分主升/初期
        wk_ma_ok_i = bool(r["wk_ma_ok"])
        wk_rsi_i = float(r["wk_rsi"]) if pd.notna(r["wk_rsi"]) else 0.0
        if not wk_ma_ok_i:
            path_i = "none"          # 周MA向下：不过闸，无买点（主升/初期都不进）
            c1 = c2 = False
        elif wk_rsi_i > 50:
            path_i = "main"          # 主升：①周RSI>50&周MA排列；②close>MA20>MA40
            c1 = bool(r["cond1_main"])
            c2 = bool(r["cond2_main"])
        else:
            path_i = "early"         # 上涨初期：①仅周MA排列(无RSI要求)；②刚金叉+突破+RSI区间
            c1 = bool(r["cond1_early"])
            c2 = bool(r["cond2_early"])

        # ③ 拆两半
        c3a = c3b = False
        if m60_daily is not None and T in m60_daily.index:
            row60 = m60_daily.loc[T]
            v20 = row60.get("ma20")
            v40 = row60.get("ma40")
            if pd.notna(v20) and pd.notna(v40):
                c3a = bool(v20 > v40)
            c3b = bool(m60_pb.get(T, False))

        c4, sdc, la = m15_raw.get(T, (False, False, False))
        base_ok = c1 and c2 and c3a          # 基座（结构类，全程必须）

        # ★C2 优先判③a：③a 破位 → 直接 void，清 c4_armed（C1）
        if not c3a:
            if armed:
                seq_stat["break"] += 1
            armed = False
            wash_end_i = None
            hidden = False
        elif base_ok:
            # ①②+③a 全守：正常推进武装/窗口
            if c4 and c3b:
                if not armed:
                    seq_stat["arm"] += 1
                    seq_stat["b_trig"] += 1   # ★④当天③b也触发（关键环计数）
                armed = True
                wash_end_i = i
                last_c4_date = dates[i].strftime("%Y-%m-%d")   # ★C5 永存
            elif c4 and armed:
                wash_end_i = i               # ④延续（已武装期内），刷新段末
            elif armed and wash_end_i is not None and (i - wash_end_i) > SEQ_WINDOW:
                armed = False                # 等待超时 → 作废，回去等下一次④
                wash_end_i = None
                seq_stat["expire"] += 1
            hidden = False
        else:
            # ★C3 ①②暂跌（③a 仍 true）：不清 armed（降权重至不可见），仅标记隐藏
            hidden = True

        c5 = False
        # ★C4 ⑤ 必须 base_ok（①②+③a）+ 已武装（c4_armed）才上车
        if armed and wash_end_i is not None and base_ok:
            if c4 and sdc:
                c5 = True                    # 同天：bar级次序（收敛段最后一根之后启动）
                seq_stat["same"] += 1
            elif (not c4) and (i - wash_end_i) <= SEQ_WINDOW and la:
                c5 = True                    # 跨日：④刚结束后的窗口内出现启动
                seq_stat["cross"] += 1
            if c5:
                # 一次洗盘只兑现一次上车机会，⑤兑现后立即卸载武装
                armed = False
                wash_end_i = None

        if c5:
            fired_i = i
            fired_date = T

        if i == n - 1:
            cur = {"c1": c1, "c2": c2, "c3a": c3a, "c3b": c3b,
                   "c4": c4, "c5": c5}
            cur_path = path_i

    # ===================== 收尾：当前阶段 =====================
    armed_now = bool(armed and wash_end_i is not None)
    c4_date = dates[wash_end_i].strftime("%Y-%m-%d") if armed_now else None
    days_waited = (n - 1 - wash_end_i) if armed_now else None
    days_left = (SEQ_WINDOW - days_waited) if armed_now else None
    entry = bool(fired_i == n - 1)           # ⑤ 落在最新交易日 = 今日上车信号

    # ★入场过滤闸门（对齐回测 --entry-filter）：⑤ 信号日 close ≤ MA5 才视为有效上车；
    #   高于 MA5 视为追高/脱离收敛区，剔除（c5 仍记 True，但今天不上车，等回踩至 MA5 附近）。
    entry_ok = True
    entry_ma5_ratio = None
    if fired_i is not None:
        close_f = float(df.iloc[fired_i]["close"])
        ma5_f = df.iloc[fired_i]["ma5"]
        ma5_f = float(ma5_f) if pd.notna(ma5_f) else None
        if ma5_f is not None:
            entry_ok = _entry_ok(close_f, ma5_f)
            entry_ma5_ratio = round((close_f / ma5_f - 1) * 100, 2)
        else:
            entry_ok = False                 # MA5 不可用 → 剔除（对齐回测 NaN 跳过）
    if entry:
        entry = entry and entry_ok           # 今日上车信号须满足贴近 MA5

    if entry:
        stage = "triggered"
    elif armed_now:
        stage = "waiting"
    else:
        stage = "idle"

    # ★新 tier：T1=①②维持+已武装(置顶) / T2=①②维持未武装 / others(①②破+事件命中)隐藏
    tier = _tier(cur["c1"], cur["c2"], cur["c3a"], cur["c3b"], cur["c4"], cur["c5"], armed)

    # --- 盘中预警（relaxed：④⑤ 盘中命中即弹，不要求落尾盘）---
    alert = None
    if m15s is not None:
        ad = min15_alert_signals(m15s)
        if ad:
            ak = list(ad.keys())[-1]
            ac4, ac5, p5c, p5t, atr15 = ad[ak]
            if ac5:
                ma20d = float(last["ma20"]) if pd.notna(last["ma20"]) else None
                rev = False
                if len(m15s) >= 3:
                    tail = m15s.iloc[-3:]
                    rev = (float(tail.iloc[-1]["vr"]) > 1.2) and \
                          (float(tail.iloc[-1]["rsi14"]) < float(tail.iloc[-2]["rsi14"]))
                tail_ok = (cur["c1"] and cur["c2"]) and (not rev) and _entry_ok(
                    float(last["close"]), float(last["ma5"]) if pd.notna(last["ma5"]) else None)
                alert = {
                    "c4": ac4, "c5": ac5,
                    "p5_close": p5c,
                    "entry_ok": bool(tail_ok),
                    "entry_ma5_ratio": (round((float(last["close"]) / float(last["ma5"]) - 1) * 100, 2)
                                         if pd.notna(last["ma5"]) and float(last["ma5"]) else None),
                    # 分钟K的时间戳被 server 截成日期，盘中时分不可得 → 不再输出假的 "00:00"
                    "p5_time": None,
                    "atr15": round(atr15, 3) if atr15 else None,
                    "ma20d": round(ma20d, 2) if ma20d else None,
                    "tail_ok": tail_ok,
                }

    ma20d_last = float(last["ma20"]) if pd.notna(last["ma20"]) else None

    # —— 第一个金叉买点（初期票展示用）：当前上升段（最近一次死叉之后）第一根日MA20金叉MA40 ——
    g_series = ((df["ma20"] > df["ma40"]).fillna(False)).astype(bool)
    golden_cross = g_series & (~g_series.shift(1, fill_value=False).astype(bool))
    death_cross = (~g_series) & g_series.shift(1, fill_value=False).astype(bool)
    golden_idx = [j for j in range(n) if bool(golden_cross.iloc[j])]
    death_idx = [j for j in range(n) if bool(death_cross.iloc[j])]
    ld = max(death_idx) if death_idx else -1
    leg_start = [j for j in golden_idx if j > ld]
    first_golden_date = None
    first_golden_close = None
    if leg_start:
        gj = leg_start[0]
        gd = dates[gj]
        first_golden_date = gd.strftime("%Y-%m-%d") if isinstance(gd, pd.Timestamp) else str(gd)[:10]
        first_golden_close = round(float(df.iloc[gj]["close"]), 2)

    # 现价 / 涨跌幅（日线末两根）
    close_v = float(last["close"]) if pd.notna(last["close"]) else None
    pct_v = None
    if close_v is not None and len(df) >= 2:
        prev_c = float(df.iloc[-2]["close"])
        if pd.notna(prev_c) and prev_c:
            pct_v = round((close_v / prev_c - 1) * 100, 2)
    return {
        "secid": secid,
        "name": name,
        "close": round(close_v, 2) if close_v is not None else None,
        "pct": pct_v,
        "c1": cur["c1"], "c2": cur["c2"],
        "c3": (cur["c3a"] and cur["c3b"]),   # 旧口径，仅供展示
        "c3a": cur["c3a"], "c3b": cur["c3b"],
        "c4": cur["c4"], "c5": cur["c5"],
        # --- 序贯状态机输出 ---
        "stage": stage,                      # idle / waiting / triggered
        "c4_date": c4_date,                  # ④ 武装日（等待期起算，当前武装时有效）
        "last_c4_date": last_c4_date,       # ★C5 复盘：最近一次④武装日，永不清
        "armed": armed,                      # ★c4_armed 当前是否武装（时序闸门）
        "hidden": hidden,                    # ★C3 ①②暂跌隐藏标记（others）
        "days_waited": days_waited,          # 已等待交易日数
        "days_left": days_left,              # 窗口剩余交易日数
        "fired_date": (fired_date.strftime("%Y-%m-%d") if fired_date is not None else None),
        "seq_stat": seq_stat,
        # --- 兼容旧字段 ---
        "seq8_base": (cur["c1"] and cur["c2"] and cur["c3a"]),
        "seq8_qualify": armed_now,           # 武装中（④已确认，等⑤）
        "seq8_trigger": entry,               # 今日⑤响
        "tier": tier,
        "entry": entry,                      # full = c1 and c2 and c3a and c5 and entry_ok(贴MA5)
        "entry_ok": entry_ok,                # ★入场过滤：⑤信号日 close ≤ MA5 才 True
        "entry_ma5_ratio": entry_ma5_ratio,  # 信号日 close/MA5-1（%，正=高于MA5=追高）
        # ★2026-08-05 双路径分类标签：main=主升 / early=上涨初期 / none=周MA向下不过闸
        "trend_type": cur_path,
        # ★2026-08-05 初期票展示：当前上升段第一根日MA20金叉MA40（第一个金叉买点）
        "first_golden_date": first_golden_date,
        "first_golden_close": first_golden_close,
        "ma20d": round(ma20d_last, 2) if ma20d_last else None,
        "alert": alert,
        "wk_rsi": round(float(last["wk_rsi"]), 1) if pd.notna(last["wk_rsi"]) else None,
        "d_rsi": round(float(last["d_rsi"]), 1) if pd.notna(last["d_rsi"]) else None,
    }


def _get_members(bk, limit):
    """取板块成分股（直接调 tdx.board_members，跳过 scan_sector 的逐票评估以省请求）。"""
    try:
        # ★卡死修复：board_members 在派生线程里走 from_best_host() 建连，可能无限挂起。
        # 套 _call_timeout：单次超时就放弃该板块成分股（记日志），不让整轮扫描挂死。
        with server._TDX_LOCK:
            df = server._call_timeout(lambda: server.tdx.board_members(bk, count=limit), 30, default=None, label=f"board_members {bk}")
        if df is None or len(df) == 0:
            return []
        out = []
        for _, r in df.iterrows():
            code = str(r.get("code", "")).strip()
            if not code:
                continue
            mkt = int(r["market"]) if not server._isnan(r.get("market")) else 1
            out.append({"code": code, "market": mkt, "name": str(r.get("name", ""))})
        return out
    except Exception as e:
        print(f"[picks] board_members err {bk}: {e}")
        return []


def _sina_universe(top_n=PICKS_SINA_MAX):
    """★TDX 死亡兜底：用新浪全市场行情节点取 A 股列表，按 |涨跌幅| 取动量前 N 作为扫描宇宙。
    返回 [{"secid","name","market","code","symbol"}]。绕过代理直连（server.NO_PROXY）。
    实现：直接按 changepercent 排序取「涨幅榜(asc=0)+跌幅榜(asc=1)」各若干页，
    合并且按 |涨跌幅| 取前 N（新股/历史不足者会被 scan_stock 自动跳过）。
    说明：本机 TDX（Mac 版通达信）已熔断、东方财富 push2 被环境限流，唯一稳定源是新浪；
    Sina kline 已在 _klines 内作为兜底，这里补上「扫哪些票」的成分股来源。
    """
    URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData")
    out = {}
    try:
        half = (top_n // 2) + 20
        pages = half // 100 + 2
        for asc in (0, 1):   # 0=涨幅最大在前；1=跌幅最大在前（洗盘候选）
            for pg in range(1, pages + 1):
                try:
                    r = requests.get(URL,
                        params={"page": pg, "num": 100, "sort": "changepercent",
                                "asc": asc, "node": "hs_a"},
                        headers=server.SINA_HDR, timeout=20, proxies=server.NO_PROXY)
                    data = r.json() or []
                except Exception as e:
                    print(f"[picks] sina universe err (asc={asc} pg={pg}): {e}")
                    break
                if not data:
                    break
                for x in data:
                    sym = (x.get("symbol") or x.get("code") or "")
                    if len(sym) < 4:
                        continue
                    pre = sym[:2].lower()
                    if pre == "sh":
                        market = 1
                    elif pre == "sz":
                        market = 0
                    else:
                        continue  # 跳过 bj/其它（Sina kline 前缀不匹配）
                    code = sym[2:]
                    try:
                        cp = abs(float(x.get("changepercent") or 0))
                    except Exception:
                        cp = 0.0
                    out[f"{market}.{code}"] = (cp, f"{market}.{code}",
                                              x.get("name") or "", market, code, sym)
                if len(data) < 100:
                    break
    except Exception as e:
        print(f"[picks] sina universe fatal: {e}")
    buf = sorted(out.values(), reverse=True, key=lambda t: t[0])
    print(f"[picks] 新浪动量宇宙：去重 {len(buf)} 只，取 |涨跌幅| 前 {top_n}")
    return [{"secid": t[1], "name": t[2], "market": t[3], "code": t[4], "symbol": t[5]}
            for t in buf[:top_n]]


# ---------------------------------------------------------------------------
# 全量预计算
# ---------------------------------------------------------------------------
def compute_picks():
    """后台线程执行：扫热点板块成分股五条件，结果写入 _PICKS_CACHE。"""
    with _PICKS_LOCK:
        if _PICKS_CACHE["computing"]:
            return None
        _PICKS_CACHE["computing"] = True
        _PICKS_CACHE["progress"] = {"stage": "板块矩阵", "scanned": 0, "total": 0, "hit": 0}
    t0 = time.time()
    try:
        sectors = server.sector_matrix()
        if not sectors:
            with _PICKS_LOCK:
                _PICKS_CACHE["last_err"] = "sector_matrix 返回空（板块数据暂不可达）"
            return None
        hot = [b for b in sectors if b.get("state") in ("主推", "观察")]
        # 主推优先，取最前 N 个
        hot = sorted(hot, key=lambda b: 0 if b.get("state") == "主推" else 1)[:PICKS_MAX_SECTORS]
        alert_on = load_config().get("intradaily_alert_enabled", True)

        res = {
            "ts": int(time.time()),
            "params": {
                "max_sectors": PICKS_MAX_SECTORS,
                "max_per_sector": PICKS_MAX_PER_SECTOR,
                "max_stocks": PICKS_MAX_STOCKS,
            },
            "hot_sectors": [],
            "full_match": [],
            "pool": [],
            "seq8_candidates": [],
            "seq8_triggers": [],
            "seq8_others": [],                # ★C3 内部标签：①②破+事件命中，前端不展示
            "alerts": [],
            "stats": {},
        }
        # ---- 扫描宇宙：优先 TDX 板块成分股；TDX 死亡则回退新浪全市场动量宇宙 ----
        use_sina_fallback = False
        scan_items = []   # (secid, name, bk, sname)
        sector_members = {}
        for _i, b in enumerate(hot):
            bk = b.get("bk")
            with _PICKS_LOCK:
                _PICKS_CACHE["progress"] = {"stage": f"取成分股 {_i + 1}/{len(hot)}",
                                            "scanned": 0, "total": 0, "hit": 0}
            members = _get_members(bk, PICKS_MAX_PER_SECTOR)
            sector_members[bk] = members
        total_members = sum(len(v) for v in sector_members.values())
        if total_members == 0:
            # TDX 板块成分股不可用 → 新浪全市场动量前 N 作为扫描宇宙
            use_sina_fallback = True
            uni = _sina_universe(min(PICKS_SINA_MAX, PICKS_MAX_STOCKS))
            for m in uni:
                scan_items.append((m["secid"], m["name"], "__sina__", "动量全市场·新浪兜底"))
            print(f"[picks] ★TDX 成分股不可用，回退新浪动量宇宙 {len(scan_items)} 只")
        else:
            for b in hot:
                bk = b.get("bk")
                sname = b.get("name")
                for m in sector_members[bk]:
                    scan_items.append((f"{m['market']}.{m['code']}", m.get("name"), bk, sname))

        # rec 初始化（用于展示）
        recs = {}
        if use_sina_fallback:
            rec = {"bk": "__sina__", "name": "动量全市场·新浪兜底", "label": "主推",
                   "members": len(scan_items), "full": [], "pool": []}
            recs["__sina__"] = rec
            res["hot_sectors"].append(rec)
        else:
            for b in hot:
                bk = b.get("bk")
                recs[bk] = {"bk": bk, "name": b.get("name"), "label": b.get("state"),
                            "members": len(sector_members[bk]), "full": [], "pool": []}
                res["hot_sectors"].append(recs[bk])

        alerts_map = {}
        scanned = 0
        print(f"[picks] 热点板块 {len(hot)} 个，扫描宇宙 {len(scan_items)} 只（上限 {PICKS_MAX_STOCKS}）")
        for secid, name, bk, sname in scan_items:
            if scanned >= PICKS_MAX_STOCKS:
                break
            scanned += 1
            if scanned % 10 == 0 or scanned == 1:
                with _PICKS_LOCK:
                    _PICKS_CACHE["progress"] = {
                        "stage": "扫描个股", "scanned": scanned,
                        "total": min(len(scan_items), PICKS_MAX_STOCKS),
                        "hit": len(res["seq8_triggers"]) + len(res["seq8_candidates"])}
            if scanned % 40 == 0:
                print(f"[picks] 进度 {scanned}/{PICKS_MAX_STOCKS} …（命中⑤ {len(res['seq8_triggers'])} / ④等待 {len(res['seq8_candidates'])}）", flush=True)
            try:
                r = scan_stock(secid, name)
            except Exception as e:
                print(f"[picks] scan err {secid}: {e}")
                r = None
            if not r:
                continue
            r["sector"] = sname
            r["bk"] = bk
            # 序贯状态机分流：waiting=④已武装等⑤ / triggered=今日⑤响
            if r["stage"] == "waiting":
                res["seq8_candidates"].append(r)
            elif r["stage"] == "triggered":
                res["seq8_triggers"].append(r)
            if r["entry"]:
                recs[bk]["full"].append(r)
                res["full_match"].append(r)
            elif r["c3a"] and r["tier"] <= 2:
                # ★T1/T2 进观察池（展示门槛要求 ③a=true；③a=false 结构性脱钩不展示，见下）
                recs[bk]["pool"].append(r)
                res["pool"].append(r)
            elif r.get("hidden") or (not r["c3a"]):
                # ★不展示栏：①②破+事件命中(hidden) 或 ③a=false（用户 2026-08-05 选 A 模型，
                # 但 c3a=false 不放 T2，归入不显示栏）。entry(⑤触发)必 c3a=true，不会落入此处。
                res["seq8_others"].append(r)
            # 盘中预警（带价挂单）：④⑤ 盘中命中即记录，去重(同票覆盖)
            if alert_on and r.get("alert") and r["alert"].get("c5") and r["alert"].get("entry_ok"):
                a = r["alert"]
                P = a["p5_close"]; atr15 = a["atr15"]; ma20d = a["ma20d"]
                if P and atr15 is not None and ma20d:
                    T = max(atr15 * K_ATR, PCT_MIN)
                    ph = P + T * 0.3
                    pl = max(P - T, ma20d * 0.995)
                    alerts_map[secid] = {
                        "secid": secid, "name": r.get("name"),
                        "sector": sname, "bk": bk,
                        "time": a["p5_time"], "P": round(P, 2),
                        "price_low": round(pl, 2), "price_high": round(ph, 2),
                        "atr15": a["atr15"], "ma20d": a["ma20d"],
                        "tail_ok": a.get("tail_ok"),
                        "status": ("明天可继续关注" if a.get("tail_ok") else "今日失效"),
                    }

        res["alerts"] = list(alerts_map.values())
        res["_date"] = _today_str()
        res["stats"] = {
            "scanned": scanned,
            "hot_sectors": len(res["hot_sectors"]),
            "full_match": len(res["full_match"]),
            "pool": len(res["pool"]),
            "waiting": len(res["seq8_candidates"]),   # ④已武装，窗口内等⑤
            "triggered": len(res["seq8_triggers"]),   # 今日⑤响
            "alerts": len(res["alerts"]),
            "seq_window": SEQ_WINDOW,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        with _PICKS_LOCK:
            _PICKS_CACHE["data"] = res
            _PICKS_CACHE["ts"] = res["ts"]
            _PICKS_CACHE["last_err"] = None
        _save_picks_cache_file()
        print(f"[picks] 完成：扫描 {scanned} 只，⑤触发 {len(res['seq8_triggers'])}，"
              f"④等待中 {len(res['seq8_candidates'])}，观察池 {len(res['pool'])}，"
              f"用时 {res['stats']['elapsed_sec']}s")
        # 选股完成后立即刷新生命周期漏斗（观察池命中字段、阶段推进）
        try:
            import lifecycle
            lifecycle.trigger_refresh()
        except Exception as e:
            print("[picks] lifecycle refresh err:", e)
        return res
    except Exception as e:
        print(f"[picks] compute err:", e)
        with _PICKS_LOCK:
            _PICKS_CACHE["last_err"] = str(e)
        return None
    finally:
        with _PICKS_LOCK:
            _PICKS_CACHE["computing"] = False
            _PICKS_CACHE["progress"] = {"stage": "完成", "scanned": 0, "total": 0, "hit": 0}


def _bg_run():
    # 卡死诊断看门狗：若扫描线程长时间不退出，dump 全部线程栈到 stderr（服务日志），
    # 便于定位挂死点（无需 py-spy）。仅诊断用，不影响正常流程。
    def _watchdog():
        for _i in range(25):
            time.sleep(60)
            with _PICKS_LOCK:
                if not _PICKS_CACHE["computing"]:
                    return
            print(f"[watchdog] 扫描已卡 {_i + 1} 分钟仍未结束，dump 全部线程栈：", flush=True)
            faulthandler.dump_traceback(all_threads=True)
    _w = threading.Thread(target=_watchdog, daemon=True)
    _w.start()
    compute_picks()


def trigger_refresh():
    """触发后台重算（若已在算则忽略）。返回是否新启动。"""
    with _PICKS_LOCK:
        if _PICKS_CACHE["computing"]:
            return False
    t = threading.Thread(target=_bg_run, daemon=True)
    t.start()
    return True


def get_cache():
    with _PICKS_LOCK:
        data = _PICKS_CACHE["data"]
    # 跨天自动失效：若缓存里记录的 date 不是今天，清空并触发后台刷新
    if data and isinstance(data, dict) and data.get("_date") != _today_str():
        print("[picks] 缓存跨天失效，清空并触发刷新")
        with _PICKS_LOCK:
            _PICKS_CACHE["data"] = None
            _PICKS_CACHE["ts"] = 0
        try:
            os.remove(_PICKS_CACHE_FILE)
        except OSError:
            pass
        trigger_refresh()
        return None
    return data


def cache_ts():
    with _PICKS_LOCK:
        return _PICKS_CACHE["ts"]


def is_computing():
    with _PICKS_LOCK:
        return _PICKS_CACHE["computing"]


def progress():
    """当前扫描进度（前台可显示「扫描个股 120/480」而不是干等）。"""
    with _PICKS_LOCK:
        return dict(_PICKS_CACHE.get("progress") or {})


def last_err():
    with _PICKS_LOCK:
        return _PICKS_CACHE["last_err"]


if __name__ == "__main__":
    # 直接运行 = 前台跑一轮（调试用）
    r = compute_picks()
    print(json.dumps(r["stats"] if r else {"err": "none"}, ensure_ascii=False, indent=2))
