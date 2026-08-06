# -*- coding: utf-8 -*-
"""全库对比：现行版（五日线，全信号）vs 优化买点版（入场过滤+五日线）（信号宇宙子集 4984 只 × setup 窗口）。

设计：
- 读 signal_universe_12.csv 子集（①② 已成立的票 + setup 窗口），dtype 保前导零。
- 每只票 fetch+prep 一次（日线1200 / 60分3000 / 15分8000），在 setup 窗口并集（含前25天预热）内
  纯信号扫描 entry（5日冷却）。同一批买点，两腿同跑（卖点都是 B.run_exit 五日线，隔离变量=买点）：
    * cur  = 现行版：所有 ⑤ 信号都买（五日线出场）
    * fcur = 优化买点版：买价距 MA5 收敛（<= ENTRY_MAX_ABOVE_MA5）才买（同款五日线出场）
- 断点续跑（按 code 记录 done，即时落盘两 CSV），防崩丢进度。
输出：
  backtest_report/dualpath_trades_current_full.csv   （现行版）
  backtest_report/dualpath_trades_filtered_full.csv  （优化买点版）
  对比报告由 gen_compare_full_report.py 生成。
"""
import os, sys, json, argparse, time
import pandas as pd, numpy as np
import backtest_dualpath as B
from tdx_source import available

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report")
UNIV_CSV = os.path.join(OUT, "signal_universe_12.csv")
STATE = os.path.join(OUT, "compare_full_state.json")
CSV_CUR = os.path.join(OUT, "dualpath_trades_current_full.csv")
CSV_FCUR = os.path.join(OUT, "dualpath_trades_filtered_full.csv")
COOLDOWN = 5
DAILY_COUNT, M60_COUNT, M15_COUNT = 1200, 3000, 8000


