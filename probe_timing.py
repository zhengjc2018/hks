# -*- coding: utf-8 -*-
"""单票计时探针：拆出 取数 / prep / 出场计算 三段耗时，定位全库慢的根因。"""
import time, sys
import backtest_dualpath as B
from tdx_source import available

market, code, name = (sys.argv[1:4] + ["sh", "600519", "贵州茅台"])[:3]
if not available():
    print("[FATAL] 通达信源不可用"); sys.exit(2)

t = time.time()
d = B.fetch(market, code, B.Period.DAILY, 1200); td = time.time() - t
t = time.time()
m6 = B.fetch(market, code, B.Period.MIN_60, 3000); t6 = time.time() - t
t = time.time()
m1 = B.fetch(market, code, B.Period.MIN_15, 8000); t1 = time.time() - t
print(f"[取数] daily={td:.2f}s m60={t6:.2f}s m15={t1:.2f}s | 合计取数={td+t6+t1:.2f}s")
print(f"       daily行={len(d)} m60行={len(m6)} m15行={len(m1)}")

t = time.time()
daily, m60_daily, m60_pb, m15_raw, m15_rsi = B.prep_indicators(d, m6, m1)
print(f"[prep] {time.time()-t:.2f}s | m15_raw={len(m15_raw)}")

# 找一个 entry 测出场计算
dates = daily["date"].tolist()
for i in range(len(daily) - 60, len(daily) - 5):
    _, path, entry = B.seq_state_at(daily, m60_daily, m60_pb, m15_raw, dates, i)
    if entry and path in ("main", "early"):
        bi = i + 1
        t = time.time()
        tc = B.run_exit(daily, bi, path, False); t_a = time.time() - t
        t = time.time()
        to = B.run_exit_orig(daily, m60_daily, m15_rsi, bi, path); t_b = time.time() - t
        print(f"[出场计算] run_exit={t_a*1000:.1f}ms run_exit_orig={t_b*1000:.1f}ms (共 {t_a+t_b:.3f}s)")
        break
else:
    print("[出场计算] 近60日未找到 entry，跳过")

print(f"[结论] 单票总耗时≈取数{td+t6+t1:.1f}s + prep0.59s + 出场（每entry约{t_a+t_b:.3f}s）")
