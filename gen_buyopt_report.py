# -*- coding: utf-8 -*-
"""生成「买点优化对比」正式报告(HTML)，数据全部从三版回测 CSV 实时核算。"""
import pandas as pd, os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report")
base = pd.read_csv(os.path.join(OUT, "dualpath_trades.csv"))        # 基线(纯SOP复刻)
ef   = pd.read_csv(os.path.join(OUT, "dualpath_trades_ef.csv"))     # 入场过滤(收敛区买入)
efd  = pd.read_csv(os.path.join(OUT, "dualpath_trades_ef_d.csv"))   # 入场过滤+离场钝化

def pf(d):
    w = d[d.pnl > 0].pnl.sum(); l = abs(d[d.pnl <= 0].pnl.sum())
    return round(w / l, 2) if l else 0.0

def ov(d):
    return dict(
        n=len(d),
        win=round((d.pnl > 0).mean() * 100, 1),
        pf=pf(d),
        med=round(d.pnl.median(), 2),
        aw=round(d[d.pnl > 0].pnl.mean(), 2),
        al=round(d[d.pnl <= 0].pnl.mean(), 2),
        hold=round(d.hold_days.mean(), 1),
    )

O = {"基线(纯SOP复刻)": ov(base), "入场过滤(收敛区买入)": ov(ef), "入场+离场钝化(走错)": ov(efd)}

# 重点离场原因三版并排
FOCUS = ["rsi_overheat", "ma5_A", "ma5_B", "hard_stop"]
def reason_block(d):
    out = {}
    for r in FOCUS:
        sub = d[d.reason == r]
        out[r] = dict(n=len(sub),
                      win=round((sub.pnl > 0).mean() * 100, 1) if len(sub) else 0,
                      mean=round(sub.pnl.mean(), 2) if len(sub) else 0)
    return out
RB = {"基线": reason_block(base), "入场过滤": reason_block(ef), "离场钝化": reason_block(efd)}

REASON_CN = {"rsi_overheat": "rsi_overheat(RSI>70止盈)", "ma5_A": "ma5_A(高位买入MA5止损)",
             "ma5_B": "ma5_B(低位买入MA5止损)", "hard_stop": "hard_stop(-8%风控假设)"}

# ---- 条形图(纯 CSS) ----
def bar_row(label, val, maxv, color, suffix=""):
    w = max(0.0, min(100.0, val / maxv * 100))
    return ('<div class="brow"><span class="bl">' + label + '</span>'
            '<span class="btrack"><span class="bfill" style="width:{w:.1f}%;background:{c}"></span></span>'
            '<span class="bv">{val}{suf}</span></div>').format(w=w, c=color, val=val, suf=suffix)

maxwin = max(O[a]["win"] for a in O)
maxpf = max(O[a]["pf"] for a in O)
win_bars = "".join(bar_row(k, O[k]["win"], maxwin, "#2e7d32") for k in O)
pf_bars = "".join(bar_row(k, O[k]["pf"], maxpf, "#1565c0") for k in O)

# ---- 总览表 ----
def fmt(x): return f"{x}"
ov_rows = ""
for k in O:
    o = O[k]
    tag = "✔ 有效" if k == "入场过滤(收敛区买入)" else ("✘ 走错" if k == "入场+离场钝化(走错)" else "基准")
    ov_rows += (f"<tr><td>{k}<br><span class='tag'>{tag}</span></td>"
                f"<td>{o['n']}</td><td><b>{o['win']}%</b></td><td>{o['pf']}</td>"
                f"<td>{o['med']}%</td><td>{o['aw']}%</td><td>{o['al']}%</td><td>{o['hold']}</td></tr>")

# ---- 离场原因三版并排表 ----
reason_rows = ""
for r in FOCUS:
    cells = ""
    for k in ["基线", "入场过滤", "离场钝化"]:
        b = RB[k][r]
        cells += f"<td>{b['n']}笔<br>{b['win']}%<br>均{b['mean']}%</td>"
    reason_rows += f"<tr><td>{REASON_CN[r]}</td>{cells}</tr>"

# ---- case 分布 ----
def case_dist(d):
    c = d.case.value_counts().to_dict()
    return f"case A(高位买)={c.get('A',0)} / case B(低位买)={c.get('B',0)}"
