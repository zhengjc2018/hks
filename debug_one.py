import traceback
import backtest_dualpath as B
from tdx_source import available

print("available:", available())
market, code, name = 'sz', '002179', '中航光电'
try:
    daily_df = B.fetch(market, code, B.Period.DAILY, 1200)
    m60_df = B.fetch(market, code, B.Period.MIN_60, 3000)
    m15_df = B.fetch(market, code, B.Period.MIN_15, 8000)
    daily, m60_daily, m60_pb, m15_raw, m15_rsi = B.prep_indicators(daily_df, m60_df, m15_df)
    m60 = B.min60_signals(m60_df.copy())
    m60["date"] = m60["datetime"].dt.strftime("%Y-%m-%d")
    dates = daily["date"].tolist()
    n = len(daily); last = -999; i = 0
    while i < n:
        _, path, entry = B.seq_state_at(daily, m60_daily, m60_pb, m15_raw, dates, i)
        if entry and path in ("main", "early"):
            b = i + 1
            if i - last < 5:
                i += 1; continue
            print("ENTRY at", i, dates[i], "path", path)
            tc = B.run_exit(daily, b, path, False)
            print("run_exit type:", type(tc), tc if isinstance(tc, (list, dict)) else "")
            t6 = B.run_exit_60m(daily, m60, m15_rsi, b, path)
            print("run_exit_60m type:", type(t6), t6 if isinstance(t6, (list, dict)) else "")
            for fl, te, pc, lo, tag in [(-0.05,60,True,True,"vA"),(-0.05,60,False,True,"vB"),
                                        (-0.05,999,True,True,"vC"),(-0.08,60,True,True,"vD")]:
                tu = B.run_exit_ultra(daily, m60, m15_rsi, b, path, fl, te, pc, lo)
                print("ultra", tag, "ok", tu["reason"], tu["pnl"])
            break
        i += 1
    else:
        print("NO ENTRY FOUND")
except Exception:
    traceback.print_exc()
