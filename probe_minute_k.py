"""探测 easy_tdx 分钟级K线 拉取能力（仅1只样例，快速验证）。
目的：确认能否离线把 60分/15分 K线 下载到本地 cache，供后续精确层回测使用。
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from easy_tdx.mac.client import MacClient
from easy_tdx.mac.enums import Period, Adjust

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report", "cache", "minute_probe")
os.makedirs(OUT, exist_ok=True)

def log(*a):
    print(*a, flush=True)

def main():
    c = MacClient.from_best_host()
    c.ensure_connected()
    log("CONNECTED")
    # 用一只代表性个股（华勤 603296）探测
    market, code = 1, "603296"
    for period, name in [(Period.MIN_60, "60min"), (Period.MIN_15, "15min"), (Period.MIN_5, "5min"), (Period.MIN_1, "1min")]:
        try:
            t0 = time.time()
            # count=0 尝试全量；某些服务器有上限，这里先试 800 根
            df = c.get_stock_kline(market, code, period, 0, 800, Adjust.QFQ)
            dt = time.time() - t0
            if df is not None and len(df) > 0:
                log(f"[{name}] OK rows={len(df)} cost={dt:.2f}s head={df['datetime'].iloc[0]} tail={df['datetime'].iloc[-1]}")
                df.to_csv(os.path.join(OUT, f"probe_{code}_{name}.csv"), index=False)
            else:
                log(f"[{name}] EMPTY/None cost={dt:.2f}s")
        except Exception as e:
            log(f"[{name}] ERR: {repr(e)[:200]}")
    log("DONE")

if __name__ == "__main__":
    main()
