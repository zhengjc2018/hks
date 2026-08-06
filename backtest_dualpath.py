# -*- coding: utf-8 -*-
"""
双路径(主升/初期)框架历史回测 —— 直连本地通达信(easy_tdx)
================================================================
逐字移植 picks.py(2026-08-05) 的信号逻辑，避免与线上引擎分化。
买点：序贯状态机 ⑤ 响应 = 上车信号（full = c1&c2&c3a&c5）
卖点：SOP 卖出铁律(MA5 相对止损 甲乙情形) + 清仓四条件
      -8% 硬砍 标记为「风控假设」(非框架规则贡献)，单独统计

关键决策(用户 2026-08-05 拍板)：
  [Part1] preview 信号明细切到 picks 双路径口径（前端任务，本脚本只验证框架本身）
  [Part2] 初期票第一个金叉买点（前端展示，本脚本按双路径统一口径回测）
  [Part3] 放量阈值沿用代码常量(SHRINK_RATIO=0.8 / EARLY_BREAKOUT_VOL=1.0 / 突破>1.5显著)，
          不另立 SOP，本脚本严格照搬常量。
  [Part4] 本文件 = 回测规则，用户自跑。

铁律(回测防自欺，详见 ashare-strategy-backtest SKILL)：
  1) 防未来函数：第 i 根收盘确认信号，并以第 i 根收盘价作为回测入场价
  2) 指标切片只到当前：所有指标因果(rolling/ewm/resample-last)，无前视
  3) 不输出逐笔顺序复利（多票同期持仓会重叠）
  4) 区分「框架规则」与「回测假设」(-8%硬砍/手续费/冷却单独标注)
  5) 止损不算规则贡献：按离场原因分层时，hard_stop 单独剔除

运行(venv default 已装 easy_tdx/pandas/numpy)：
  cd C:/Users/natsu/WorkBuddy/2026-07-20-13-17-12/apanel
  python backtest_dualpath.py                 # 跑全池 + MA20两日破位 A/B
  python backtest_dualpath.py --baseline 600519   # 先只跑一只验收(茅台)
  python backtest_dualpath.py --ma20           # 仅开启 MA20 两日破位变体
  python backtest_dualpath.py --entry-filter   # 入场过滤：剔除高开追入(case A)，只买≤MA5
  python backtest_dualpath.py --entry-filter --exit-dull   # 入场过滤 + 离场钝化(MA5有效跌破才止损)
输出：
  backtest_report/dualpath_trades.csv
  backtest_report/dualpath_report.md
  backtest_report/dualpath_year.svg / dualpath_pnl.svg
================================================================
"""
from __future__ import annotations
import os
import sys
import argparse
import numpy as np
import pandas as pd

# 复用项目线程安全封装（easy_tdx 直连），不要自己 new client
from tdx_source import kline, available, Period, Adjust

# ===================== 常量（与 picks.py 2026-08-05 逐字一致）=====================
RSI_W = 14
C4_TAIL_BARS = 0        # ④ 0=全天任意15分K内找收敛段（对齐回测放宽）
C4_BAND = (35, 50)      # ④ 收敛段 RSI 区间
C4_MIN_BARS = 3         # ④ 收敛段最少根数
C4_SHRINK = 0.8         # ④ 缩量阈值（量比 <）
C5_RUN = 2              # ⑤ 连续递增根数（现仅注释用）
C5_EXPAND = 1.0         # ⑤ 放量阈值（现仅注释用）
C5_TAIL_BARS = 0        # ⑤ 0=尾盘闸已去掉
SEQ_WINDOW = 8          # ④ 武装后最多等 N 个交易日等 ⑤
SEQ_SCAN_DAYS = 25      # 状态机回溯交易日数（=25）
SHRINK_RATIO = 0.8      # ③④⑤ 统一缩量比例
PULLBACK_DEV = 0.02     # ③ 回踩偏离绝对值上限
EARLY_GOLDEN_WINDOW = 20   # 初期②：日MA20金叉MA40 须在近 N 日内
EARLY_BREAKOUT_VOL = 1.0   # 初期②：放量突破平台（温和放大，量比>1.0）
DAILY_BARS = 320        # ① 周MA40 需≈40周≈280交易日（回测另按窗口拉满）

# [入场过滤] 只在价格仍贴着 5 日线(收敛区)时上车，剔除高位追入
# 信号日收盘 ≤ 当日 MA5 * 该值 才成交；=1.0 即严格禁止高于 MA5（与实盘 picks.py 同口径）
ENTRY_MAX_ABOVE_MA5 = 1.0


# ===================== 指标函数（逐字移植 picks.py）=====================
def rsi(series, w=RSI_W):
    s = series.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / w, min_periods=w, adjust=False).mean()
    al = loss.ewm(alpha=1 / w, min_periods=w, adjust=False).mean()
    rs = ag / al
    return 100 - 100 / (1 + rs)


