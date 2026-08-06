import re
from easy_tdx.mac.client import MacClient
c = MacClient.from_best_host()
boards = c.get_board_list()
def show(df, pat, label):
    m = df[df["name"].astype(str).str.contains(pat, regex=True, na=False)]
    print(f"\n=== {label} ({len(m)}) ===")
    for _,r in m.iterrows():
        print(f"  m={r['market']} code={r['code']} name={r['name']}")
show(boards, r"300", "含300")
show(boards, r"500", "含500")
show(boards, r"沪深|中证|指数", "含沪深/中证/指数")
show(boards, r"上证50|科创|创业|深证|沪指|深成", "宽基指数")
