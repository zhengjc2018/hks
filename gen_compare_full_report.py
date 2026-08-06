# -*- coding: utf-8 -*-
"""全盘双版本对比报告生成器。
读 dualpath_trades_current_full.csv（现行版）与 dualpath_trades_filtered_full.csv（优化买点版），
输出 backtest_report/fullmarket_compare.html：并排对比 KPI、分板块、卖出原因、持股天数、年度。
若 CSV 缺失或过小（全盘还没落盘成功），自动从 compare_full_state.json 导出数据再出报告。
用法：python gen_compare_full_report.py
"""
import os
import json
import pandas as pd
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report")
CSV_CUR = os.path.join(OUT, "dualpath_trades_current_full.csv")
CSV_FCUR = os.path.join(OUT, "dualpath_trades_filtered_full.csv")
STATE = os.path.join(OUT, "compare_full_state.json")
HTML = os.path.join(OUT, "fullmarket_compare.html")

COLS = ["code", "name", "market", "path", "case", "buy_date", "buy_price",
        "exit_date", "exit_price", "hold_days", "pnl", "reason"]


def load_data():
    """优先读 CSV（全盘正常落盘）；CSV 过小/缺失则从 STATE 导出（沙箱 PermissionError 兜底）。"""
    cur = fcur = None
    try:
        cur = pd.read_csv(CSV_CUR, dtype={"code": str})
        fcur = pd.read_csv(CSV_FCUR, dtype={"code": str})
        if len(cur) >= 1000 and len(fcur) >= 1000:
            print(f"[csv] 现行版 {len(cur)} 笔 / 优化版 {len(fcur)} 笔")
            return cur, fcur
        print(f"[warn] CSV 行数过小(cur={len(cur)},fcur={len(fcur)})，改从 STATE 导出")
    except Exception as e:
        print(f"[warn] 读 CSV 失败({e})，改从 STATE 导出")
    state = json.load(open(STATE))
    cur = pd.DataFrame(state["cur"])[COLS]
    fcur = pd.DataFrame(state["fcur"])[COLS]
    # 顺带把 CSV 补写出来（用 .new 后缀避开沙箱对旧文件的锁定）
    for df, p in [(cur, CSV_CUR), (fcur, CSV_FCUR)]:
        try:
            df.to_csv(p, index=False, encoding="utf-8-sig")
            print(f"[csv] 已补写 {p}")
        except Exception as e:
            np_ = p + ".new"
            df.to_csv(np_, index=False, encoding="utf-8-sig")
            print(f"[warn] 覆盖 {p} 失败({e})，已写 {np_}")
    print(f"[state] 现行版 {len(cur)} 笔 / 优化版 {len(fcur)} 笔")
    return cur, fcur

BLOCKS = {"沪主板": "60", "科创": "68", "深主板": "00", "创业": "30"}


def block_of(code: str) -> str:
    if str(code).startswith("60"):
        return "沪主板"
    if str(code).startswith("68"):
        return "科创"
    if str(code).startswith("00"):
        return "深主板"
    if str(code).startswith("30"):
        return "创业"
    return "其他"


def kpi(d: pd.DataFrame) -> dict:
    if d.empty:
        return {"n": 0, "wr": 0, "pf": 0, "med": 0, "avg_w": 0, "avg_l": 0, "hold": 0, "total": 0}
    wins = d[d.pnl > 0]
    losses = d[d.pnl <= 0]
    pf = (wins.pnl.sum() / abs(losses.pnl.sum())) if len(losses) and losses.pnl.sum() != 0 else float("inf")
    return {
        "n": len(d),
        "wr": round(len(wins) / len(d) * 100, 1),
        "pf": round(pf, 2) if np.isfinite(pf) else 999,
        "med": round(d.pnl.median(), 2),
        "avg_w": round(wins.pnl.mean(), 2) if len(wins) else 0,
        "avg_l": round(losses.pnl.mean(), 2) if len(losses) else 0,
        "hold": round(d.hold_days.mean(), 1),
        "total": round(d.pnl.sum(), 1),
    }


def reason_dist(d: pd.DataFrame) -> pd.Series:
    return d.reason.value_counts()


