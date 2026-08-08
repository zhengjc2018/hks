# -*- coding: utf-8 -*-
"""次日高开排序模型训练（纯 numpy 逻辑回归，无 sklearn 依赖）。

A 方案：桌面/云端离线训练，产物是 gap_model.json，App 只用 numpy 做
sigmoid(w·x+b) 推理。线上 gap_pick.py 保留全部规则硬过滤，模型只负责 TopN 排序；
没有模型时自动回退原规则评分。

样本口径（与 gap_pick.py 对齐）：
  - 股票池默认沪深主板；--codes 可指定单票验收
  - 排除当日涨停、价格 > 80 的票
  - 标签 = T+1 开盘 >= 信号日收盘 * 1.01
  - 历史 PE/PB 不可靠，回测/训练阶段不做基本面过滤
  - ST 名称和行业过滤默认关闭（历史名称拿不到），可 --industry-filter 按当前
    F10 行业排除证券/地产/白酒；报告会标注该偏差

运行：
  python train_gap_model.py --codes 600519,000001 --epochs 30   # 冒烟
  python train_gap_model.py --industry-filter --epochs 100      # 全主板块训练
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

import gap_pick
import paths
import server

MODEL_FEATURES = [
    "pct_chg",
    "vol_ratio_5",
    "amplitude_pct",
    "body_ratio",
    "pos_ma20",
    "pos_ma60",
    "dist_high60",
    "dist_low60",
    "amount_yi",
    "prev_limit_up",
    "limit_streak_prev",
]

RULE_HIGH_FEATURES = [
    f for f in gap_pick.SCORE_FACTORS_HIGH if f != "industry_limit_count"
]
RULE_LOW_FEATURES = list(gap_pick.SCORE_FACTORS_LOW)

DAILY_BARS = 800
BOARD_PREFIXES = {
    "main": ("600", "601", "603", "605", "000", "001", "002", "003"),
    "chi_next": ("300", "301", "688"),
}


def codes_for_boards(boards="main"):
    prefixes = []
    for b in boards.split(","):
        b = b.strip().lower()
        prefixes.extend(BOARD_PREFIXES.get(b, ()))
    out = []
    for pre in prefixes:
        for i in range(1000):
            out.append(f"{pre}{i:03d}")
    return out


def fetch_daily(code: str):
    secid = f"{'1' if code.startswith('6') else '0'}.{code}"
    try:
        rows = server._klines(secid, 101, DAILY_BARS)
    except Exception:
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if df.empty or "date" not in df.columns:
        return None
    if "volume" not in df.columns and "vol" in df.columns:
        df = df.rename(columns={"vol": "volume"})
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < gap_pick.MIN_HISTORY_BARS + 5:
        return None
    df = gap_pick._add_limit_labels(df)
    df = gap_pick._add_features(df)
    return df


def collect_samples(codes, start, end, industry_filter, boards, verbose=True):
    rows = []
    t0 = time.time()
    n_valid = 0
    allowed_boards = {b.strip().lower() for b in boards.split(",") if b.strip()}
    for idx, code in enumerate(codes, 1):
        code = str(code).zfill(6)
        if gap_pick._board_of(code) not in allowed_boards:
            continue
        if industry_filter:
            industry = gap_pick._stock_industry(code) or ""
            if not gap_pick._industry_allowed(industry):
                continue
        df = fetch_daily(code)
        if df is None:
            continue
        n_valid += 1
        for j in range(len(df) - 1):
            r = df.iloc[j]
            nxt = df.iloc[j + 1]
            date = str(r["date"])[:10]
            if start and date < start:
                continue
            if end and date > end:
                continue
            price = float(r["close"])
            if not gap_pick._price_ok(price, None):
                continue
            if not gap_pick._not_limit_up(float(r.get("pct_chg") or 0.0)):
                continue
            feats = {}
            bad = False
            for name in MODEL_FEATURES:
                v = r.get(name)
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    bad = True
                    break
                if math.isnan(fv):
                    bad = True
                    break
                feats[name] = fv
            if bad:
                continue
            label = 1 if float(nxt["open"]) >= price * 1.01 else 0
            rows.append({"date": date, "code": code, **feats, "label": label})
        if verbose and idx % 50 == 0:
            el = time.time() - t0
            print(
                f"[train] {idx}/{len(codes)} 有效 {n_valid} 只 | "
                f"样本 {len(rows)} 条 | 已用 {el:.0f}s",
                flush=True,
            )
    return rows, n_valid


def roc_auc(y, p):
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = pd.Series(p).rank(method="average").values
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def sigmoid(z):
    z = np.clip(z, -50.0, 50.0)
    return 1.0 / (1.0 + np.exp(-z))


def train_logreg(Xtr, ytr, Xva, yva, epochs, lr, l2):
    n, d = Xtr.shape
    w = np.zeros(d)
    b = 0.0
    m = np.zeros(d)
    v = np.zeros(d)
    mb = 0.0
    vb = 0.0
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    best_auc = -1.0
    best = (w.copy(), b)
    for ep in range(1, epochs + 1):
        p = sigmoid(Xtr @ w + b)
        gw = Xtr.T @ (p - ytr) / n + l2 * w
        gb = float((p - ytr).mean())
        m = beta1 * m + (1 - beta1) * gw
        v = beta2 * v + (1 - beta2) * gw * gw
        mh = m / (1 - beta1 ** ep)
        vh = v / (1 - beta2 ** ep)
        w = w - lr * mh / (np.sqrt(vh) + eps)
        mb = beta1 * mb + (1 - beta1) * gb
        vb = beta2 * vb + (1 - beta2) * gb * gb
        mbh = mb / (1 - beta1 ** ep)
        vbh = vb / (1 - beta2 ** ep)
        b = b - lr * mbh / (math.sqrt(vbh) + eps)
        if ep % 10 == 0 or ep == epochs:
            va = roc_auc(yva, sigmoid(Xva @ w + b))
            if not math.isnan(va) and va > best_auc:
                best_auc = va
                best = (w.copy(), b)
    if best_auc < 0:
        best = (w.copy(), b)
    return best[0], best[1]


def topk_rates(df, col, ks=(1, 3, 10)):
    out = {k: [0, 0] for k in ks}
    for _, g in df.groupby("date", sort=True):
        g = g.sort_values(col, ascending=False)
        for k in ks:
            if len(g) < k:
                continue
            out[k][1] += 1
            if bool(g.iloc[:k]["label"].any()):
                out[k][0] += 1
    return {k: round(out[k][0] / max(1, out[k][1]), 4) for k in ks}


def main():
    ap = argparse.ArgumentParser(description="训练次日高开排序模型（纯 numpy）")
    ap.add_argument("--codes", type=str, default=None, help="指定股票代码，逗号分隔")
    ap.add_argument("--symbols-file", type=str, default=None, help="每行一个股票代码的文本文件")
    ap.add_argument("--boards", type=str, default="main",
                    help="股票池板块，逗号分隔：main / chi_next")
    ap.add_argument("--limit", type=int, default=0, help="板块枚举只取前 N 个（冒烟）")
    ap.add_argument("--start", default="2024-08-01", help="样本开始日期")
    ap.add_argument("--end", default=time.strftime("%Y-%m-%d"), help="样本结束日期")
    ap.add_argument("--industry-filter", action="store_true",
                    help="按当前 F10 行业排除证券/地产/白酒（慢）")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--lr", type=float, default=0.1)
    ap.add_argument("--l2", type=float, default=0.001)
    ap.add_argument("--min-samples", type=int, default=2000)
    ap.add_argument("--out", default=paths.data_path("gap_model.json"))
    ap.add_argument("--report", default=os.path.join("backtest_report", "gap_model_train_report.csv"))
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
        mode = f"codes:{len(codes)}"
    elif args.symbols_file:
        with open(args.symbols_file, "r", encoding="utf-8") as f:
            codes = [
                c.strip().zfill(6)
                for line in f
                for c in [line.split("#", 1)[0].strip()]
                if c
            ]
        mode = f"file:{len(codes)}"
    else:
        codes = codes_for_boards(args.boards)
        if args.limit:
            codes = codes[: args.limit]
        mode = f"boards:{args.boards}:{len(codes)}"

    print(f"[train] 股票池 {len(codes)} 只 | {args.start} ~ {args.end}", flush=True)
    if not args.industry_filter:
        print("[train] 注意：未开启行业过滤，训练集与线上行业口径存在偏差", flush=True)

    rows, n_valid = collect_samples(
        codes, args.start, args.end, args.industry_filter, args.boards, verbose=True
    )
    if len(rows) < args.min_samples:
        print(f"[train] 样本不足 {len(rows)} < {args.min_samples}，中止", flush=True)
        sys.exit(2)

    df = pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True)
    y = df["label"].to_numpy(dtype=float)
    X = df[MODEL_FEATURES].to_numpy(dtype=float)
    dates = df["date"].unique()
    cut1 = dates[int(len(dates) * 0.7)]
    cut2 = dates[int(len(dates) * 0.85)]
    tr = df["date"] < cut1
    va = (df["date"] >= cut1) & (df["date"] < cut2)
    te = df["date"] >= cut2

    mean = X[tr.to_numpy()].mean(axis=0)
    std = X[tr.to_numpy()].std(axis=0)
    std[std < 1e-8] = 1.0
    Xs = (X - mean) / std
    Xtr, ytr = Xs[tr.to_numpy()], y[tr.to_numpy()]
    Xva, yva = Xs[va.to_numpy()], y[va.to_numpy()]
    Xte, yte = Xs[te.to_numpy()], y[te.to_numpy()]

    print(
        f"[train] 样本 {len(df)} 条 | 正样本率 {y.mean():.4f} | "
        f"train/val/test {len(Xtr)}/{len(Xva)}/{len(Xte)}",
        flush=True,
    )
    w, b = train_logreg(Xtr, ytr, Xva, yva, args.epochs, args.lr, args.l2)
    df["prob"] = sigmoid(Xs @ w + b)

    for col in RULE_HIGH_FEATURES:
        df[f"hit_{col}"] = df[col] >= df.groupby("date")[col].transform("median")
    for col in RULE_LOW_FEATURES:
        df[f"hit_{col}"] = df[col] <= df.groupby("date")[col].transform("median")
    df["rule_score"] = df[
        [f"hit_{c}" for c in RULE_HIGH_FEATURES + RULE_LOW_FEATURES]
    ].sum(axis=1)

    test_df = df[te.to_numpy()].copy()
    metrics = {
        "n_samples": int(len(df)),
        "n_valid_symbols": int(n_valid),
        "base_rate": round(float(y.mean()), 4),
        "train_auc": round(roc_auc(ytr, Xtr @ w + b), 4),
        "val_auc": round(roc_auc(yva, Xva @ w + b), 4),
        "test_auc": round(roc_auc(yte, Xte @ w + b), 4),
        "test_top1_model": topk_rates(test_df, "prob").get(1),
        "test_top3_model": topk_rates(test_df, "prob").get(3),
        "test_top10_model": topk_rates(test_df, "prob").get(10),
        "test_top1_rule": topk_rates(test_df, "rule_score").get(1),
        "test_top3_rule": topk_rates(test_df, "rule_score").get(3),
        "test_top10_rule": topk_rates(test_df, "rule_score").get(10),
    }
    print("---- 验收 ----", flush=True)
    for k, v in metrics.items():
        print(f"  {k}: {v}", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    payload = {
        "name": "gap_up_logreg",
        "version": 1,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "features": MODEL_FEATURES,
        "mean": [round(float(x), 8) for x in mean],
        "std": [round(float(x), 8) for x in std],
        "weights": [round(float(x), 8) for x in w],
        "intercept": round(float(b), 8),
        "metrics": metrics,
        "n_samples": int(len(df)),
        "date_range": [args.start, args.end],
        "universe": {"mode": mode, "n_symbols": len(codes), "n_valid": n_valid},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[train] 模型已写入 {args.out}", flush=True)

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        cols = ["date", "code", "prob", "rule_score", "label"]
        test_df[cols].to_csv(args.report, index=False)
        print(f"[train] 测试样本已写入 {args.report}", flush=True)


if __name__ == "__main__":
    main()
