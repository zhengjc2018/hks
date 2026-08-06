# -*- coding: utf-8 -*-
"""
全市场信号宇宙扫描器 —— 一次性筛出「会响 ④/⑤ 信号的票 + 信号发生的时间段」并落盘。
================================================================
目的：后续回测只加载本子集，不必从 5000+ 只逐个拉 15分 碰信号。
口径：逐字复用 backtest_dualpath 的状态机(seq_state_at)，与回测完全一致，不另立逻辑。

两阶段（省资源）：
  ① 仅拉【日线】，算 weekly/daily 前置(wk_ma_ok)。若整段窗口 wk_ma_ok 恒为 False，
     则路径必为 none、绝不可能触发 → 直接记录 0 信号跳过（数学安全剪枝，不漏真信号）。
  ② 通过①者再拉【60分+15分】，跑完整 seq_state_at 扫描，记录全部信号日与路径。

健壮性：
  - 断点续跑：已写入 CSV 的 code 自动跳过（sig_count=-1 的错误项会重试）。
  - 每 50 只即时落盘 CSV + JSON，崩溃最多丢当前一只。
  - 单只 try/except，坏票记 -1 不中断。

输出：
  backtest_report/signal_universe.csv      列: code,market,name,sig_count,first_sig,last_sig,paths
  backtest_report/signal_universe_dates.json   {code: [信号日...]}  供「时间段」分析
================================================================
"""
from __future__ import annotations
import os
import sys
import json
import argparse
import time
import pandas as pd

import backtest_dualpath as B
from tdx_source import Period, available, quotes

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report")
CSV_PATH = os.path.join(OUT_DIR, "signal_universe.csv")
JSON_PATH = os.path.join(OUT_DIR, "signal_universe_dates.json")

# 标准 A 股号段（沪 market=1 / 深 market=0）
PREFIXES = {
    "1": ["600", "601", "603", "605", "688"],
    "0": ["000", "001", "002", "003", "300", "301"],
}

# 日线前置所需最少根数（weekly MA40 需≈280根前置；<250 视为无法判定直接排除）
MIN_DAILY = 250


def gen_codes():
    out = []
    for mkt, pres in PREFIXES.items():
        for p in pres:
            for suf in range(0, 1000):
                out.append((mkt, f"{p}{suf:03d}"))
    return out


def daily_pre(market, code):
    """阶段①：仅日线，判 wk_ma_ok 是否在整段有 True。返回 (ok, daily_with_indicators)。"""
    daily = B.fetch(market, code, Period.DAILY, 1200)
    if daily is None or len(daily) < MIN_DAILY:
        return False, None
    daily = B.daily_signals(daily.copy())
    if bool(daily["wk_ma_ok"].any()):
        return True, daily
    return False, None


def scan(market, code, daily):
    """阶段②：拉 60分+15分，跑完整信号扫描，返回 [(信号日, path), ...]。"""
    m60 = B.fetch(market, code, Period.MIN_60, 3000)
    m15 = B.fetch(market, code, Period.MIN_15, 8000)
    if m15 is None or len(m15) < 20:
        return []
    daily2, m60_daily, m60_pb, m15_raw = B.prep_indicators(daily, m60, m15)
    if not m15_raw:
        return []
    min_day = min(pd.to_datetime(d) for d in m15_raw.keys())
    dates = daily2["date"].tolist()
    first_i = next((i for i, d in enumerate(dates) if d >= min_day), None)
    if first_i is None:
        return []
    n = len(daily2)
    sigs = []
    i = first_i
    while i < n:
        _, path, entry = B.seq_state_at(daily2, m60_daily, m60_pb, m15_raw, dates, i)
        if entry and path in ("main", "early"):
            sigs.append((str(dates[i])[:10], path))
        i += 1
    return sigs


