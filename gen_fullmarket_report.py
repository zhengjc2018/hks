# -*- coding: utf-8 -*-
"""
全市场五日线(现版)回测报告生成器
读取 backtest_report/dualpath_trades_current_full.csv
（由 backtest_orig_vs_current.py 的 cur 部分产出，即 run_exit 五日线版本逐笔数据）
生成 HTML 报告 + 终端摘要。
"""
import os
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(BASE, "backtest_report", "dualpath_trades_current_full.csv")
OUT = os.path.join(BASE, "backtest_report", "fullmarket_5day_report.html")

REASON_CN = {
    "a_ma5": "情形A·跌破5日线",
    "b_triple_ma5": "情形B·三重确认后破5日线",
    "h_hard_stop": "-8%硬止损",
    "rsi_over": "RSI>70过热了结",
    "time_exit": "20日时间退出",
}


def board(c):
    c = str(c)
    if c.startswith("60"):
        return "沪市主板"
    if c.startswith("68"):
        return "科创板"
    if c.startswith("00"):
        return "深市主板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith("8") or c.startswith("4"):
        return "北交所"
    return "其他"


def hbucket(d):
    d = float(d)
    if d <= 3:
        return "0-3天"
    if d <= 7:
        return "4-7天"
    if d <= 15:
        return "8-15天"
    if d <= 30:
        return "16-30天"
    return "30天以上"


def pf(s):
    win = s[s > 0]
    loss = s[s <= 0]
    if len(loss) == 0:
        return float("inf")
    total_win = float(win.sum())
    total_loss = float(-loss.sum())
    return total_win / total_loss if total_loss > 0 else float("inf")


def metrics(s):
    n = len(s)
    if n == 0:
        return dict(n=0, win=0.0, pf=0.0, med=0.0, mean=0.0, mw=0.0, ml=0.0)
    win = (s > 0).mean() * 100
    return dict(
        n=n,
        win=round(win, 1),
        pf=round(pf(s), 2),
        med=round(s.median(), 2),
        mean=round(s.mean(), 2),
        mw=round(s[s > 0].mean(), 2) if (s > 0).any() else 0.0,
        ml=round(s[s <= 0].mean(), 2) if (s <= 0).any() else 0.0,
    )


