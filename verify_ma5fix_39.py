# -*- coding: utf-8 -*-
"""验证 MA5 三重确认方向 bug 修复：39 只 POOL 跑 cur/fcur 两腿，量化 B 路离场变化。"""
import pandas as pd
import backtest_dualpath as B
import backtest_orig_vs_current as M

univ = pd.read_csv(M.UNIV_CSV, dtype={"code": str, "market": str, "windows": str})
cur_all, fcur_all = [], []
for market, code, name in B.POOL:
    r = univ[univ["code"] == code]
    if r.empty:
        print(f"[skip] {code} {name} 不在信号宇宙")
        continue
    windows = [w for w in str(r.iloc[0]["windows"]).split(";") if w]
    c, f, err = M.scan_dual(market, code, name, windows)
    if err:
        print(f"[skip] {code} {name}: {err}")
        continue
    cur_all += c
    fcur_all += f
    print(f"  {code} {name}: cur={len(c)} fcur={len(f)}")

cur = pd.DataFrame(cur_all)
fcur = pd.DataFrame(fcur_all)


def rep(lab, d):
    if d.empty:
        print(f"{lab}: 空"); return
    a = d[d.case == "A"]
    b = d[d.case == "B"]
    wins = d[d.pnl > 0]
    losses = d[d.pnl <= 0]
    pf = wins.pnl.sum() / abs(losses.pnl.sum()) if len(losses) and losses.pnl.sum() != 0 else 999
    print(f"\n===== {lab} n={len(d)} =====")
    print(f"  总: 胜率{round((d.pnl>0).mean()*100,1)}% 盈亏比{round(pf,2)} 中位{round(d.pnl.median(),2)}% 均盈{round(wins.pnl.mean(),2)}% 均亏{round(losses.pnl.mean(),2)}% 均持{round(d.hold_days.mean(),1)}天")
    print(f"  case A: {len(a)} 笔 | case B: {len(b)} 笔")
    if len(b):
        print(f"  B路离场: {b.reason.value_counts().to_dict()}")
        print(f"  B路 ma5_B: 胜率{round((b[b.reason=='ma5_B'].pnl>0).mean()*100,1) if len(b[b.reason=='ma5_B']) else '-'}% 均亏{round(b[b.reason=='ma5_B'].pnl.mean(),2) if len(b[b.reason=='ma5_B']) else '-'}%")
        print(f"  B路 hard_stop: {len(b[b.reason=='hard_stop'])} 笔 均亏{round(b[b.reason=='hard_stop'].pnl.mean(),2) if len(b[b.reason=='hard_stop']) else '-'}%")


rep("现行版(修复后)", cur)
rep("优化买点版(修复后)", fcur)