def enrich_names(rows):
    """用 quotes 补全股票名称（非致命，失败留空）。"""
    try:
        df = pd.DataFrame(rows)
        need = df[df.get("name", "").fillna("") == ""]["code"].astype(str).tolist()
        if not need:
            return
        for i in range(0, len(need), 80):
            batch = need[i:i + 80]
            try:
                q = quotes(batch)
                if q is None or len(q) == 0:
                    continue
                code_col = "code" if "code" in q.columns else (q.columns[0])
                name_col = "name" if "name" in q.columns else (q.columns[1] if len(q.columns) > 1 else None)
                if name_col is None:
                    continue
                qmap = dict(zip(q[code_col].astype(str), q[name_col].astype(str)))
                for c in batch:
                    if c in qmap and qmap[c]:
                        df.loc[df["code"].astype(str) == c, "name"] = qmap[c]
            except Exception as e:
                print(f"  [name] batch err: {e}")
                break
        rows[:] = df.to_dict("records")
    except Exception as e:
        print(f"  [name] skip: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="仅扫前 N 只(调试冒烟)")
    ap.add_argument("--reset", action="store_true", help="清空旧输出重跑")
    args = ap.parse_args()

    if not available():
        print("[FATAL] 通达信源不可用(available()=False)。请确认本机通达信已开。")
        sys.exit(2)
    os.makedirs(OUT_DIR, exist_ok=True)

    # 载入已有结果（断点续跑）
    rows = []
    if os.path.exists(CSV_PATH) and not args.reset:
        try:
            rows = pd.read_csv(CSV_PATH).to_dict("records")
            print(f"[resume] 已有 {len(rows)} 行")
        except Exception:
            rows = []
    done = {r["code"] for r in rows if r.get("sig_count", -1) != -1}  # 错误项(-1)留待重试
    dates_map = {}
    if os.path.exists(JSON_PATH):
        try:
            dates_map = json.load(open(JSON_PATH, encoding="utf-8"))
        except Exception:
            dates_map = {}

    codes = gen_codes()
    if args.limit:
        codes = codes[:args.limit]

    total = len(codes)
    t0 = time.time()
    cnt = 0
    hit = 0
    for mkt, code in codes:
        cnt += 1
        if code in done:
            continue
        try:
            pre_ok, daily = daily_pre(mkt, code)
            if not pre_ok:
                rows.append(dict(code=code, market=mkt, name="", sig_count=0,
                                 first_sig="", last_sig="", paths=""))
                done.add(code)
                continue
            sigs = scan(mkt, code, daily)
            if sigs:
                ds = [d for d, _ in sigs]
                paths = {}
                for _, p in sigs:
                    paths[p] = paths.get(p, 0) + 1
                paths_s = ",".join(f"{k}:{v}" for k, v in paths.items())
                rows.append(dict(code=code, market=mkt, name="", sig_count=len(sigs),
                                 first_sig=ds[0], last_sig=ds[-1], paths=paths_s))
                dates_map[code] = ds
                hit += 1
            else:
                rows.append(dict(code=code, market=mkt, name="", sig_count=0,
                                 first_sig="", last_sig="", paths=""))
            done.add(code)
        except Exception as e:
            print(f"  [err] {mkt}{code}: {e}")
            rows.append(dict(code=code, market=mkt, name="", sig_count=-1,
                             first_sig="", last_sig="", paths="ERR"))
            done.add(code)
            continue

        if cnt % 50 == 0:
            pd.DataFrame(rows).to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
            json.dump(dates_map, open(JSON_PATH, "w", encoding="utf-8"), ensure_ascii=False)
            el = time.time() - t0
            rate = el / max(cnt - len(done), 1)
            eta = rate * (total - cnt) / 60
            print(f"[prog] {cnt}/{total} 已命中{hit} 耗时{el/60:.1f}min ETA~{eta:.1f}min")

    # 名称补全 + 最终落盘
    enrich_names(rows)
    pd.DataFrame(rows).to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    json.dump(dates_map, open(JSON_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[done] 扫描{cnt} 命中{hit} 输出→ {CSV_PATH}")


if __name__ == "__main__":
    main()