def main():
    if not os.path.exists(CSV):
        print("CSV 不存在:", CSV)
        return
    df = pd.read_csv(CSV, dtype={"code": str, "market": str})
    df["pnl"] = df["pnl"].astype(float)
    df["hold_days"] = df["hold_days"].astype(float)
    df["board"] = df["code"].apply(board)
    df["hb"] = df["hold_days"].apply(hbucket)
    df["reason_cn"] = df["reason"].map(lambda r: REASON_CN.get(r, r))
    df["year"] = pd.to_datetime(df["buy_date"], errors="coerce").dt.year

    overall = metrics(df["pnl"])
    overall["hold"] = round(df["hold_days"].mean(), 1)
    overall["stocks"] = df["code"].nunique()

    # 分板块
    board_rows = []
    for b in ["沪市主板", "科创板", "深市主板", "创业板", "其他"]:
        sub = df[df["board"] == b]
        if len(sub) == 0:
            continue
        m = metrics(sub["pnl"])
        m["board"] = b
        m["hold"] = round(sub["hold_days"].mean(), 1)
        board_rows.append(m)

    # 卖出原因
    reason = df.groupby("reason_cn")["pnl"].agg(["count", "mean"]).reset_index()
    reason["win"] = df.groupby("reason_cn")["pnl"].apply(lambda s: round((s > 0).mean()*100, 1) if False else (s > 0).sum()/len(s)*100)
    reason = reason.sort_values("count", ascending=False)
    reason_rows = [
        dict(name=r["reason_cn"], n=int(r["count"]), win=round((df[df["reason_cn"]==r["reason_cn"]]["pnl"]>0).mean()*100,1), mean=round(r["mean"],2))
        for _, r in reason.iterrows()
    ]

    # 持股天数
    hb_order = ["0-3天", "4-7天", "8-15天", "16-30天", "30天以上"]
    hb_rows = []
    for h in hb_order:
        sub = df[df["hb"] == h]
        if len(sub) == 0:
            continue
        hb_rows.append(dict(name=h, n=len(sub), win=round((sub["pnl"]>0).mean()*100,1), mean=round(sub["pnl"].mean(),2)))

    # 年度
    yr_rows = []
    for y in sorted(df["year"].dropna().unique()):
        sub = df[df["year"] == y]
        m = metrics(sub["pnl"])
        m["year"] = int(y)
        m["hold"] = round(sub["hold_days"].mean(), 1)
        yr_rows.append(m)

    # Top 股票（按笔数 & 按累计盈亏）
    top_n = df.groupby(["code", "name"]).agg(笔数=("pnl", "size"), 胜率=("pnl", lambda s: round((s>0).mean()*100,1)),
                                             平均盈亏=("pnl", "mean"), 累计盈亏=("pnl", "sum"), 平均持股=("hold_days", "mean")).reset_index()
    top_n["平均盈亏"] = top_n["平均盈亏"].round(2)
    top_n["累计盈亏"] = top_n["累计盈亏"].round(2)
    top_n["平均持股"] = top_n["平均持股"].round(1)
    top_by_cnt = top_n.sort_values("笔数", ascending=False).head(20)
    top_by_pnl = top_n.sort_values("累计盈亏", ascending=False).head(20)

    # ---------- HTML ----------
    def bar(pct, color):
        return f'<div class="bar"><span style="width:{max(0,min(100,pct))}%;background:{color}"></span></div>'

    board_html = "".join(
        f"<tr><td>{m['board']}</td><td>{m['n']}</td><td>{m['win']}%</td><td>{m['pf']}</td>"
        f"<td>{m['med']}%</td><td>{m['mw']}%</td><td>{m['ml']}%</td><td>{m['hold']}天</td>"
        f"<td>{bar(m['win'], '#c0392b' if m['win']>=50 else '#e67e22')}</td></tr>"
        for m in board_rows
    )
    reason_html = "".join(
        f"<tr><td>{r['name']}</td><td>{r['n']}</td><td>{r['win']}%</td><td>{r['mean']}%</td>"
        f"<td>{bar(r['win'], '#c0392b' if r['win']>=50 else '#e67e22')}</td></tr>"
        for r in reason_rows
    )
    hb_html = "".join(
        f"<tr><td>{r['name']}</td><td>{r['n']}</td><td>{r['win']}%</td><td>{r['mean']}%</td>"
        f"<td>{bar(r['win'], '#c0392b' if r['win']>=50 else '#e67e22')}</td></tr>"
        for r in hb_rows
    )
    yr_html = "".join(
        f"<tr><td>{m['year']}</td><td>{m['n']}</td><td>{m['win']}%</td><td>{m['pf']}</td>"
        f"<td>{m['med']}%</td><td>{m['hold']}天</td></tr>"
        for m in yr_rows
    )
    top_cnt_html = "".join(
        f"<tr><td>{r['code']}</td><td>{r['name']}</td><td>{int(r['笔数'])}</td><td>{r['胜率']}%</td>"
        f"<td>{r['平均盈亏']}%</td><td>{r['平均持股']}天</td></tr>"
        for _, r in top_by_cnt.iterrows()
    )
    top_pnl_html = "".join(
        f"<tr><td>{r['code']}</td><td>{r['name']}</td><td>{r['累计盈亏']}%</td><td>{int(r['笔数'])}</td>"
        f"<td>{r['胜率']}%</td><td>{r['平均盈亏']}%</td></tr>"
        for _, r in top_by_pnl.iterrows()
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>全市场五日线版本回测报告</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:24px;color:#222;background:#fff}}
h1{{font-size:22px;border-bottom:3px solid #c0392b;padding-bottom:8px}}
h2{{font-size:17px;margin-top:30px;color:#333}}
.kpi{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.kpi div{{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:12px 16px;min-width:120px}}
.kpi b{{display:block;font-size:22px;color:#c0392b}}
.kpi span{{font-size:12px;color:#888}}
table{{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}}
th,td{{border:1px solid #eee;padding:6px 8px;text-align:center}}
th{{background:#f5f5f5}}
.bar{{background:#f0f0f0;border-radius:4px;height:14px;width:120px;overflow:hidden;display:inline-block;vertical-align:middle}}
.bar span{{display:block;height:100%}}
.note{{background:#fff8f0;border-left:4px solid #e67e22;padding:10px 14px;font-size:13px;color:#666;margin:14px 0}}
</style></head><body>
<h1>全市场五日线（现版）回测报告 · dualpath 双路径</h1>
<div class="note">数据来源：dualpath_trades_current_full.csv（run_exit 五日线卖点，全市场 4984 只信号子集）。
买入信号：①②③④⑤ 序列状态机；信号日收盘确认并以信号日收盘价买入；卖出：情形A/B 跌破5日线 + RSI&gt;70过热了结 + 20日时间退出 + -8%硬止损。
本报告仅含五日线版本；原版对比数据已忽略。</div>

<div class="kpi">
  <div><b>{overall['n']}</b><span>总交易笔数</span></div>
  <div><b>{overall['stocks']}</b><span>涉及股票数</span></div>
  <div><b>{overall['win']}%</b><span>胜率（盈利比例）</span></div>
  <div><b>{overall['pf']}</b><span>盈亏比（赚÷亏）</span></div>
  <div><b>{overall['med']}%</b><span>盈亏中位数</span></div>
  <div><b>{overall['mean']}%</b><span>平均盈亏</span></div>
  <div><b>{overall['hold']}天</b><span>平均持股天数</span></div>
</div>

<h2>一、分板块表现</h2>
<table><tr><th>板块</th><th>笔数</th><th>胜率</th><th>盈亏比</th><th>中位</th><th>均盈</th><th>均亏</th><th>均持</th><th>胜率</th></tr>{board_html}</table>

<h2>二、卖出原因分布</h2>
<table><tr><th>卖出原因</th><th>笔数</th><th>胜率</th><th>平均盈亏</th><th>胜率</th></tr>{reason_html}</table>

<h2>三、持股天数分布</h2>
<table><tr><th>区间</th><th>笔数</th><th>胜率</th><th>平均盈亏</th><th>胜率</th></tr>{hb_html}</table>

<h2>四、分年度表现</h2>
<table><tr><th>年度</th><th>笔数</th><th>胜率</th><th>盈亏比</th><th>中位</th><th>均持</th></tr>{yr_html}</table>

<h2>五、交易最频繁的前 20 只</h2>
<table><tr><th>代码</th><th>名称</th><th>笔数</th><th>胜率</th><th>平均盈亏</th><th>均持</th></tr>{top_cnt_html}</table>

<h2>六、累计盈亏最高的前 20 只</h2>
<table><tr><th>代码</th><th>名称</th><th>累计盈亏(%)</th><th>笔数</th><th>胜率</th><th>平均盈亏</th></tr>{top_pnl_html}</table>

<p style="margin-top:30px;color:#aaa;font-size:12px">由 gen_fullmarket_report.py 自动生成 · dualpath 双路径框架</p>
</body></html>"""

    with open(OUT, "w", encoding="utf-8-sig") as f:
        f.write(html)

    print(f"[done] 报告已生成: {OUT}")
    print(f"  总笔数={overall['n']} 涉及股票={overall['stocks']} 胜率={overall['win']}% "
          f"盈亏比={overall['pf']} 中位={overall['med']}% 均盈亏={overall['mean']}% 均持={overall['hold']}天")
    print(f"  板块数={len(board_rows)} 原因类={len(reason_rows)} 年度数={len(yr_rows)}")


if __name__ == "__main__":
    main()
