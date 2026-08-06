# -*- coding: utf-8 -*-
import pandas as pd, numpy as np

def pf(d):
    w = d[d.pnl > 0].pnl.sum(); l = abs(d[d.pnl <= 0].pnl.sum())
    return round(w / l, 2) if l else 0

cur = pd.read_csv("backtest_report/dualpath_trades.csv")
orig = pd.read_csv("backtest_report/dualpath_trades_orig.csv")

print("=== 总览对比 ===")
lab = ["指标", "现有卖点(基线)", "原版卖点+分批"]
rows = [
    ("笔数", len(cur), len(orig)),
    ("胜率%", round((cur.pnl > 0).mean() * 100, 1), round((orig.pnl > 0).mean() * 100, 1)),
    ("盈亏比", pf(cur), pf(orig)),
    ("中位%", round(cur.pnl.median(), 2), round(orig.pnl.median(), 2)),
    ("均盈利%", round(cur[cur.pnl > 0].pnl.mean(), 2), round(orig[orig.pnl > 0].pnl.mean(), 2)),
    ("均亏损%", round(cur[cur.pnl <= 0].pnl.mean(), 2), round(orig[orig.pnl <= 0].pnl.mean(), 2)),
    ("平均持仓", round(cur.hold_days.mean(), 1), round(orig.hold_days.mean(), 1)),
]
for r in rows:
    print(f"{r[0]:10} {r[1]:>14} {r[2]:>14}")

print("\n=== 原版版 按离场原因 ===")
print(orig.groupby("reason").agg(笔数=("pnl", "size"), size=("pnl", "size"),
      胜率=("pnl", lambda s: round((s > 0).mean() * 100, 1)),
      均值=("pnl", "mean")).round(2).sort_values("笔数", ascending=False).to_string())

print("\n=== 原版版 分批次数分布 ===")
print(orig["n_reductions"].value_counts().sort_index().to_string())

print("\n=== 诊断: 每票 orig vs cur 笔数 (看 orig 是否丢 entry) ===")
cc = cur.groupby("code").size()
co = orig.groupby("code").size()
allc = sorted(set(cc.index) | set(co.index))
diffs = []
for c in allc:
    a = int(co.get(c, 0)); b = int(cc.get(c, 0))
    if a != b:
        diffs.append((c, a, b))
print("笔数不同的票:", len(diffs), "/", len(allc))
for c, a, b in sorted(diffs, key=lambda x: x[2] - x[1], reverse=True)[:12]:
    print(f"  {c}: orig={a} cur={b} (差 {a-b})")

print("\n=== 诊断: orig 是否有 exit_date <= buy_date 的倒挂 ===")
bd = pd.to_datetime(orig["buy_date"]); ed = pd.to_datetime(orig["exit_date"])
bad = orig[bd >= ed]
print("exit<=buy 的异常笔数:", len(bad))
if len(bad):
    print(bad[["code", "buy_date", "exit_date", "hold_days", "reason", "n_reductions"]].head(8).to_string())

print("\n=== 诊断: orig 最长单笔持仓(是否卡死) ===")
print("hold_days 分布:", orig["hold_days"].describe().round(1).to_dict())
