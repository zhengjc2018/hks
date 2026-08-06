# -*- coding: utf-8 -*-
"""
①② 级宇宙扫描（周线/日线，零 60分/15分 拉取）—— 快版
================================================================
目的：在「全市场 5000+ 只」里，先用**最便宜的①②信号**（周MA + 日MA/量，
      全部由日线 resample 得到，不碰 60分/15分）筛出"①② 曾同时成立"的票，
      并记录其 setup 窗口（连续成立日区间），落盘为候选宇宙子集。
后续做 ③④⑤ 全信号回测时，只扫描这个子集（且只扫其 setup 窗口），
不必每次从 5000+ 只挨个探测，大幅省资源。

为什么安全（不漏真信号）：
  信号链 ①→②→③→④→⑤，③ 需 armed（依赖 ① 路径成立）。若 ①② 从未成立，
  ③ 永不能 armed → ⑤ 永不能 fire → 永不成交。因此"①② 曾成立"是"可能成交"
  的充分超集，存这个子集不会漏掉任何未来可能成交的票。

与 backtest_dualpath.py 同口径：
  - ①② 判定逐字复用 daily_signals() 算出的列（wk_ma_ok / wk_rsi / cond1_* / cond2_*），
    与 seq_state_at() 第 i 天处的 path/c1/c2 逻辑一致。
  - 仅 fetch 日线（DAILY_COUNT 根），不 fetch 60分/15分。

运行：
  cd C:/Users/natsu/WorkBuddy/2026-07-20-13-17-12/apanel
  python screen_universe_12.py                # 全市场快扫（断点续跑，后台跑）
  python screen_universe_12.py --limit 600     # 前 600 个号段冒烟
  python screen_universe_12.py --codes 600519,000725  # 指定票验收
  python screen_universe_12.py --reset         # 清空续跑状态重来
输出：
  backtest_report/signal_universe_12.csv   # 有 setup 的票（候选宇宙子集）
  backtest_report/signal_universe_12_state.json  # 续跑状态（含全部结果，完赛后转 CSV）
================================================================
"""
from __future__ import annotations
import os
import sys
import argparse
import json
import pandas as pd
import numpy as np

# 复用项目信号口径（同目录）
import backtest_dualpath as B
from tdx_source import kline, available, Period, Adjust

DAILY_COUNT = 900          # 日线根数：覆盖 2024.07 以前即可（≈3.6y）
MIN_SETUP_DATE = "2024-07-01"   # setup 窗口只记此日之后（对齐 15分最早覆盖 2024-07-15）
WARMUP_BARS = 250          # 周MA40 需 ~40 周≈200 日；少于此视为数据不足
FLUSH_EVERY = 25           # 每处理 N 只刷新一次续跑状态

# A 股号段（沪 6xxxx / 深 0,3xxxx）
SEGMENTS = [
    ("1", ["600", "601", "603", "605", "688"]),
    ("0", ["000", "001", "002", "003", "300", "301"]),
]


def enumerate_codes():
    """生成全市场候选代码（含大量无效号，kline 返回空会自动跳过）。"""
    out = []
    for mkt, pres in SEGMENTS:
        for pre in pres:
            for i in range(1000):
                out.append((mkt, f"{pre}{i:03d}", f"{pre}{i:03d}"))
    return out


def check_12(daily):
    """逐日判定 ①② 是否同时成立（与 seq_state_at 第 i 天同口径）。
    返回 list[(date_str, path)]，仅含 ①② 同时成立日。"""
    n = len(daily)
    dates = daily["date"].tolist()
    res = []
    for i in range(n):
        r = daily.iloc[i]
        wk_ma_ok = bool(r["wk_ma_ok"]) if pd.notna(r["wk_ma_ok"]) else False
        wk_rsi = float(r["wk_rsi"]) if pd.notna(r["wk_rsi"]) else 0.0
        if not wk_ma_ok:
            continue  # ① 不成立（周MA20 未站上 MA40 或周MA20下行）→ 任一路径 c1=False
        if wk_rsi > 50:
            path = "main"
            c1 = bool(r["cond1_main"])
            c2 = bool(r["cond2_main"])
        else:
            path = "early"
            c1 = bool(r["cond1_early"])
            c2 = bool(r["cond2_early"])
        if c1 and c2:
            d = dates[i]
            dstr = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            res.append((dstr, path))
    return res


