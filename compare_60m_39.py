# -*- coding: utf-8 -*-
"""39 只 POOL 三方公平对比：现版(五日线) vs 原版(日线60分快照) vs 60分K线(逐根)。

公平原则：三种卖点跑在完全相同的买点上（纯信号扫描 + 5 日冷却），
差异纯粹来自「怎么卖 / 什么粒度」。每只票 fetch+prep 一次，三种退出各算一遍。
输出：backtest_report/cmp60_cur.csv / cmp60_orig.csv / cmp60_60m.csv
"""
import pandas as pd, numpy as np
import backtest_dualpath as B
from tdx_source import available

COOLDOWN = 5
DAILY_COUNT, M60_COUNT, M15_COUNT = 1200, 3000, 8000


def scan3(market, code, name, entry_filter):
    daily_df = B.fetch(market, code, B.Period.DAILY, DAILY_COUNT)
    if daily_df is None or len(daily_df) < 120:
        return None, None, None, "日线不足"
    m60_df = B.fetch(market, code, B.Period.MIN_60, M60_COUNT)
    m15_df = B.fetch(market, code, B.Period.MIN_15, M15_COUNT)
    daily, m60_daily, m60_pb, m15_raw, m15_rsi = B.prep_indicators(daily_df, m60_df, m15_df)
    if not m15_raw:
        return None, None, None, "15分缺失"
    # 原始 60分K线（含 OHLC + MA20/MA40/vol_ma20），供 run_exit_60m 逐根扫描
    m60 = B.min60_signals(m60_df.copy())
    m60["date"] = m60["datetime"].dt.strftime("%Y-%m-%d")
    dates = daily["date"].tolist()
    cur, orig, s60 = [], [], []
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
            if entry_filter:
                bp = float(daily["close"].values[buy_idx])
                m5b = float(daily["ma5"].values[buy_idx])
                if bp > m5b * B.ENTRY_MAX_ABOVE_MA5:
                    i += 1
                    continue
            tc = B.run_exit(daily, buy_idx, path, False)
            to = B.run_exit_orig(daily, m60_daily, m15_rsi, buy_idx, path)
            t6 = B.run_exit_60m(daily, m60, m15_rsi, buy_idx, path)
            for t in (tc, to, t6):
                t["code"] = code
                t["name"] = name
                t["market"] = market
                t["buy_date"] = str(t["buy_date"])[:10]
                t["exit_date"] = str(t["exit_date"])[:10]
            cur.append(tc)
            orig.append(to)
            s60.append(t6)
            last_entry_i = i
        i += 1
    return cur, orig, s60, None


def pf(d):
    w = d[d.pnl > 0].pnl.sum()
    l = abs(d[d.pnl <= 0].pnl.sum())
    return round(w / l, 2) if l else 0


def main():
    if not available():
        print("[FATAL] 通达信源不可用")
        return
    cur_all, orig_all, s60_all = [], [], []
    for (market, code, name) in B.POOL:
        try:
            c, o, s, err = scan3(market, code, name, False)
        except Exception as e:
            print(f"  [skip] {code} {name}: {e}")
            continue
        if err:
            print(f"  [skip] {code} {name}: {err}")
            continue
        cur_all += c or []
        orig_all += o or []
        s60_all += s or []
        print(f"  [done] {code} {name}: cur={len(c)} orig={len(o)} 60m={len(s)}")
    cols = ["code", "name", "market", "path", "case", "buy_date", "buy_price",
            "exit_date", "exit_price", "hold_days", "pnl", "reason"]
    cols_o = cols + ["n_reductions", "partials"]
    pd.DataFrame(cur_all)[cols].to_csv("backtest_report/cmp60_cur.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(orig_all)[cols_o].to_csv("backtest_report/cmp60_orig.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(s60_all)[cols_o].to_csv("backtest_report/cmp60_60m.csv", index=False, encoding="utf-8-sig")
    cur = pd.DataFrame(cur_all)
    orig = pd.DataFrame(orig_all)
    s60 = pd.DataFrame(s60_all)
    print("\n=== 三方对比（同一批买点，笔数应相等）===")
    for lab, d in [("现版(五日线)", cur), ("原版(日线快照)", orig), ("60分K线(逐根)", s60)]:
        wd = d[d.pnl > 0]
        print(f"{lab:16} 笔数={len(d)} 胜率={round((d.pnl>0).mean()*100,1)}% 盈亏比={pf(d)} "
              f"中位={round(d.pnl.median(),2)}% 均盈={round(wd.pnl.mean(),2) if len(wd) else 0}% "
              f"均亏={round(d[d.pnl<=0].pnl.mean(),2)}% 均持={round(d.hold_days.mean(),1)}天")
    print("\n=== 60分K线 按离场原因 ===")
    print(s60.groupby("reason").agg(笔数=("pnl", "size"),
          胜率=("pnl", lambda s: round((s > 0).mean() * 100, 1)),
          均值=("pnl", "mean")).round(2).sort_values("笔数", ascending=False).to_string())
    print("\n=== 60分K线 分批次数分布（n_reductions=减仓档数）===")
    print(s60["n_reductions"].value_counts().sort_index().to_string())


if __name__ == "__main__":
    main()