def daily_signals(df):
    """①周K ②日K —— 主升/初期双路径前置（移植 picks.daily_signals）。
    df['date'] 须为 datetime（用于周线 resample）。"""
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma40"] = df["close"].rolling(40).mean()
    df["ma20_up"] = df["ma20"] > df["ma20"].shift(1)
    df["d_golden"] = (df["ma20"] > df["ma40"]) & (df["ma20"].shift(1) <= df["ma40"].shift(1))
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["cond2_main"] = (df["close"] > df["ma20"]) & (df["ma20"] > df["ma40"])
    roll_high = df["close"].shift(1).rolling(20).max()
    df["breakout"] = (df["close"] > roll_high) & (df["vol"] > df["vol_ma20"] * EARLY_BREAKOUT_VOL)
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
    df["d_golden_near"] = df["d_golden"].rolling(EARLY_GOLDEN_WINDOW, min_periods=1).max().astype(bool)
    df["cond2_early"] = df["d_golden_near"] & df["breakout"] & df["d_rsi_band"]
    df["cond1_main"] = (df["wk_rsi"] > 50) & (df["wk_ma_ok"])
    df["cond1_early"] = df["wk_ma_ok"]
    df["ma5"] = df["close"].rolling(5).mean()
    # [原版卖点数据] 上影占比 + 放量滞涨(顶部预警 rule1 用)
    # up_shadow = (最高价 - max(开,收)) / (最高-最低)，衡量上影线长度占比
    hl = (df["high"] - df["low"]).replace(0, np.nan)
    df["up_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / hl
    # 放量滞涨(rule1)：量>20日均量1.5倍(放量) 且 上影>0.5(长上影=冲高回落)。
    # 长上影本身已隐含"冲高回落"，不再额外约束实体涨幅(否则把大阳线顶部排除，导致极少触发)。
    df["stagnation_top"] = (df["vol"] > df["vol_ma20"] * 1.5) & (df["up_shadow"] > 0.5)
    return df


def min60_signals(df):
    """③ 60分 MA20>MA40 状态制 + 金叉后缩量回踩 MA20（移植 picks.min60_signals）。"""
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma40"] = df["close"].rolling(40).mean()
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["golden"] = (df["ma20"] > df["ma40"]) & (df["ma20"].shift(1) <= df["ma40"].shift(1))
    df["golden_done"] = df["golden"].cummax()
    df["ma_up"] = df["ma20"] > df["ma40"]
    dev = df["close"] / df["ma20"] - 1
    in_zone = dev.abs() < PULLBACK_DEV
    df["pullback"] = in_zone & (df["vol"] < df["vol_ma20"] * SHRINK_RATIO) & df["golden_done"]
    return df


def min15_signals(df):
    """15分 原料（移植 picks.min15_signals）。"""
    df = df.copy()
    df["rsi14"] = rsi(df["close"])
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["vr"] = df["vol"] / df["vol_ma20"]
    return df


def _buy_confirm(g, j):
    """⑤ 低位买点确认（移植 picks._buy_confirm）：15分 RSI∈[35,50] 拐头/企稳。"""
    r = float(g.at[j, "rsi14"])
    if not (C4_BAND[0] <= r <= C4_BAND[1]):
        return False
    if j >= 1:
        prev = float(g.at[j - 1, "rsi14"])
        if r >= prev:
            return True
    return False


def min15_daily_raw(m15s):
    """按【日】返回 15分三元组 (④收敛, 同天⑤, 全天⑤)（移植 picks.min15_daily_raw）。
    m15s['date'] 为字符串 YYYY-MM-DD。"""
    out = {}
    for d, g in m15s.groupby("date"):
        g = g.reset_index(drop=True)
        n = len(g)
        c4 = sdc = la = False
        if n >= C4_MIN_BARS:
            seg = g.iloc[max(0, n - C4_TAIL_BARS):] if C4_TAIL_BARS > 0 else g
            conv = seg[(seg["rsi14"] >= C4_BAND[0]) & (seg["rsi14"] <= C4_BAND[1]) & (seg["vr"] < C4_SHRINK)]
            if len(conv) >= C4_MIN_BARS:
                c4 = True
                last_i = int(conv.index[-1])
                for j in range(last_i + 1, n):
                    if _buy_confirm(g, j):
                        sdc = True
                        break
                for j in range(n):
                    if _buy_confirm(g, j):
                        la = True
                        break
        out[d] = (c4, sdc, la)
    return out


# ===================== 序贯状态机（逐字移植 picks.scan_stock 主循环）=====================
def seq_state_at(df, m60_daily, m60_pb, m15_raw, dates, i):
    """在「第 i 根」处运行状态机（仅用 [i-24, i] 窗口，armed 每次复位 = 复刻线上逐日扫描）。
    返回 (cur_dict, path_i, entry_bool)。"""
    n = i + 1
    start = max(0, n - SEQ_SCAN_DAYS)
    armed = False
    wash_end_i = None
    fired_i = None
    cur = {"c1": False, "c2": False, "c3a": False, "c3b": False, "c4": False, "c5": False}
    cur_path = "none"
    for j in range(start, n):
        T = dates[j]
        r = df.iloc[j]
        key = T.strftime("%Y-%m-%d") if isinstance(T, (pd.Timestamp,)) else str(T)[:10]
        wk_ma_ok_j = bool(r["wk_ma_ok"])
        wk_rsi_j = float(r["wk_rsi"]) if pd.notna(r["wk_rsi"]) else 0.0
        if not wk_ma_ok_j:
            path_j = "none"
            c1 = c2 = False
        elif wk_rsi_j > 50:
            path_j = "main"
            c1 = bool(r["cond1_main"])
            c2 = bool(r["cond2_main"])
        else:
            path_j = "early"
            c1 = bool(r["cond1_early"])
            c2 = bool(r["cond2_early"])
        c3a = c3b = False
        if m60_daily is not None and key in m60_daily.index:
            row60 = m60_daily.loc[key]
            v20 = row60.get("ma20")
            v40 = row60.get("ma40")
            if pd.notna(v20) and pd.notna(v40):
                c3a = bool(v20 > v40)
            c3b = bool(m60_pb.get(key, False))
        c4, sdc, la = m15_raw.get(key, (False, False, False))
        base_ok = c1 and c2 and c3a
        if not c3a:
            armed = False
            wash_end_i = None
        elif base_ok:
            if c4 and c3b:
                armed = True
                wash_end_i = j
            elif c4 and armed:
                wash_end_i = j
            elif armed and wash_end_i is not None and (j - wash_end_i) > SEQ_WINDOW:
                armed = False
                wash_end_i = None
        c5 = False
        if armed and wash_end_i is not None and base_ok:
            if c4 and sdc:
                c5 = True
            elif (not c4) and (j - wash_end_i) <= SEQ_WINDOW and la:
                c5 = True
            if c5:
                armed = False
                wash_end_i = None
        if c5:
            fired_i = j
        if j == i:
            cur = {"c1": c1, "c2": c2, "c3a": c3a, "c3b": c3b, "c4": c4, "c5": c5}
            cur_path = path_j
    entry = bool(fired_i == i)
    return cur, cur_path, entry


# ===================== 数据获取 =====================
def fetch(market, code, period, count):
    df = kline(market, code, period=period, count=count, adjust=Adjust.QFQ)
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    for c in ("open", "high", "low", "close", "vol", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"]  # daily_signals 依赖 'date' 列(周线resample/dates列表)
    return df


def prep_indicators(daily_df, m60_df, m15_df):
    daily = daily_signals(daily_df.copy())
    m60 = None
    m60_daily = None
    m60_pb = {}
    if m60_df is not None and len(m60_df) > 60:
        m60 = min60_signals(m60_df.copy())
        m60["date"] = m60["datetime"].dt.strftime("%Y-%m-%d")
        m60_daily = m60.groupby("date").last()[["ma20", "ma40"]]
        m60_pb = m60.groupby("date")["pullback"].max().to_dict()
    m15_raw = {}
    m15_rsi = {}
    if m15_df is not None and len(m15_df) > 20:
        m15 = min15_signals(m15_df.copy())
        m15["date"] = m15["datetime"].dt.strftime("%Y-%m-%d")
        m15_raw = min15_daily_raw(m15)
        # [原版卖点] 每日 15分 RSI(14) 的 日内 max/min，供 rule1(>75)/rule2(<45) 判定
        for d, g in m15.groupby("date"):
            m15_rsi[d] = (float(g["rsi14"].max()), float(g["rsi14"].min()))
    return daily, m60_daily, m60_pb, m15_raw, m15_rsi


# ===================== 回测主流程 =====================
def run_exit(daily, buy_idx, path, use_ma20, exit_dull=False):
    """从 buy_idx(信号日收盘买入) 起跑卖出逻辑，返回成交记录。"""
    n = len(daily)
    dates = daily["date"].tolist()
    opens = daily["open"].values
    closes = daily["close"].values
    ma5 = daily["ma5"].values
    ma20 = daily["ma20"].values
    d_rsi = daily["d_rsi"].values
    buy_price = float(daily["close"].values[buy_idx])
    buy_date = dates[buy_idx]
    ma5_buy = float(ma5[buy_idx])
    case = "A" if buy_price > ma5_buy else "B"   # 高位买入 / 低位买入
    triple_done = False
    exit_idx = None
    exit_price = None
    reason = None
    hold = 0
    for k in range(buy_idx + 1, n):
        hold += 1
        c = float(closes[k])
        m5 = float(ma5[k])
        m20 = float(ma20[k])
        r = float(d_rsi[k]) if pd.notna(d_rsi[k]) else 50.0
        # [风控假设] -8% 硬砍（横盘期/通用）
        if c <= buy_price * 0.92:
            exit_idx, exit_price, reason = k, c, "hard_stop"
            break
        # [框架] RSI>70 情绪过热
        if r > 70:
            exit_idx, exit_price, reason = k, c, "rsi_overheat"
            break
        # [框架] 持仓满 20 日
        if hold >= 20:
            exit_idx, exit_price, reason = k, c, "time_exit"
            break
        # [框架] MA5 相对止损
        if case == "A":
            if c < m5:
                exit_idx, exit_price, reason = k, c, "ma5_A"
                break
        else:
            if not triple_done and k >= 2:
                # 三重确认（对齐实盘 sell.py:117-121，2026-08-06 修方向 bug）：
                # MA5 向上拐头 + 昨日收在 MA5 下方 + 昨日上涨 + 今日收盘上穿 MA5
                ma5_turn = m5 > float(ma5[k - 1])                    # 实盘: ma5_today > ma5_yest
                y_below = float(closes[k - 1]) <= float(ma5[k - 1])  # 实盘: close_y <= ma5_yest
                y_up = float(closes[k - 1]) > float(closes[k - 2])   # 实盘: close_2 < close_y
                cross_up = c > m5                                      # 实盘: close > ma5_today
                if ma5_turn and y_below and y_up and cross_up:
                    triple_done = True
            if triple_done:
                if exit_dull:
                    # [钝化] 仅"有效跌破"MA5 才止损：连续两日收破，或单日跌幅>1.5%
                    # 过滤单日探针式下影线，避免被噪音反复扫（对应 ma5_B 滞后问题）
                    prev_below = k >= 1 and float(closes[k - 1]) < float(ma5[k - 1])
                    big_drop = k >= 1 and (float(closes[k - 1]) - c) / float(closes[k - 1]) > 0.015
                    if c < m5 and (prev_below or big_drop):
                        exit_idx, exit_price, reason = k, c, "ma5_B"
                        break
                else:
                    if c < m5:
                        exit_idx, exit_price, reason = k, c, "ma5_B"
                        break
        # [A/B 变体] 多头仓 连续两日收破 MA20（趋势失守）
        if use_ma20 and path in ("main", "early"):
            if k >= 1 and c < m20 and float(closes[k - 1]) < float(ma20[k - 1]):
                exit_idx, exit_price, reason = k, c, "ma20_break"
                break
    if exit_idx is None:
        exit_idx = n - 1
        exit_price = float(closes[n - 1])
        reason = "eod"
    pnl = (exit_price - buy_price) / buy_price * 100
    return dict(
        buy_date=buy_date, buy_price=round(buy_price, 3),
        exit_date=dates[exit_idx], exit_price=round(exit_price, 3),
        hold_days=hold, pnl=round(pnl, 2), reason=reason,
        path=path, case=case,
    )


def run_exit_orig(daily, m60_daily, m15_rsi, buy_idx, path):
    """原版卖点(用户截图 4 条) + 分批减仓(3份递进)。

    原版卖点(截图)：
      rule1 = 日K 放量滞涨 + 15分 RSI(14)>75 + 长上影  → 顶部预警，减 1/3 锁利
      rule2 = 60分 跌破 MA20 + 15分 RSI(14)<45         → 趋势破位，再减 1/3
      rule3 = 60分 跌破 MA40                           → 深破，清剩余
      rule4 = 日K 跌破 MA20(跌破最后一根阳线)          → 最后防线，清剩余
    分批：持仓分 3 份，按 rule1→2→3/4 递进减仓，每档减 1 份；
          全程含 -8% 硬砍(风控假设) 与 60 日长持兜底。
    返回聚合 dict(等效整仓收益率，字段同 run_exit，可直接对比) + n_reductions + partials。
    """
    n = len(daily)
    dates = daily["date"].tolist()
    opens = daily["open"].values
    closes = daily["close"].values
    ma5 = daily["ma5"].values
    ma20 = daily["ma20"].values
    stag = daily["stagnation_top"].values
    buy_price = float(daily["close"].values[buy_idx])
    buy_date = dates[buy_idx]
    ma5_buy = float(ma5[buy_idx])
    case = "A" if buy_price > ma5_buy else "B"

    def key_of(d):
        return d.strftime("%Y-%m-%d") if isinstance(d, pd.Timestamp) else str(d)[:10]

    remaining = 3
    parts = []          # (exit_idx, exit_price, reason, frac_in_thirds)
    hold = 0
    for k in range(buy_idx + 1, n):
        hold += 1
        c = float(closes[k])
        dk = key_of(dates[k])
        # 60分 MA（每日最后一笔）
        m60_20 = m60_40 = None
        if m60_daily is not None and dk in m60_daily.index:
            row = m60_daily.loc[dk]
            m60_20 = float(row["ma20"]) if pd.notna(row["ma20"]) else None
            m60_40 = float(row["ma40"]) if pd.notna(row["ma40"]) else None
        rmax, rmin = m15_rsi.get(dk, (50.0, 50.0))
        # 原版 4 条
        r1 = bool(stag[k]) and (rmax > 75)
        r2 = (m60_20 is not None and c < m60_20) and (rmin < 50)
        r3 = (m60_40 is not None and c < m60_40)
        r4 = c < float(ma20[k])
        hard = c <= buy_price * 0.92
        if hard and remaining > 0:
            parts.append((k, c, "hard_stop", remaining)); remaining = 0; break
        if remaining == 3 and r1:
            parts.append((k, c, "rule1_top", 1)); remaining = 2
        elif remaining == 2 and r2:
            parts.append((k, c, "rule2_m60ma20", 1)); remaining = 1
        elif remaining == 1 and (r3 or r4):
            parts.append((k, c, "rule3_m60ma40" if r3 else "rule4_dailyma20", 1)); remaining = 0; break
        if hold >= 60 and remaining > 0:
            parts.append((k, c, "time_exit", remaining)); remaining = 0; break
    if remaining > 0:
        parts.append((n - 1, float(closes[n - 1]), "eod", remaining))
    # 聚合：等效整仓收益率（3 份各自卖出价加权）
    tot_frac = sum(p[3] for p in parts)
    equiv_pnl = sum((p[1] - buy_price) / buy_price * 100 * (p[3] / 3.0) for p in parts)
    w_hold = sum((p[0] - buy_idx) * p[3] for p in parts) / tot_frac if tot_frac else 0.0
    last = parts[-1]
    partials = ";".join(f"{p[3]}/3@{key_of(dates[p[0]])}:{p[2]}" for p in parts)
    return dict(
        buy_date=buy_date, buy_price=round(buy_price, 3),
        exit_date=dates[last[0]], exit_price=round(last[1], 3),
        hold_days=round(w_hold, 1), pnl=round(equiv_pnl, 2), reason=last[2],
        path=path, case=case,         n_reductions=len(parts),
        partials=partials,
    )


def run_exit_60m(daily, m60, m15_rsi, buy_idx, path):
    """[60分K线回测] 原版卖点4条 + 3份分批减仓，但在【每根60分K线】上逐根判定
    （而非日线框架里每天只看一次60分快照——那正是原版迟钝的根因）。
    用于验证：原版思路是否因「时间框架错位」而失真；若60分逐根版跑赢五日线版，则闭环。

    隔离变量原则：与原版 run_exit_orig 保持【相同规则逻辑】，
      - 放量滞涨 / 破MA20/MA40 下放到【单根60分K】（粒度变量，唯一差异）
      - RSI 沿用原版【15分 RSI 按日】rmax>75 / rmin<45（与原版一致，不引入新变量）
    入场：信号日收盘；在60分K上定位该日首根=入场棒。
    """
    dates = daily["date"].tolist()
    opens = daily["open"].values
    closes = daily["close"].values
    ma20_d = daily["ma20"].values
    buy_price = float(daily["close"].values[buy_idx])
    buy_date = dates[buy_idx]
    ma5_buy = float(daily["ma5"].values[buy_idx])
    case = "A" if buy_price > ma5_buy else "B"
    # 日线 ma20 / close 按日期查表（rule4 用）
    daily_ma20_by_date, daily_close_by_date = {}, {}
    for j, d in enumerate(dates):
        key = d.strftime("%Y-%m-%d") if isinstance(d, pd.Timestamp) else str(d)[:10]
        daily_ma20_by_date[key] = float(ma20_d[j]) if pd.notna(ma20_d[j]) else None
        daily_close_by_date[key] = float(closes[j])
    bk = buy_date.strftime("%Y-%m-%d") if isinstance(buy_date, pd.Timestamp) else str(buy_date)[:10]
    m60_dates = m60["date"].tolist()
    try:
        entry_bar = m60_dates.index(bk)
    except ValueError:
        return run_exit_orig(daily, None, m15_rsi, buy_idx, path)  # 极端兜底（买入日无60分数据）
    m60_close = m60["close"].values
    m60_open = m60["open"].values
    m60_high = m60["high"].values
    m60_low = m60["low"].values
    m60_vol = m60["vol"].values
    m60_ma20 = m60["ma20"].values
    m60_ma40 = m60["ma40"].values
    m60_vma20 = m60["vol_ma20"].values
    remaining = 3
    parts = []
    seen_days = set()
    hold_days = 0
    k = entry_bar + 1
    N = len(m60)
    while k < N:
        c = float(m60_close[k]); o = float(m60_open[k])
        h = float(m60_high[k]); l = float(m60_low[k])
        vol = float(m60_vol[k])
        ma20_60 = float(m60_ma20[k]) if pd.notna(m60_ma20[k]) else None
        ma40_60 = float(m60_ma40[k]) if pd.notna(m60_ma40[k]) else None
        vma20_60 = float(m60_vma20[k]) if pd.notna(m60_vma20[k]) else None
        dk = m60_dates[k]
        if dk != bk and dk not in seen_days:
            seen_days.add(dk); hold_days += 1
        rmax, rmin = m15_rsi.get(dk, (50.0, 50.0))   # 15分 RSI（与原版一致，按日恒定）
        hl = (h - l)
        up_shadow = (h - max(o, c)) / hl if hl > 0 else 0.0
        stag = (vma20_60 is not None and vol > vma20_60 * 1.5) and (up_shadow > 0.5)
        r1 = bool(stag) and (rmax > 75)
        r2 = (ma20_60 is not None and c < ma20_60) and (rmin < 50)
        r3 = (ma40_60 is not None and c < ma40_60)
        d20 = daily_ma20_by_date.get(dk)
        r4 = (d20 is not None and float(daily_close_by_date.get(dk, c)) < d20)
        hard = c <= buy_price * 0.92
        if hard and remaining > 0:
            parts.append((k, c, "60m_hard_stop", remaining)); remaining = 0; break
        if remaining == 3 and r1:
            parts.append((k, c, "60m_rule1_top", 1)); remaining = 2
        elif remaining == 2 and r2:
            parts.append((k, c, "60m_rule2_m60ma20", 1)); remaining = 1
        elif remaining == 1 and (r3 or r4):
            parts.append((k, c, "60m_rule3_m60ma40" if r3 else "60m_rule4_dailyma20", 1)); remaining = 0; break
        if hold_days >= 60 and remaining > 0:
            parts.append((k, c, "60m_time_exit", remaining)); remaining = 0; break
        k += 1
    if remaining > 0:
        parts.append((N - 1, float(m60_close[N - 1]), "60m_eod", remaining))
    equiv_pnl = sum((p[1] - buy_price) / buy_price * 100 * (p[3] / 3.0) for p in parts)
    last = parts[-1]
    partials = ";".join(f"{p[3]}/3@{m60_dates[p[0]]}:{p[2]}" for p in parts)
    return dict(
        buy_date=bk, buy_price=round(buy_price, 3),
        exit_date=m60_dates[last[0]], exit_price=round(last[1], 3),
        hold_days=hold_days, pnl=round(equiv_pnl, 2), reason=last[2],
        path=path, case=case, n_reductions=len(parts), partials=partials,
    )


def run_exit_ultra(daily, m60, m15_rsi, buy_idx, path,
                   stop_floor=-0.05, time_exit=60, precond=True, loss_only=True):
    """[最强版·待拟合] 60分K线锁利(原版分批减仓) + 日MA5带前置条件的紧止损。

    设计目标（取已验证的两处长处的交集）：
      * 利润引擎：沿用 run_exit_60m 的【每根60分K逐根】原版4条 + 3份分批减仓
        —— 顶部预警(r1)减1/3 / 破60分MA20(r2)再减1/3 / 破60分MA40或日MA20(r3/r4)清仓。
        已验证：60分MA40破位清仓 = 81笔/胜率87.7%/均值+8.26%（让利润跑的能力）。
      * 止损（替换原版-8%松硬砍）：
        (1) 日MA5前置条件止损：仅在【当日收盘】判 close<MA5 且 (close<MA20 或 放量派发)
            才清剩余；仅收破MA5但仍在MA20上方且非放量 → 判为洗盘/回踩 → 持有（前置过滤）。
            —— 直接治「跌破日MA5即砍在强波动洗盘下被洗出去」的毛病。
        (2) 绝对地板：60分收盘 <= 买入价*(1+stop_floor)（默认-5%，比原版-8%更紧）。
        (3) 长持兜底：time_exit 日（默认60）。
    聚合：3份加权等效整仓收益（同 run_exit_orig），字段可直接对比。
    """
    dates = daily["date"].tolist()
    opens = daily["open"].values
    closes = daily["close"].values
    ma5_d = daily["ma5"].values
    ma20_d = daily["ma20"].values
    vol_d = daily["vol"].values
    vol_ma20_d = daily["vol"].rolling(20).mean().values
    buy_price = float(daily["close"].values[buy_idx])
    buy_date = dates[buy_idx]
    ma5_buy = float(ma5_d[buy_idx])
    case = "A" if buy_price > ma5_buy else "B"

    daily_ma5_by_date, daily_ma20_by_date = {}, {}
    daily_close_by_date, daily_vol_by_date, daily_vma20_by_date = {}, {}, {}
    for j, d in enumerate(dates):
        key = d.strftime("%Y-%m-%d") if isinstance(d, pd.Timestamp) else str(d)[:10]
        daily_ma5_by_date[key] = float(ma5_d[j]) if pd.notna(ma5_d[j]) else None
        daily_ma20_by_date[key] = float(ma20_d[j]) if pd.notna(ma20_d[j]) else None
        daily_close_by_date[key] = float(closes[j])
        daily_vol_by_date[key] = float(vol_d[j]) if pd.notna(vol_d[j]) else 0.0
        daily_vma20_by_date[key] = float(vol_ma20_d[j]) if pd.notna(vol_ma20_d[j]) else 0.0

    bk = buy_date.strftime("%Y-%m-%d") if isinstance(buy_date, pd.Timestamp) else str(buy_date)[:10]
    m60_dates = m60["date"].tolist()
    try:
        entry_bar = m60_dates.index(bk)
    except ValueError:
        return run_exit(daily, buy_idx, path, False)   # 极端兜底（买入日无60分数据）→ 退化为五日线版

    m60_close = m60["close"].values
    m60_open = m60["open"].values
    m60_high = m60["high"].values
    m60_low = m60["low"].values
    m60_vol = m60["vol"].values
    m60_ma20 = m60["ma20"].values
    m60_ma40 = m60["ma40"].values
    m60_vma20 = m60["vol_ma20"].values

    remaining = 3
    parts = []
    seen_days = set()
    hold_days = 0
    last_day = None
    k = entry_bar + 1
    N = len(m60)
    while k < N:
        c = float(m60_close[k]); o = float(m60_open[k])
        h = float(m60_high[k]); l = float(m60_low[k])
        vol = float(m60_vol[k])
        ma20_60 = float(m60_ma20[k]) if pd.notna(m60_ma20[k]) else None
        ma40_60 = float(m60_ma40[k]) if pd.notna(m60_ma40[k]) else None
        vma20_60 = float(m60_vma20[k]) if pd.notna(m60_vma20[k]) else None
        dk = m60_dates[k]
        if dk != last_day:
            last_day = dk
            if dk not in seen_days:
                seen_days.add(dk); hold_days += 1
        rmax, rmin = m15_rsi.get(dk, (50.0, 50.0))
        hl = (h - l)
        up_shadow = (h - max(o, c)) / hl if hl > 0 else 0.0
        stag = (vma20_60 is not None and vol > vma20_60 * 1.5) and (up_shadow > 0.5)
        # 60分利润引擎（原版4条逐根）
        r1 = bool(stag) and (rmax > 75)
        r2 = (ma20_60 is not None and c < ma20_60) and (rmin < 50)
        r3 = (ma40_60 is not None and c < ma40_60)
        d20 = daily_ma20_by_date.get(dk)
        r4 = (d20 is not None and float(daily_close_by_date.get(dk, c)) < d20)
        hard = c <= buy_price * (1.0 + stop_floor)
        # ===== 日MA5带前置条件止损（在【当日最后一根】60分K判定，用日线收盘确认）=====
        # 前置条件：默认仅在【已处亏损】且(收破MA20或放量派发)时砍
        #   —— 盈利单的回踩洗盘【绝不砍】，交给60分锁利引擎去跑（治「被洗出去」）
        is_last_bar_of_day = (k == N - 1) or (m60_dates[k + 1] != dk)
        ma5_stop = False
        if is_last_bar_of_day:
            d_ma5 = daily_ma5_by_date.get(dk)
            d_ma20 = daily_ma20_by_date.get(dk)
            d_close = daily_close_by_date.get(dk)
            d_vol = daily_vol_by_date.get(dk, 0.0)
            d_vma20 = daily_vma20_by_date.get(dk, 0.0)
            if d_ma5 is not None and d_close < d_ma5:
                if not precond:
                    cond = True
                else:
                    distribution = (d_vma20 > 0) and (d_vol > d_vma20 * 1.5)
                    below_ma20 = (d_ma20 is not None) and (d_close < d_ma20)
                    cond = bool(below_ma20 or distribution)
                if loss_only:
                    cond = cond and (d_close < buy_price)
                ma5_stop = cond
        # ===== 触发顺序：地板 > 利润引擎(减/清) > MA5前置止损 > 长持兜底 =====
        if hard and remaining > 0:
            parts.append((k, c, "u_floor", remaining)); remaining = 0; break
        if remaining == 3 and r1:
            parts.append((k, c, "u_rule1_top", 1)); remaining = 2
        elif remaining == 2 and r2:
            parts.append((k, c, "u_rule2_m60ma20", 1)); remaining = 1
        elif remaining == 1 and (r3 or r4):
            parts.append((k, c, "u_rule3_m60ma40" if r3 else "u_rule4_dailyma20", 1)); remaining = 0; break
        if ma5_stop and remaining > 0:
            parts.append((k, c, "u_ma5_stop", remaining)); remaining = 0; break
        if hold_days >= time_exit and remaining > 0:
            parts.append((k, c, "u_time_exit", remaining)); remaining = 0; break
        k += 1
    if remaining > 0:
        parts.append((N - 1, float(m60_close[N - 1]), "u_eod", remaining))
    equiv_pnl = sum((p[1] - buy_price) / buy_price * 100 * (p[3] / 3.0) for p in parts)
    last = parts[-1]
    partials = ";".join(f"{p[3]}/3@{m60_dates[p[0]]}:{p[2]}" for p in parts)
    return dict(
        buy_date=bk, buy_price=round(buy_price, 3),
        exit_date=m60_dates[last[0]], exit_price=round(last[1], 3),
        hold_days=hold_days, pnl=round(equiv_pnl, 2), reason=last[2],
        path=path, case=case, n_reductions=len(parts), partials=partials,
    )


def run_exit_hybrid(daily, m60_daily, m15_rsi, buy_idx, path, use_ma20=False):
    """揉合版：原版分批减仓(锁利引擎) + 现版五日线保底网(快刀止损)。

    设计取舍（取两者长处）：
      * 保留原版 run_exit_orig 的 3 份递进减仓(rule1顶/rule2破60分MA20/rule3破60分MA40或rule4破日MA20)
        —— 这是盈利单能吃 +12% 的利润引擎（让利润跑）。
      * 叠加现版 run_exit 的「五日线保底网」作 FLOOR(只清不清减)：
          情形A(买价>5日线) 收盘破5日线 → 清剩余
          情形B(买价<5日线) 三重确认后破5日线 → 清剩余
          -8% 硬砍 → 清剩余
        —— 治原版「弱势票扛到-8%、平均34天」的毛病，让亏损单像现版一样早砍(~-3.6%)。
      * 保底网用现版【完整】纪律(硬砍-8% + RSI>70过热了结 + 20日时间退出 + 五日线破位)，
        不学原版那样撤掉这些导致弱势票扛到-8%；时间退出用20日(同现版)而非60日。
    聚合方式同 run_exit_orig(3份加权等效整仓收益)，字段可直接三方对比。
    """
    n = len(daily)
    dates = daily["date"].tolist()
    opens = daily["open"].values
    closes = daily["close"].values
    ma5 = daily["ma5"].values
    ma20 = daily["ma20"].values
    stag = daily["stagnation_top"].values
    d_rsi = daily["d_rsi"].values
    buy_price = float(daily["close"].values[buy_idx])
    buy_date = dates[buy_idx]
    ma5_buy = float(ma5[buy_idx])
    case = "A" if buy_price > ma5_buy else "B"
    triple_done = False

    def key_of(d):
        return d.strftime("%Y-%m-%d") if isinstance(d, pd.Timestamp) else str(d)[:10]

    remaining = 3
    parts = []
    hold = 0
    for k in range(buy_idx + 1, n):
        hold += 1
        c = float(closes[k])
        m5 = float(ma5[k])
        m20 = float(ma20[k])
        dk = key_of(dates[k])
        m60_20 = m60_40 = None
        if m60_daily is not None and dk in m60_daily.index:
            row = m60_daily.loc[dk]
            m60_20 = float(row["ma20"]) if pd.notna(row["ma20"]) else None
            m60_40 = float(row["ma40"]) if pd.notna(row["ma40"]) else None
        rmax, rmin = m15_rsi.get(dk, (50.0, 50.0))
        r = float(d_rsi[k]) if pd.notna(d_rsi[k]) else 50.0
        # === 保底网(现版完整纪律：硬砍+过热+20日+五日线，优先检查，清剩余) ===
        if c <= buy_price * 0.92:
            parts.append((k, c, "h_hard_stop", remaining)); remaining = 0; break
        if r > 70:
            parts.append((k, c, "h_rsi_overheat", remaining)); remaining = 0; break
        if case == "A":
            if c < m5:
                parts.append((k, c, "h_ma5_A", remaining)); remaining = 0; break
        else:
            if not triple_done and k >= 2:
                # 三重确认（对齐实盘 sell.py:117-121，2026-08-06 修方向 bug）：
                # MA5 向上拐头 + 昨日收在 MA5 下方 + 昨日上涨 + 今日收盘上穿 MA5
                ma5_turn = m5 > float(ma5[k - 1])                    # 实盘: ma5_today > ma5_yest
                y_below = float(closes[k - 1]) <= float(ma5[k - 1])  # 实盘: close_y <= ma5_yest
                y_up = float(closes[k - 1]) > float(closes[k - 2])   # 实盘: close_2 < close_y
                cross_up = c > m5                                      # 实盘: close > ma5_today
                if ma5_turn and y_below and y_up and cross_up:
                    triple_done = True
            if triple_done and c < m5:
                parts.append((k, c, "h_ma5_B", remaining)); remaining = 0; break
        if use_ma20 and path in ("main", "early"):
            if k >= 1 and c < m20 and float(closes[k - 1]) < float(ma20[k - 1]):
                parts.append((k, c, "h_ma20_break", remaining)); remaining = 0; break
        # === 分批减仓(原版利润引擎，只减不清) ===
        r1 = bool(stag[k]) and (rmax > 75)
        r2 = (m60_20 is not None and c < m60_20) and (rmin < 50)
        r3 = (m60_40 is not None and c < m60_40)
        r4 = c < float(ma20[k])
        if remaining == 3 and r1:
            parts.append((k, c, "h_rule1_top", 1)); remaining = 2
        elif remaining == 2 and r2:
            parts.append((k, c, "h_rule2_m60ma20", 1)); remaining = 1
        elif remaining == 1 and (r3 or r4):
            parts.append((k, c, "h_rule3_m60ma40" if r3 else "h_rule4_dailyma20", 1)); remaining = 0; break
        if hold >= 20 and remaining > 0:
            parts.append((k, c, "h_time_exit", remaining)); remaining = 0; break
    if remaining > 0:
        parts.append((n - 1, float(closes[n - 1]), "h_eod", remaining))
    tot_frac = sum(p[3] for p in parts)
    equiv_pnl = sum((p[1] - buy_price) / buy_price * 100 * (p[3] / 3.0) for p in parts)
    w_hold = sum((p[0] - buy_idx) * p[3] for p in parts) / tot_frac if tot_frac else 0.0
    last = parts[-1]
    partials = ";".join(f"{p[3]}/3@{key_of(dates[p[0]])}:{p[2]}" for p in parts)
    return dict(
        buy_date=buy_date, buy_price=round(buy_price, 3),
        exit_date=dates[last[0]], exit_price=round(last[1], 3),
        hold_days=round(w_hold, 1), pnl=round(equiv_pnl, 2), reason=last[2],
        path=path, case=case, n_reductions=len(parts),
        partials=partials,
    )


def backtest_stock(market, code, name, use_ma20, daily_count=1200, min60_count=3000, min15_count=8000, entry_filter=False, exit_dull=False, use_orig_sell=False):
    daily_df = fetch(market, code, Period.DAILY, daily_count)
    if daily_df is None or len(daily_df) < 120:
        return None, f"日线不足"
    m60_df = fetch(market, code, Period.MIN_60, min60_count)
    m15_df = fetch(market, code, Period.MIN_15, min15_count)
    daily, m60_daily, m60_pb, m15_raw, m15_rsi = prep_indicators(daily_df, m60_df, m15_df)
    if not m15_raw:
        return None, "15分数据缺失"
    # 回测窗口起点 = 15分最早覆盖日（保证④⑤可判）
    min_day = min(pd.to_datetime(d) for d in m15_raw.keys())
    dates = daily["date"].tolist()
    first_i = next((i for i, d in enumerate(dates) if d >= min_day), None)
    if first_i is None:
        return None, "日期对齐失败"
    n = len(daily)
    trades = []
    i = first_i
    last_entry_i = -999
    COOLDOWN = 5   # 同票冷却：避免连续信号刷屏，保证两种卖点用相同 entry 集合(公平对比)
    while i < n:
        _, path, entry = seq_state_at(daily, m60_daily, m60_pb, m15_raw, dates, i)
        if entry and path in ("main", "early"):
            buy_idx = i
            if buy_idx >= n:
                break
            if i - last_entry_i < COOLDOWN:
                i += 1
                continue
            # [入场过滤] 对齐实盘 scan_stock：以【信号日】收盘 vs 当日MA5 判定是否贴近收敛区
            #   （实盘只能收盘时确认，无法预知次日开盘；故两端统一用信号日 close ≤ MA5×ENTRY_MAX_ABOVE_MA5）
            if entry_filter:
                s_close = float(daily["close"].values[i])
                s_ma5 = float(daily["ma5"].values[i])
                if pd.isna(s_ma5) or s_close > s_ma5 * ENTRY_MAX_ABOVE_MA5:
                    i += 1
                    continue
            if use_orig_sell:
                t = run_exit_orig(daily, m60_daily, m15_rsi, buy_idx, path)
            else:
                t = run_exit(daily, buy_idx, path, use_ma20, exit_dull=exit_dull)
            t["code"] = code
            t["name"] = name
            t["market"] = market
            trades.append(t)
            last_entry_i = i
        i += 1
    return trades, None


# ===================== 统计 =====================
def summarize(trades):
    if not trades:
        return dict(total=0)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = len(trades)
    sum_w = sum(wins)
    sum_l = sum(losses)
    pf = (sum_w / abs(sum_l)) if sum_l and sum_l != 0 else float("inf")
    return dict(
        total=total,
        win_rate=round(len(wins) / total * 100, 1),
        avg_win=round(sum_w / len(wins), 2) if wins else 0,
        avg_loss=round(sum_l / len(losses), 2) if losses else 0,
        profit_factor=round(pf, 2) if pf != float("inf") else 999,
        median=round(float(np.median(pnls)), 2),
        avg_hold=round(sum(t["hold_days"] for t in trades) / total, 1),
        best=round(max(pnls), 2),
        worst=round(min(pnls), 2),
    )


def by_field(trades, field):
    out = {}
    for t in trades:
        out.setdefault(t[field], []).append(t["pnl"])
    rows = []
    for k, v in out.items():
        w = [x for x in v if x > 0]
        l = [x for x in v if x <= 0]
        rows.append((k, len(v), round(len(w) / len(v) * 100, 1),
                     round(sum(v) / len(v), 2), round(min(v), 2), round(max(v), 2)))
    return rows


def by_year(trades):
    out = {}
    for t in trades:
        y = pd.to_datetime(t["exit_date"]).year
        out.setdefault(y, []).append(t["pnl"])
    rows = []
    for y in sorted(out):
        v = out[y]
        w = [x for x in v if x > 0]
        rows.append((y, len(v), round(len(w) / len(v) * 100, 1), round(sum(v) / len(v), 2)))
    return rows


# ===================== SVG（零依赖）=====================
def bar_svg(title, items, fname, ymax=100, label_w=46):
    """items: list[(label, value)]，value 为百分比。"""
    W = 560
    H = 260
    pad_l = label_w
    pad_b = 36
    pad_t = 30
    plot_w = W - pad_l - 14
    plot_h = H - pad_t - pad_b
    bw = plot_w / max(len(items), 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">']
    parts.append(f'<text x="{pad_l}" y="18" font-size="13" font-family="sans-serif">{title}</text>')
    for idx, (lab, val) in enumerate(items):
        x = pad_l + idx * bw
        h = plot_h * min(max(val, 0), ymax) / ymax
        y = pad_t + plot_h - h
        col = "#c0392b" if val >= 50 else "#2980b9"
        parts.append(f'<rect x="{x+4:.0f}" y="{y:.0f}" width="{bw-8:.0f}" height="{h:.0f}" fill="{col}"/>')
        parts.append(f'<text x="{x+bw/2:.0f}" y="{y-3:.0f}" font-size="10" text-anchor="middle" font-family="sans-serif">{val:.0f}</text>')
        parts.append(f'<text x="{x+bw/2:.0f}" y="{pad_t+plot_h+14:.0f}" font-size="10" text-anchor="middle" font-family="sans-serif">{lab}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h:.0f}" x2="{W-14}" y2="{pad_t+plot_h:.0f}" stroke="#888"/>')
    parts.append(f'<text x="{pad_l}" y="{pad_t+plot_h-2:.0f}" font-size="9" fill="#888" font-family="sans-serif">50%</text>')
    parts.append("</svg>")
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def hist_svg(title, pnls, fname, bins=11):
    import math
    W = 560
    H = 260
    pad_l = 40
    pad_b = 36
    pad_t = 30
    plot_w = W - pad_l - 14
    plot_h = H - pad_t - pad_b
    lo = math.floor(min(pnls) / 5) * 5
    hi = math.ceil(max(pnls) / 5) * 5
    if hi == lo:
        hi = lo + 5
    step = (hi - lo) / bins
    counts = [0] * bins
    for p in pnls:
        b = min(bins - 1, int((p - lo) / step))
        if b < 0:
            b = 0
        counts[b] += 1
    mx = max(counts) or 1
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">']
    parts.append(f'<text x="{pad_l}" y="18" font-size="13" font-family="sans-serif">{title} (%)</text>')
    bw = plot_w / bins
    for i, c in enumerate(counts):
        x = pad_l + i * bw
        h = plot_h * c / mx
        y = pad_t + plot_h - h
        parts.append(f'<rect x="{x+2:.0f}" y="{y:.0f}" width="{bw-4:.0f}" height="{h:.0f}" fill="#16a085"/>')
        parts.append(f'<text x="{x+bw/2:.0f}" y="{pad_t+plot_h+14:.0f}" font-size="9" text-anchor="middle" font-family="sans-serif">{lo+i*step:.0f}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h:.0f}" x2="{W-14}" y2="{pad_t+plot_h:.0f}" stroke="#888"/>')
    parts.append("</svg>")
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


# ===================== 股票池（跨板块 ~40 只，含用户提到的龙净环保）=====================
POOL = [
    ("1", "600519", "贵州茅台"), ("1", "600036", "招商银行"), ("1", "601318", "中国平安"),
    ("1", "601012", "隆基绿能"), ("1", "600276", "恒瑞医药"), ("1", "603259", "药明康德"),
    ("1", "601633", "长城汽车"), ("1", "600660", "福耀玻璃"), ("1", "601689", "拓普集团"),
    ("1", "601899", "紫金矿业"), ("1", "600309", "万华化学"), ("1", "601088", "中国神华"),
    ("1", "688981", "中芯国际"), ("1", "603501", "韦尔股份"), ("1", "600893", "航发动力"),
    ("1", "600585", "海螺水泥"), ("1", "600048", "保利发展"), ("1", "600388", "龙净环保"),
    ("1", "600031", "三一重工"), ("1", "600887", "伊利股份"),
    ("0", "000858", "五粮液"), ("0", "000568", "泸州老窖"), ("0", "002594", "比亚迪"),
    ("0", "300750", "宁德时代"), ("0", "300274", "阳光电源"), ("0", "300760", "迈瑞医疗"),
    ("0", "300059", "东方财富"), ("0", "002475", "立讯精密"), ("0", "002241", "歌尔股份"),
    ("0", "300433", "蓝思科技"), ("0", "002179", "中航光电"), ("0", "000333", "美的集团"),
    ("0", "000651", "格力电器"), ("0", "002371", "北方华创"), ("0", "002271", "东方雨虹"),
    ("0", "000725", "京东方A"), ("0", "002230", "科大讯飞"), ("0", "300015", "爱尔眼科"),
    ("0", "002142", "宁波银行"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=str, default=None, help="只跑一只验收，格式 600519(默认沪) 或 SH600519")
    ap.add_argument("--ma20", action="store_true", help="仅开启 MA20 两日破位 A/B 变体")
    ap.add_argument("--entry-filter", action="store_true", help="入场过滤：剔除高开追入(case A)，只在买价≤MA5(收敛区)时上车")
    ap.add_argument("--exit-dull", action="store_true", help="离场钝化：case B 仅'有效跌破'MA5才止损(连续两日破/单日跌>1.5%)，过滤单日噪音扫损")
    ap.add_argument("--orig-sell", action="store_true", help="原版卖点(截图4条)+分批减仓：日K放量滞涨+15分RSI>75+长上影/60分破MA20+15分RSI<45/60分破MA40/日K破MA20，3份递进减仓")
    args = ap.parse_args()

    if not available():
        print("[FATAL] 通达信源不可用(available()=False)。请确认本机通达信客户端已开、数据可读。")
        sys.exit(2)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report")
    os.makedirs(out_dir, exist_ok=True)

    use_ma20 = args.ma20
    entry_filter = args.entry_filter
    exit_dull = args.exit_dull
    use_orig_sell = args.orig_sell
    suf = ""
    if entry_filter:
        suf += "_ef"
    if exit_dull:
        suf += "_d"
    if use_orig_sell:
        suf += "_orig"
    if args.baseline:
        code = args.baseline[-6:]
        market = "0" if code.startswith(("0", "3", "2")) else "1"
        pool = [(market, code, code)]
        print(f"[run] 基准股模式: {market}{code}")
    else:
        pool = POOL
        print(f"[run] 全池模式: {len(pool)} 只，use_ma20={use_ma20}，entry_filter={entry_filter}")

    all_trades = []
    for market, code, name in pool:
        try:
            tr, err = backtest_stock(market, code, name, use_ma20, entry_filter=entry_filter, exit_dull=exit_dull, use_orig_sell=use_orig_sell)
        except Exception as e:
            print(f"  [skip] {code} {name}: {e}")
            continue
        if tr is None:
            print(f"  [skip] {code} {name}: {err}")
            continue
        print(f"  [ok] {code} {name}: {len(tr)} 笔")
        all_trades.extend(tr)

    if not all_trades:
        print("[done] 无成交。可能回测窗口内无 ⑤ 触发，或 15分数据不足。")
        return

    # CSV
    cols = ["code", "name", "market", "path", "case", "buy_date", "buy_price",
            "exit_date", "exit_price", "hold_days", "pnl", "reason"]
    if use_orig_sell:
        cols += ["n_reductions", "partials"]
    df_out = pd.DataFrame(all_trades)[cols]
    csv_path = os.path.join(out_dir, f"dualpath_trades{suf}.csv")
    df_out.to_csv(csv_path, index=False, encoding="utf-8-sig")

    s = summarize(all_trades)
    by_p = by_field(all_trades, "path")
    by_r = by_field(all_trades, "reason")
    by_y = by_year(all_trades)

    # SVG
    year_items = [(str(y), wr) for y, _, wr, _ in by_y]
    if year_items:
        bar_svg("逐年胜率(%)", year_items, os.path.join(out_dir, f"dualpath_year{suf}.svg"))
    hist_svg("单笔收益分布", [t["pnl"] for t in all_trades], os.path.join(out_dir, f"dualpath_pnl{suf}.svg"))

    # 报告
    lines = []
    tags = []
    if entry_filter:
        tags.append("入场过滤")
    if exit_dull:
        tags.append("离场钝化")
    if use_orig_sell:
        tags.append("原版卖点+分批减仓")
    tag_str = "(" + "·".join(tags) + ")" if tags else ""
    lines.append(f"# 双路径(主升/初期)框架回测报告{tag_str}\n")
    lines.append(f"- 模式: {'基准股' if args.baseline else '全池'} | MA20两日破位变体: {use_ma20} | 入场过滤: {entry_filter} | 离场钝化: {exit_dull}")
    lines.append(f"- 回测窗口由 15分数据覆盖决定(约 150 交易日)；买点=序贯状态机⑤响应；卖点=SOP MA5相对止损+清仓四条件")
    lines.append(f"- 防未来函数: 信号日 i 收盘确认并以信号日收盘价入场\n")
    lines.append(f"## 总览\n")
    lines.append(f"- 总笔数: **{s['total']}**")
    lines.append(f"- 胜率: **{s['win_rate']}%**")
    lines.append(f"- 平均每笔: {s['median']}% (中位数) / 均值见分层")
    lines.append(f"- 平均盈利: {s['avg_win']}% | 平均亏损: {s['avg_loss']}% | 盈亏比: **{s['profit_factor']}**")
    lines.append(f"- 平均持仓: {s['avg_hold']} 日 | 最佳: {s['best']}% | 最差: {s['worst']}%\n")
    lines.append(f"## 按路径分层\n")
    lines.append(f"| 路径 | 笔数 | 胜率 | 均值% | 最差 | 最佳 |")
    lines.append(f"|---|---|---|---|---|---|")
    for k, n, wr, mean, lo, hi in by_p:
        lines.append(f"| {k} | {n} | {wr}% | {mean} | {lo} | {hi} |")
    lines.append(f"\n## 按离场原因分层（hard_stop 为风控假设，单列不计入框架贡献）\n")
    lines.append(f"| 原因 | 笔数 | 胜率 | 均值% | 最差 | 最佳 |")
    lines.append(f"|---|---|---|---|---|---|")
    for k, n, wr, mean, lo, hi in by_r:
        lines.append(f"| {k} | {n} | {wr}% | {mean} | {lo} | {hi} |")
    lines.append(f"\n## 按年份分层\n")
    lines.append(f"| 年份 | 笔数 | 胜率 | 均值% |")
    lines.append(f"|---|---|---|---|")
    for y, n, wr, mean in by_y:
        lines.append(f"| {y} | {n} | {wr}% | {mean} |")
    lines.append(f"\n## 结论\n")
    lines.append(f"- 属 __型策略（按胜率/盈亏比判断）；盈亏比 {s['profit_factor']}，中位数 {s['median']}%。")
    lines.append(f"- 若 hard_stop 笔数占比高，说明框架本身触发点偏多假信号；若 ma5_A/ma5_B 大面积亏损，说明相对止损滞后。")
    lines.append(f"- A/B 对比：另跑 `--ma20` 看 MA20 两日破位是否改善趋势失守类离场。\n")
    lines.append(f"> 仅供研究参考，不构成投资建议，决策权在人。\n")
    md_path = os.path.join(out_dir, f"dualpath_report{suf}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n[done] 总笔数={s['total']} 胜率={s['win_rate']}% 盈亏比={s['profit_factor']}")
    print(f"  CSV : {csv_path}")
    print(f"  报告: {md_path}")
    print(f"  图表: {os.path.join(out_dir, f'dualpath_year{suf}.svg')} / dualpath_pnl{suf}.svg")


if __name__ == "__main__":
    main()