def compress_windows(date_strs, gap_days=7):
    """把连续交易日压缩成 [start~end] 区间（gap<=gap_days 视为同一窗口）。"""
    if not date_strs:
        return []
    ts = sorted(pd.to_datetime(x) for x in date_strs)
    wins = []
    s = ts[0]
    prev = ts[0]
    for d in ts[1:]:
        if (d - prev).days <= gap_days:
            prev = d
        else:
            wins.append((s, prev))
            s = d
            prev = d
    wins.append((s, prev))
    return [(a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")) for a, b in wins]


def scan_one(market, code, name):
    """返回 dict（含 setup 信息）或 None（无数据/不足）。"""
    daily_df = B.fetch(market, code, Period.DAILY, DAILY_COUNT)
    if daily_df is None or len(daily_df) < WARMUP_BARS:
        return None
    daily = B.daily_signals(daily_df.copy())
    hits = check_12(daily)
    if not hits:
        return None
    # 只保留 >= MIN_SETUP_DATE 的 setup 日（对齐 15分覆盖，避免存无用的早期窗口）
    hits = [(d, p) for d, p in hits if d >= MIN_SETUP_DATE]
    if not hits:
        return None
    dates = [d for d, _ in hits]
    paths = [p for _, p in hits]
    wins = compress_windows(dates)
    if not wins:
        return None
    return {
        "market": market,
        "code": code,
        "name": name,
        "setup_days": len(hits),
        "n_windows": len(wins),
        "first_setup": wins[0][0],
        "last_setup": wins[-1][1],
        "paths": ",".join(sorted(set(paths))),
        "windows": ";".join(f"{a}~{b}" for a, b in wins),
    }


def load_state(state_path):
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            return set(st.get("done", [])), st.get("rows", [])
        except Exception:
            pass
    return set(), []


def save_state(state_path, done, rows):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done), "rows": rows}, f, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个候选代码（冒烟用）")
    ap.add_argument("--codes", type=str, default=None, help="指定票，逗号分隔，如 600519,000725")
    ap.add_argument("--reset", action="store_true", help="清空续跑状态，从头开始")
    args = ap.parse_args()

    if not available():
        print("[FATAL] 通达信源不可用(available()=False)。请确认本机通达信客户端已开。")
        sys.exit(2)

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "backtest_report")
    os.makedirs(out_dir, exist_ok=True)
    state_path = os.path.join(out_dir, "signal_universe_12_state.json")
    csv_path = os.path.join(out_dir, "signal_universe_12.csv")

    if args.reset:
        # 沙箱下 os.remove 会被安全删除拦截(回收站不可用即抛错)；直接覆写空状态更稳
        save_state(state_path, set(), [])
        print("[reset] 已清空续跑状态")

    # 构造任务列表
    if args.codes:
        tasks = []
        for c in args.codes.split(","):
            c = c.strip()
            if not c:
                continue
            code = c[-6:]
            market = "0" if code.startswith(("0", "3", "2")) else "1"
            tasks.append((market, code, code))
    else:
        tasks = enumerate_codes()
        if args.limit:
            tasks = tasks[: args.limit]

    done, rows = load_state(state_path)
    tasks = [(m, c, n) for (m, c, n) in tasks if c not in done]
    print(f"[run] 待处理 {len(tasks)} 只（已落盘 {len(done)} 只）| ①②-only 快扫（不拉 60分/15分）")

    n_scanned = 0
    n_valid = 0
    n_with_setup = 0
    for idx, (market, code, name) in enumerate(tasks):
        try:
            row = scan_one(market, code, name)
        except Exception as e:
            row = None
            print(f"  [err] {market}{code}: {e}")
        done.add(code)
        n_scanned += 1
        if row is not None:
            rows.append(row)
            n_with_setup += 1
            n_valid += 1
        else:
            n_valid += 1  # 有数据但无 setup 也算 valid 扫描过
        if (idx + 1) % FLUSH_EVERY == 0:
            save_state(state_path, done, rows)
            print(f"  ...{idx+1}/{len(tasks)} 已扫 | 有①② setup: {n_with_setup}")

    save_state(state_path, done, rows)

    # 写 CSV
    if rows:
        cols = ["market", "code", "name", "setup_days", "n_windows",
                "first_setup", "last_setup", "paths", "windows"]
        df = pd.DataFrame(rows)[cols].sort_values(
            ["setup_days", "code"], ascending=[False, True]).reset_index(drop=True)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        total_windows = int(df["n_windows"].sum())
        print(f"\n[done] 扫描 {n_scanned} 只 | 有数据 {n_valid} | ①② 候选宇宙 {len(df)} 只 | setup窗口合计 {total_windows}")
        print(f"  CSV : {csv_path}")
    else:
        print(f"\n[done] 扫描 {n_scanned} 只，无 ①② setup（极小概率：检查 MIN_SETUP_DATE / 通达信数据）")


if __name__ == "__main__":
    main()