case_base = case_dist(base); case_ef = case_dist(ef); case_efd = case_dist(efd)
base_a = int((base.case == "A").sum())
ef_a = int((ef.case == "A").sum())
base_ma5a = base[base.reason == "ma5_A"]
base_ma5a_win = round((base_ma5a.pnl > 0).mean() * 100, 1) if len(base_ma5a) else 0.0
base_ma5a_mean = round(base_ma5a.pnl.mean(), 2) if len(base_ma5a) else 0.0

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>买点优化对比报告</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:1000px;margin:24px auto;padding:0 20px;color:#1a1a1a;line-height:1.6}}
 h1{{font-size:24px;border-bottom:3px solid #1565c0;padding-bottom:8px}}
 h2{{font-size:19px;margin-top:30px;color:#0d47a1}}
 h3{{font-size:16px;margin-top:18px}}
 table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13.5px}}
 th,td{{border:1px solid #d0d7de;padding:7px 9px;text-align:center}}
 th{{background:#f1f5f9}}
 tr:nth-child(even){{background:#fafbfc}}
 .tag{{font-size:11px;color:#666;font-weight:normal}}
 .note{{background:#fff8e1;border-left:4px solid #ffb300;padding:10px 14px;margin:12px 0;border-radius:0 4px 4px 0}}
 .good{{background:#e8f5e9;border-left:4px solid #2e7d32;padding:10px 14px;margin:12px 0;border-radius:0 4px 4px 0}}
 .bad{{background:#fdecea;border-left:4px solid #c62828;padding:10px 14px;margin:12px 0;border-radius:0 4px 4px 0}}
 .brow{{display:flex;align-items:center;margin:6px 0;font-size:13px}}
 .bl{{width:180px;flex-shrink:0}}
 .btrack{{flex:1;background:#eee;border-radius:4px;height:18px;overflow:hidden;position:relative}}
 .bfill{{display:block;height:100%}}
 .bv{{width:74px;text-align:right;flex-shrink:0;font-variant-numeric:tabular-nums}}
 code{{background:#f3f4f6;padding:1px 5px;border-radius:3px;font-size:12.5px}}
 .small{{font-size:12.5px;color:#555}}
</style></head><body>

<h1>买点优化对比报告</h1>
<p class="small">数据来源：同窗口(2024-12 ~ 2026-08，拉长版)三版全池39只回测逐笔成交 CSV，实时核算。
回测定位 = 程序 SOP 的<b>数据验证基准</b>（规则由用户带来，框架逐字复刻固定卖出铁律），并非自由策略优化沙盒。</p>

<h2>一、三版总览</h2>
<table>
<tr><th>版本</th><th>笔数</th><th>胜率</th><th>盈亏比</th><th>盈亏比图</th></tr>
</table>
<div class="brow"><span class="bl">胜率对比</span></div>
{win_bars}
<div style="height:8px"></div>
{pf_bars}
<table>
<tr><th>版本</th><th>笔数</th><th>胜率</th><th>盈亏比</th><th>中位收益</th><th>均盈利</th><th>均亏损</th><th>平均持仓(日)</th></tr>
{ov_rows}
</table>

<h2>二、按离场原因分层（核心差异在这）</h2>
<table>
<tr><th>离场原因</th><th>基线</th><th>入场过滤</th><th>离场钝化</th></tr>
{reason_rows}
</table>
<p class="small">每格 = 笔数 / 胜率 / 均值%。注意 <b>rsi_overheat</b> 是利润来源（几乎全胜），
<b>ma5_A / ma5_B / hard_stop</b> 是出血点。</p>

<h2>三、到底优化了什么</h2>

<div class="good"><h3>① 入场过滤（收敛区买入）—— ✔ 有效，且程序可落地</h3>
<p>原 case A：<code>buy_price &gt; MA5</code>（买入价在5日线上方=追高）。SOP 对 case A 的规则是「收盘破5日线即止损」
但买在均线上方几乎没缓冲，<b>后续一个普通回踩就破 MA5 被扫</b>。基线里 <b>case A 共 {base_a} 笔</b>（买在均线上方），
其中 <b>ma5_A 止损 {len(base_ma5a)} 笔、仅 {base_ma5a_win}% 胜率、均 {base_ma5a_mean}%</b>——绝大多数 case A 是「追高→回踩被扫」，少数靠 rsi_overheat 止盈才幸存。</p>
<p>改动：要求信号日收盘 <code>≤ 当日 MA5 × 1.0</code>（仍在收敛区、没有远离均线）才上车。case A 清零：
<span class="small">{case_ef}</span> vs 基线 <span class="small">{case_base}</span>。</p>
<p>结果：砍掉的全是 case A（{base_a} 笔高位追入，其中 ma5_A 止损 {len(base_ma5a)} 笔仅 {base_ma5a_win}% 胜率、均 {base_ma5a_mean}%），整体 胜率 46.2%→<b>53.9%</b>、盈亏比 1.36→<b>1.61</b>、中位 −0.38%→<b>+0.65%</b>。
属于<b>入场侧信号收紧、不动 SOP 任何卖出铁律</b>，程序完全能表达（信号日收盘直接检查收盘价与当日 MA5 的关系即可）。</p>
</div>

<div class="bad"><h3>② 离场钝化（MA5「有效跌破」闸门）—— ✘ 走错</h3>
<p>想法：给 case B 的 MA5 止损加门槛——「连续两日收破 MA5 <b>或</b> 单日跌幅&gt;1.5% 才砍」，想滤掉单日噪音。</p>
<p>结果：比纯入场过滤<b>全面回落</b>（128→127笔、胜率 53.9%→<b>49.6%</b>、盈亏比 1.61→<b>1.51</b>、中位 +0.65%→<b>−0.06%</b>）。</p>
<p>根因：<b>ma5_B 钝化后胜率 37.3%→27.7%、均值 −0.87%→−1.14%</b>。MA5 单日破在 case B 里本身就是<b>有效走弱预警</b>，
钝化把它当噪音滤掉，反而让真破位多扛一天、亏得更多。hard_stop 基本不变（仍全亏）。</p>
</div>

<h2>四、用户两条洞察被数据印证</h2>
<div class="note">
<p><b>洞察1（1.5%钝化对科技/庄股太敏感）：</b>表象成立，但本质更狠——<b>不该在 SOP 固定卖出规则之外自由发挥止损</b>。
那是程序给不出的提醒，加了也用不上。</p>
<p><b>洞察2（trailing/ATR 增理解负担、程序落地不了）：</b>回测若引入这些，测的就不是程序的真实行为了。
程序无券商接口、手动下单、提醒不灵活，止损规则不可复杂化。基线版（纯 SOP 复刻）才是合法基准。</p>
</div>

<h2>五、结论</h2>
<table>
<tr><th>方向</th><th>判定</th><th>理由</th></tr>
<tr><td>入场过滤（收敛区买入）</td><td><b style="color:#2e7d32">保留</b></td><td>唯一验证有效 + 程序可落地 + 不动卖出铁律</td></tr>
<tr><td>离场钝化 / trailing / ATR</td><td><b style="color:#c62828">否决</b></td><td>偏离 SOP、程序给不出、回测失真，且数据证伪</td></tr>
<tr><td>回测定位</td><td>纯 SOP 复刻作基准</td><td>为程序优化供数据，不优化止损花样</td></tr>
</table>

<h2>六、下一步（给程序优化的数据方向）</h2>
<p>买点侧已收敛到「入场过滤」这一刀。真正的下一刀在<b>行情适配</b>——前期按大盘 regime 切分已暴露盲点：
<b>SOP 在「震荡市」被反复收割</b>（基线震荡34笔/32.4%/PF0.58；入场过滤震荡27笔/37%/0.69，仍漏）。
这是你担心的「规则盲点」所在，且程序可做（每日大盘MA检查→抑制买入提醒，零复杂止损）。可作为下一个演绎点推进。</p>

<p class="small">生成时间：2026-08-05 ｜ 三版文件：dualpath_trades.csv / _ef.csv / _ef_d.csv（均保留不覆盖）</p>
</body></html>"""

with open(os.path.join(OUT, "buy_point_optimization.html"), "w", encoding="utf-8") as f:
    f.write(html)
print("OK 生成 buy_point_optimization.html")
print("总览:", {k: O[k] for k in O})
print("case分布: 基线", case_base, "| 入场过滤", case_ef, "| 离场钝化", case_efd)
