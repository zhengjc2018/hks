# -*- coding: utf-8 -*-
"""看清 get_board_list() 返回的 DataFrame 结构，决定全量探测怎么遍历。"""
import sys
OUT = r"C:\Users\natsu\WorkBuddy\2026-07-20-13-17-12\apanel\_probe_struct.txt"
out = open(OUT, "w", encoding="utf-8")
def log(*a):
    s = " ".join(str(x) for x in a)
    out.write(s + "\n")
    print(s)

from easy_tdx.mac.client import MacClient
from easy_tdx.mac.enums import Period, Adjust
c = MacClient.from_best_host()

boards = c.get_board_list()
log("TYPE:", type(boards).__name__, "ROWS:", len(boards))
log("COLUMNS:", list(boards.columns))
log("DTYPES:", {k: str(v) for k, v in boards.dtypes.to_dict().items()})

log("\n--- head 12 ---")
for _, row in boards.head(12).iterrows():
    log(dict(row))

# 从列名找 code 字段
code_col = None
for cand in ("code", "bkcode", "bk_code", "symbol", "板块代码", "code_str"):
    if cand in boards.columns:
        code_col = cand
        break
log("\nCODE_COL:", code_col)
if code_col is None:
    log("NO CODE COL, columns=", list(boards.columns))
else:
    code0 = str(boards.iloc[0][code_col])
    log("CODE0:", code0, "NAME0:", boards.iloc[0].get("name", boards.iloc[0].get("板块名称", "?")))
    try:
        df = c.get_stock_kline(1, code0, Period.DAILY, 0, 5, Adjust.NONE)
        log("KLINE_OK rows=%d cols=%s" % (0 if df is None else len(df), None if df is None else list(df.columns)))
    except Exception as e:
        log("KLINE_ERR", type(e).__name__, str(e)[:160])
    try:
        r = c.get_board_members(code0)
        n = len(r) if hasattr(r, "__len__") else "?"
        log("MEMBERS_OK len=", n)
        if hasattr(r, "__len__") and n:
            row = r.iloc[0].to_dict() if hasattr(r, "iloc") else r[0]
            log("  member sample:", row)
    except Exception as e:
        log("MEMBERS_ERR", type(e).__name__, str(e)[:160])

out.close()
