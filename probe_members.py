from easy_tdx.mac.client import MacClient
c = MacClient.from_best_host()
# 先用一个已知存在的行业板块探签名
trials = [(1,"880958"), ("880958",), (1,"000300"), ("000300",), (1,"000905"), ("000905"), (1,"399905"), ("399905",)]
for args in trials:
    try:
        r = c.get_board_members(*args)
        n = len(r) if hasattr(r,'__len__') else '?'
        print(args, "-> OK type", type(r).__name__, "len", n)
        if hasattr(r,'__len__') and n and n>0:
            row = r.iloc[0].to_dict() if hasattr(r,'iloc') else (r[0] if isinstance(r,list) else r)
            print("    sample:", row)
    except Exception as e:
        print(args, "ERR", repr(e)[:140])
