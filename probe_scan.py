# -*- coding: utf-8 -*-
"""用真实 scan_dual 测单票全管线耗时（含扫描循环 seq_state_at），不碰 STATE。"""
import time, sys
import pandas as pd
import backtest_dualpath as B
from tdx_source import available
from backtest_orig_vs_current import scan_dual, UNIV_CSV

market, code, name = (sys.argv[1:4] + ["sh", "600519", "贵州茅台"])[:3]
df = pd.read_csv(UNIV_CSV, dtype={"code": str, "market": str, "windows": str})
row = df[df["code"] == code]
if row.empty:
    print("univ 中无此票"); sys.exit(1)
r = row.iloc[0]
windows = [w for w in str(r["windows"]).split(";") if w]
print(f"票={code} 窗口数={len(windows)}")

for ef in (False, True):
    t = time.time()
    cur, orig, err = scan_dual(market, code, name, windows, ef)
    dt = time.time() - t
    nc = len(cur or []); no = len(orig or [])
    print(f"  entry_filter={ef}: 耗时 {dt:.2f}s | cur={nc}笔 orig={no}笔 | 平均每笔 {dt/max(nc+no,1)*1000:.1f}ms")
