import easy_tdx
print("easy_tdx version:", getattr(easy_tdx,"__version__","?"))
from easy_tdx.mac.client import MacClient
c = MacClient.from_best_host()
# 列一下 MacClient 所有看起来和 成分/板块/列表 相关的方法
meths = [m for m in dir(c) if not m.startswith("_")]
import re
rel = [m for m in meths if re.search(r"(stock|list|block|index|comp|secu|member|constit|bars|kline)", m, re.I)]
print("RELATED METHODS:")
for m in rel: print("  ", m)
print("\nALL METHODS (sample):")
for m in meths[:60]: print("  ", m)
# 试 block 模块
try:
    import easy_tdx.block as b
    print("\nblock module OK, attrs:", [x for x in dir(b) if not x.startswith("_")])
except Exception as e:
    print("\nblock import fail:", repr(e))
