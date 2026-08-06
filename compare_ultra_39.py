# -*- coding: utf-8 -*-
"""39 只 POOL 拟合「最强版 v2」：在完全相同的买点上对比
  基线：现版(五日线) / 60分K线(逐根)
  v2 候选（run_exit_ultra，loss_only=亏损单才砍）：
    vA  loss_only+前置(破MA20/放量)  地板-5%  60日兜底
    vB  loss_only 仅(收破MA5即砍亏损单)  地板-5%  60日兜底
    vC  loss_only+前置  地板-5%  无时间退出
    vD  loss_only+前置  地板-8%  60日兜底
公平原则：差异纯粹来自「怎么卖」。每只票 fetch+prep 一次，所有退出各算一遍。
"""
import pandas as pd, numpy as np, traceback
import backtest_dualpath as B
from tdx_source import available

COOLDOWN = 5
DAILY_COUNT, M60_COUNT, M15_COUNT = 1200, 3000, 8000
LABELS = ["五日线", "60分", "vA", "vB", "vC", "vD"]
ULTRA = {"vA": (-0.05, 60, True, True),
         "vB": (-0.05, 60, False, True),
         "vC": (-0.05, 999, True, True),
         "vD": (-0.08, 60, True, True)}


def scan_all(market, code, name):
    daily_df = B.fetch(market, code, B.Period.DAILY, DAILY_COUNT)
    if daily_df is None or len(daily_df) < 120:
        return None, "日线不足"
    m60_df = B.fetch(market, code, B.Period.MIN_60, M60_COUNT)
    m15_df = B.fetch(market, code, B.Period.MIN_15, M15_COUNT)
    daily, m60_daily, m60_pb, m15_raw, m15_rsi = B.prep_indicators(daily_df, m60_df, m15_df)
    if not m15_raw:
        return None, "15分缺失"
    m60 = B.min60_signals(m60_df.copy())
    m60["date"] = m60["datetime"].dt.strftime("%Y-%m-%d")
    dates = daily["date"].tolist()
    res = {lab: [] for lab in LABELS}
    n = len(daily)
    last_entry_i = -999
    i = 0
    while i < n:
        _, path, entry = B.seq_state_at(daily, m60_daily, m60_pb, m15_raw, dates, i)
        if entry and path in ("main", "early"):
            buy_idx = i
            if buy_idx >= n:
                break
            if i - last_entry_i < COOLDOWN:
                i += 1
                continue
            tc = B.run_exit(daily, buy_idx, path, False)
            t6 = B.run_exit_60m(daily, m60, m15_rsi, buy_idx, path)
            tc["code"] = code; tc["name"] = name; tc["market"] = market
            tc["buy_date"] = str(tc["buy_date"])[:10]; tc["exit_date"] = str(tc["exit_date"])[:10]
            t6["code"] = code; t6["name"] = name; t6["market"] = market
            t6["buy_date"] = str(t6["buy_date"])[:10]; t6["exit_date"] = str(t6["exit_date"])[:10]
            for lab in ("vA", "vB", "vC", "vD"):
                fl, te, pc, lo = ULTRA[lab]
                t = B.run_exit_ultra(daily, m60, m15_rsi, buy_idx, path, fl, te, pc, lo)
                t["code"] = code; t["name"] = name; t["market"] = market
                t["buy_date"] = str(t["buy_date"])[:10]; t["exit_date"] = str(t["exit_date"])[:10]
                res[lab].append(t)
            res["五日线"].append(tc); res["60分"].append(t6)
            last_entry_i = i
        i += 1
    return res, None


def pf(d):
    w = d[d.pnl > 0].pnl.sum()
    l = abs(d[d.pnl <= 0].pnl.sum())
    return round(w / l, 2) if l else 0


def main():
    if not available():
        print("[FATAL] 通达信源不可用")
        return
    agg = {lab: [] for lab in LABELS}
    for (market, code, name) in B.POOL:
        try:
            r, err = scan_all(market, code, name)
        except Exception as e:
            print(f"  [skip] {code} {name}: {e}")
            traceback.print_exc()
            continue
        if err:
            print(f"  [skip] {code} {name}: {err}")
            continue
        for lab in LABELS:
            agg[lab] += r[lab]
        print(f"  [done] {code} {name}: 五日线={len(r['五日线'])} vA={len(r['vA'])}")
    dfs = {lab: pd.DataFrame(agg[lab]) for lab in LABELS}
    print("\n=== 全版本对比（同一批买点，笔数应相等）===")
    for lab in LABELS:
        d = dfs[lab]
        if len(d) == 0:
            print(f"{lab:10} [无数据]")
            continue
        wd = d[d.pnl > 0]
        print(f"{lab:10} 笔数={len(d)} 胜率={round((d.pnl>0).mean()*100,1)}% 盈亏比={pf(d)} "
              f"中位={round(d.pnl.median(),2)}% 均盈={round(wd.pnl.mean(),2) if len(wd) else 0}% "
              f"均亏={round(d[d.pnl<=0].pnl.mean(),2)}% 均持={round(d.hold_days.mean(),1)}天")
    for lab in ("vA", "vB", "vC", "vD"):
        print(f"\n=== {lab} 按离场原因 ===")
        print(dfs[lab].groupby("reason").agg(笔数=("pnl", "size"),
              胜率=("pnl", lambda s: round((s > 0).mean() * 100, 1)),
              均值=("pnl", "mean")).round(2).sort_values("笔数", ascending=False).to_string())
    # 落盘（基线无 n_reductions/partials）
    base_cols = ["code", "name", "market", "path", "case", "buy_date", "buy_price",
                 "exit_date", "exit_price", "hold_days", "pnl", "reason"]
    ultra_cols = base_cols + ["n_reductions", "partials"]
    dfs["五日线"][base_cols].to_csv("backtest_report/cmpu_五日线.csv", index=False, encoding="utf-8-sig")
    dfs["60分"][base_cols].to_csv("backtest_report/cmpu_60分.csv", index=False, encoding="utf-8-sig")
    for lab in ("vA", "vB", "vC", "vD"):
        dfs[lab][ultra_cols].to_csv(f"backtest_report/cmpu_{lab}.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
