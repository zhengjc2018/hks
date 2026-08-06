# -*- coding: utf-8 -*-
"""全量探测 560 个板块的：
1) 板块指数日K 是否可拉 (get_stock_kline market=1)
2) 板块成员股是否可拉 (get_board_members)
统计覆盖度 + 去重个股数，决定「热门板块符合条件个股」回测能否落地。
注：板块列表与成员均为【当前(约2026-07)快照】，历史成分股 easy_tdx 不提供——同成分股池的时点偏差。
"""
import sys, os, json, time
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "probe_board_coverage.json")
LOGF = os.path.join(HERE, "probe_board_coverage.log")

def log(*a):
    s = " ".join(str(x) for x in a)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(s + "\n")
    print(s, flush=True)

from easy_tdx.mac.client import MacClient
from easy_tdx.mac.enums import Period, Adjust

c = MacClient.from_best_host()
boards = c.get_board_list()
log("BOARDS:", len(boards))

results = []
unique_stocks = set()
kline_ok = 0
members_ok = 0

for i, (_, b) in enumerate(boards.iterrows()):
    code = str(b["code"]); name = str(b["name"])
    rec = {"code": code, "name": name, "kline_ok": False, "kline_rows": 0,
           "members_ok": False, "member_count": 0}
    # 1) 板块指数 K 线
    try:
        df = c.get_stock_kline(1, code, Period.DAILY, 0, 5, Adjust.NONE)
        if df is not None and len(df) > 0:
            rec["kline_ok"] = True; rec["kline_rows"] = int(len(df)); kline_ok += 1
    except Exception as e:
        rec["kline_err"] = str(e)[:120]
    # 2) 板块成员
    try:
        r = c.get_board_members(code)
        if r is not None and hasattr(r, "__len__") and len(r) > 0:
            rec["members_ok"] = True; rec["member_count"] = int(len(r)); members_ok += 1
            for _, m in r.iterrows():
                unique_stocks.add((int(m["market"]), str(m["code"])))
    except Exception as e:
        rec["members_err"] = str(e)[:120]
    results.append(rec)
    if (i + 1) % 20 == 0:
        log(f"  {i+1}/{len(boards)} kline_ok={kline_ok} members_ok={members_ok} unique_stocks={len(unique_stocks)}")

summary = {
    "total_boards": int(len(boards)),
    "kline_ok": int(kline_ok),
    "members_ok": int(members_ok),
    "unique_member_stocks": int(len(unique_stocks)),
    "avg_member_count": round(sum(r["member_count"] for r in results if r["members_ok"]) / max(1, members_ok), 1),
}
json.dump({"summary": summary, "details": results},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
log("DONE " + json.dumps(summary, ensure_ascii=False))
