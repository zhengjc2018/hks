# -*- coding: utf-8 -*-
"""抽 20 只真实样本，逐票 scan_dual 计时，算真实均值与 ETA，并标出慢票。"""
import time
import pandas as pd
import backtest_dualpath as B
from tdx_source import available
from backtest_orig_vs_current import scan_dual, UNIV_CSV

df = pd.read_csv(UNIV_CSV, dtype={"code": str, "market": str, "windows": str})
sample = df.iloc[200:220]  # 取中段 20 只，避开头部大票
times = []
slow = []
for _, r in sample.iterrows():
    code = r["code"]; market = r["market"]; name = r["name"]
    windows = [w for w in str(r["windows"]).split(";") if w]
    t = time.time()
    try:
        cur, orig, err = scan_dual(market, code, name, windows, True)  # 乙用 entry_filter=True
    except Exception as e:
        print(f"  [ERR] {code} {name}: {e}")
        times.append((code, None, str(e)[:30]))
        continue
    dt = time.time() - t
    times.append((code, round(dt, 2), len(cur or [])))
    if dt > 6:
        slow.append((code, round(dt, 2)))

total = sum(t[1] for t in times if t[1] is not None)
n = len([t for t in times if t[1] is not None])
avg = total / n if n else 0
print(f"\n=== 20 只样本（entry_filter=True，乙口径）===")
print(f"有效 {n} 只 | 合计 {total:.1f}s | 均值 {avg:.2f}s/只")
print(f"=> 全库 4984 只 ETA ≈ {avg*4984/3600:.1f} 小时")
print(f"慢票(>6s): {slow}")
srt = sorted([t for t in times if t[1] is not None], key=lambda x: -x[1])[:3]
print(f"最慢: {srt}")