def scan_dual(market, code, name, windows):
    daily_df = B.fetch(market, code, B.Period.DAILY, DAILY_COUNT)
    if daily_df is None or len(daily_df) < 120:
        return None, None, "日线不足"
    m60_df = B.fetch(market, code, B.Period.MIN_60, M60_COUNT)
    m15_df = B.fetch(market, code, B.Period.MIN_15, M15_COUNT)
    daily, m60_daily, m60_pb, m15_raw, m15_rsi = B.prep_indicators(daily_df, m60_df, m15_df)
    if not m15_raw:
        return None, None, "15分缺失"
    dates = daily["date"].tolist()
    # scan_set：setup 窗口并集 + 前25天预热
    date_idx = {}
    for j, d in enumerate(dates):
        key = d.strftime("%Y-%m-%d") if isinstance(d, pd.Timestamp) else str(d)[:10]
        date_idx[key] = j
    scan_set = set()
    for w in windows:
        if "~" not in w:
            continue
        s, e = w.split("~")
        si = date_idx.get(s); ei = date_idx.get(e)
        if si is None or ei is None:
            continue
        si = max(si - 25, 0)
        for j in range(si, ei + 1):
            scan_set.add(j)
    if not scan_set:
        return None, None, "窗口无重叠"
    cur, fcur = [], []
    i = min(scan_set)
    n = len(daily)
    last_entry_i = -999
    while i < n:
        if i not in scan_set:
            i += 1
            continue
        _, path, entry = B.seq_state_at(daily, m60_daily, m60_pb, m15_raw, dates, i)
        if entry and path in ("main", "early"):
            buy_idx = i
            if buy_idx >= n:
                break
            if i - last_entry_i < COOLDOWN:
                i += 1
                continue
            # cur 腿（现行版）：所有信号都买，五日线出场
            tc = B.run_exit(daily, buy_idx, path, False)
            tc["code"] = code; tc["name"] = name; tc["market"] = market
            tc["buy_date"] = str(tc["buy_date"])[:10]
            tc["exit_date"] = str(tc["exit_date"])[:10]
            cur.append(tc)
            # fcur 腿（优化买点版）：入场过滤（买价距 MA5 收敛）才买，同款五日线出场
            bp = float(daily["close"].values[buy_idx])
            m5b = float(daily["ma5"].values[buy_idx])
            if not (pd.isna(m5b) or bp > m5b * B.ENTRY_MAX_ABOVE_MA5):
                tf = B.run_exit(daily, buy_idx, path, False)
                tf["code"] = code; tf["name"] = name; tf["market"] = market
                tf["buy_date"] = str(tf["buy_date"])[:10]
                tf["exit_date"] = str(tf["exit_date"])[:10]
                fcur.append(tf)
            last_entry_i = i
        i += 1
    return cur, fcur, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（调试）")
    ap.add_argument("--codes", type=str, default=None, help="逗号分隔指定代码，如 600519,000725")
    ap.add_argument("--reset", action="store_true", help="清空续跑状态重来")
    args = ap.parse_args()
    if not available():
        print("[FATAL] 通达信源不可用"); sys.exit(2)
    os.makedirs(OUT, exist_ok=True)
    if args.reset:
        if os.path.exists(STATE):
            try:
                os.remove(STATE)
                print("[reset] 清空续跑状态")
            except Exception as e:
                print(f"[reset] 旧状态删除被沙箱拦截({e})，将直接覆盖")

    df = pd.read_csv(UNIV_CSV, dtype={"code": str, "market": str, "windows": str})
    df = df[df["n_windows"] > 0].copy()
    if args.codes:
        want = set(args.codes.split(","))
        df = df[df["code"].isin(want)]
    tasks = [(r["market"], r["code"], r["name"], [w for w in str(r["windows"]).split(";") if w])
             for _, r in df.iterrows()]
    if args.limit:
        tasks = tasks[:args.limit]
    print(f"[run] 子集 {len(tasks)} 只 | 双版本同跑(cur=现行版全信号, fcur=优化买点版入场过滤)")

    state = {"done": [], "cur": [], "fcur": []}
    if (not args.reset) and os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            try:
                state = json.load(open(STATE + ".bak"))
                print("[warn] STATE 损坏，已用 .bak 恢复（仅损失上一批未落盘进度）")
            except Exception:
                state = {"done": [], "cur": [], "fcur": []}
                print("[warn] STATE 与 .bak 均损坏，将从头跑")
    if "fcur" not in state:  # 兼容旧格式 STATE（只有 cur/orig）
        state["fcur"] = []
    done = set(state.get("done", []))

    t0 = time.time()
    cnt = 0
    for market, code, name, windows in tasks:
        if code in done:
            continue
        try:
            cur, fcur, err = scan_dual(market, code, name, windows)
        except Exception as e:
            print(f"  [skip] {code} {name}: {e}")
            done.add(code)
            continue
        if err:
            print(f"  [skip] {code} {name}: {err}")
            done.add(code)
            continue
        state["cur"].extend(cur or [])
        state["fcur"].extend(fcur or [])
        done.add(code)
        cnt += 1
        if cnt % 25 == 0:
            save_state({"done": list(done), "cur": state["cur"], "fcur": state["fcur"]})
            print(f"  [{len(done)}/{len(tasks)}] 已扫 {code} | cur={len(state['cur'])} fcur={len(state['fcur'])} | {time.strftime('%H:%M:%S')}")
    # 终写
    save_state({"done": list(done), "cur": state["cur"], "fcur": state["fcur"]})
    cur_all = state["cur"]; fcur_all = state["fcur"]
    cols = ["code", "name", "market", "path", "case", "buy_date", "buy_price",
            "exit_date", "exit_price", "hold_days", "pnl", "reason"]
    pd.DataFrame(cur_all)[cols].to_csv(CSV_CUR, index=False, encoding="utf-8-sig")
    pd.DataFrame(fcur_all)[cols].to_csv(CSV_FCUR, index=False, encoding="utf-8-sig")
    print(f"\n[done] 总扫 {len(done)} 只 | cur={len(cur_all)} 笔 fcur={len(fcur_all)} 笔 | 耗时 {round((time.time()-t0)/60,1)}min")
    print(f"  CSV : {CSV_CUR}\n        {CSV_FCUR}")


def save_state(obj):
    """原子写：先写 .tmp 再 os.replace，避免落盘中途崩溃导致 STATE 半截损坏；
    同时保留 .bak 作为上一完整快照，供损坏时恢复。"""
    if os.path.exists(STATE):
        try:
            open(STATE + ".bak", "wb").write(open(STATE, "rb").read())
        except Exception:
            pass
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, STATE)


if __name__ == "__main__":
    main()
