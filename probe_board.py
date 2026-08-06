import re
from easy_tdx.mac.client import MacClient
c = MacClient.from_best_host()
boards = c.get_board_list()
print("TYPE:", type(boards), "LEN:", len(boards))
print("SAMPLE[0:3]:", boards[:3])
# 找沪深300 / 中证500 相关板块
hits = []
for b in boards:
    s = str(b)
    if re.search(r"沪深300|中证500|沪深300|300指数|500指数|中证?500", s):
        hits.append(b)
print(f"\nHITS ({len(hits)}):")
for b in hits[:50]:
    print("  ", b)