def block_table(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["板块"] = d.code.map(block_of)
    rows = []
    for b, g in d.groupby("板块"):
        k = kpi(g)
        rows.append({"板块": b, "笔数": k["n"], "胜率%": k["wr"], "盈亏比": k["pf"],
                     "中位%": k["med"], "累计盈亏%": k["total"]})
    return pd.DataFrame(rows).sort_values("笔数", ascending=False)


def year_table(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["年度"] = pd.to_datetime(d.buy_date).dt.year
    rows = []
    for y, g in d.groupby("年度"):
        k = kpi(g)
        rows.append({"年度": y, "笔数": k["n"], "胜率%": k["wr"], "盈亏比": k["pf"],
                     "中位%": k["med"], "累计盈亏%": k["total"]})
    return pd.DataFrame(rows).sort_values("年度")


def hold_hist(d: pd.DataFrame) -> str:
    bins = [(0, 5, "≤5天"), (5, 10, "6-10天"), (10, 20, "11-20天"), (20, 60, "21-60天"), (60, 10**9, ">60天")]
    parts = []
    for lo, hi, lab in bins:
        n = int(((d.hold_days > lo) & (d.hold_days <= hi)).sum())
        parts.append(f"<span class='hd'><b>{lab}</b> {n}</span>")
    return " ".join(parts)


def css() -> str:
    return """
<style>
body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6fa;color:#222;margin:0;padding:24px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#777;font-size:13px;margin-bottom:18px}
.card{background:#fff;border-radius:10px;padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{font-size:16px;margin:0 0 12px;border-left:4px solid #c0392b;padding-left:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid #e8e8e8;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#fafafa;color:#555;font-weight:600}
tr:hover td{background:#faf5f5}
.pos{color:#c0392b;font-weight:600}.neg{color:#1e8449;font-weight:600}
.hd{display:inline-block;margin-right:14px;font-size:13px;color:#555}
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.kpi-box{background:#fafafa;border-radius:8px;padding:10px 12px;text-align:center}
.kpi-box .v{font-size:22px;font-weight:700;color:#c0392b}
.kpi-box .l{font-size:11px;color:#888;margin-top:2px}
.kpi-box.c2 .v{color:#2471a3}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#eee;color:#555;margin:0 4px 4px 0}
.tag.best{background:#c0392b;color:#fff}
</style>"""


def pnl_class(v):
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def fmt_num(v):
    if isinstance(v, (int, float)):
        if abs(v) >= 100:
            return f"{v:,.0f}"
        return f"{v:,.1f}"
    return str(v)


def main():
    cur, fcur = load_data()
    kc, kf = kpi(cur), kpi(fcur)

    def kpi_row(k):
        return (f"<div class='kpi-box'><div class='v'>{k['n']}</div><div class='l'>总笔数</div></div>"
                f"<div class='kpi-box'><div class='v'>{k['wr']}%</div><div class='l'>胜率</div></div>"
                f"<div class='kpi-box'><div class='v'>{k['pf']}</div><div class='l'>盈亏比</div></div>"
                f"<div class='kpi-box'><div class='v'>{k['med']}%</div><div class='l'>中位盈亏</div></div>")

    # 总览对比表
    rows = []
    for lab, k in [("现行版（五日线·全信号）", kc), ("优化买点版（入场过滤+五日线）", kf)]:
        rows.append(f"<tr><td>{lab}</td><td>{k['n']}</td><td>{k['wr']}%</td><td>{k['pf']}</td>"
                    f"<td>{k['med']}%</td><td class='pos'>{k['avg_w']}%</td><td class='neg'>{k['avg_l']}%</td>"
                    f"<td>{k['hold']}</td><td>{fmt_num(k['total'])}%</td></tr>")
    # 卖出原因对比
    rc, rf = reason_dist(cur), reason_dist(fcur)
    all_reasons = list(dict.fromkeys(list(rc.index) + list(rf.index)))
    reason_rows = []
    for r in all_reasons:
        reason_rows.append(f"<tr><td>{r}</td><td>{int(rc.get(r, 0))}</td><td>{int(rf.get(r, 0))}</td></tr>")
    # 板块对比
    bc, bf = block_table(cur), block_table(fcur)
    blk_rows = []
    for b in set(bc.板块) | set(bf.板块):
        rc_ = bc[bc.板块 == b]
        rf_ = bf[bf.板块 == b]
        blk_rows.append(f"<tr><td>{b}</td>"
                        f"<td>{int(rc_['笔数'].iloc[0]) if len(rc_) else 0}</td><td>{rc_['胜率%'].iloc[0] if len(rc_) else '-'}</td><td>{rc_['盈亏比'].iloc[0] if len(rc_) else '-'}</td>"
                        f"<td>{int(rf_['笔数'].iloc[0]) if len(rf_) else 0}</td><td>{rf_['胜率%'].iloc[0] if len(rf_) else '-'}</td><td>{rf_['盈亏比'].iloc[0] if len(rf_) else '-'}</td></tr>")
    # 年度对比
    yc, yf = year_table(cur), year_table(fcur)
    yr_rows = []
    for y in sorted(set(yc.年度) | set(yf.年度)):
        rc_ = yc[yc.年度 == y]
        rf_ = yf[yf.年度 == y]
        yr_rows.append(f"<tr><td>{y}</td>"
                       f"<td>{int(rc_['笔数'].iloc[0]) if len(rc_) else 0}</td><td>{rc_['胜率%'].iloc[0] if len(rc_) else '-'}</td><td>{rc_['盈亏比'].iloc[0] if len(rc_) else '-'}</td>"
                       f"<td>{int(rf_['笔数'].iloc[0]) if len(rf_) else 0}</td><td>{rf_['胜率%'].iloc[0] if len(rf_) else '-'}</td><td>{rf_['盈亏比'].iloc[0] if len(rf_) else '-'}</td></tr>")

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>全市场双版本对比 · 现行版 vs 优化买点版</title>{css()}</head><body><div class="wrap">
<h1>全市场双版本对比报告（信号宇宙 4984 只）</h1>
<div class="sub">同一批 ⑤ 信号 · 同一卖点（五日线）· 唯一变量 = 买点过滤（买价距 MA5 收敛才买）· 信号日收盘买入口径 · 生成 {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="card"><h2>一、总览（并排对比）</h2>
<div class="kpi-grid">{kpi_row(kc)}</div>
<div class="kpi-grid c2">{kpi_row(kf)}</div>
<table>
<tr><th>版本</th><th>总笔数</th><th>胜率%</th><th>盈亏比</th><th>中位盈亏%</th><th>盈利单均赚%</th><th>亏损单均亏%</th><th>平均持股(天)</th><th>累计盈亏%</th></tr>
{''.join(rows)}
</table></div>

<div class="card"><h2>二、卖出原因分布</h2>
<table><tr><th>离场原因</th><th>现行版</th><th>优化买点版</th></tr>
{''.join(reason_rows)}
</table></div>

<div class="card"><h2>三、分板块对比（现行版 → 优化买点版）</h2>
<table><tr><th>板块</th><th>现行笔数</th><th>现行胜率%</th><th>现行盈亏比</th><th>优化笔数</th><th>优化胜率%</th><th>优化盈亏比</th></tr>
{''.join(blk_rows)}
</table></div>

<div class="card"><h2>四、分年度对比</h2>
<table><tr><th>年度</th><th>现行笔数</th><th>现行胜率%</th><th>现行盈亏比</th><th>优化笔数</th><th>优化胜率%</th><th>优化盈亏比</th></tr>
{''.join(yr_rows)}
</table></div>

<div class="card"><h2>五、持股天数分布</h2>
<p>现行版：{hold_hist(cur)}</p>
<p>优化买点版：{hold_hist(fcur)}</p></div>

</div></body></html>"""
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"报告已生成: {HTML}")
    print(f"现行版 {kc['n']} 笔 | 胜率{kc['wr']}% | 盈亏比{kc['pf']} | 中位{kc['med']}%")
    print(f"优化版 {kf['n']} 笔 | 胜率{kf['wr']}% | 盈亏比{kf['pf']} | 中位{kf['med']}%")


if __name__ == "__main__":
    main()
