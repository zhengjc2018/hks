"""个股风险独立复检 + 一句话总结（AlphaLoop 纪律落地）。

数据驱动项（免费、无需 token）：
  · PE 过高        —— 腾讯 gtimg 静态字段（休盘也可取昨收 session 值）
  · 盘中换手率奇高 —— 同上
  · 交易权限        —— 板块判定（用户仅主板权限，双创/北交标注）

需新闻源项（本环境无免费实时公告源）：
  · 减持 / 利空公告 —— 默认 ⚠️「需人工或带搜索的模型核查」；
                       若用户配置了模型，会一并请模型研判（明确标注不确定性）。

总结（原因 + 买点 + 卖点）：
  · 未配置模型 → 规则模板（诚实、可解释）
  · 已配置模型 → 调用户模型生成自然语言一句话（标注 mode=model）
"""
from __future__ import annotations

import time
import os
import datetime
import requests

import paths
from board import classify_board
from llm_client import call_llm

# 单票一句话总结的 LLM 超时（秒）：超时即降级规则模板，避免详情页长期卡在「加载中」。
# minimax-m3 等慢模型单票总结约需 15-20s，设 45s 留足余量（后台异步生成，不阻塞页面）。
STOCK_LLM_TIMEOUT = 45
from news import get_news_risk

_QHDR = {"User-Agent": "Mozilla/5.0"}
_RANK = {"✅": 0, "⚠️": 1, "🟡": 2, "🔴": 3}


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_quote_extra(code: str, market: int) -> dict:
    """从腾讯 gtimg 取 PE / 换手率 / 市净率 / 总市值（静态字段，休盘也有值）。"""
    sym = ("sh" if str(market) == "1" else "sz") + str(code)
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + sym, headers=_QHDR, timeout=10,
                        proxies={"http": None, "https": None})
        txt = r.text.replace("\n", ";")
    except Exception:
        return {}
    for seg in txt.split(";"):
        if "v_" + sym + "=" in seg:
            f = seg.split("=", 1)[1].strip().strip('"').split("~")
            if len(f) > 39:
                return {
                    "price": _to_float(f[3]),
                    "pct": _to_float(f[32]),
                    "turnover": _to_float(f[38]),   # 换手率(%)
                    "pe": _to_float(f[39]),         # 市盈率(TTM)
                    "pb": _to_float(f[43]) if len(f) > 43 else None,  # 市净率
                    "mv": _to_float(f[44]) if len(f) > 44 else None,  # 总市值(亿)
                }
    return {}


def compute_risk(code: str, market: int, name: str = "", llm: bool = True) -> dict:
    qe = fetch_quote_extra(code, market)
    pe = qe.get("pe")
    turn = qe.get("turnover")
    board = classify_board(code)
    checks = []

    # 1) PE 估值
    if pe is None:
        checks.append({"key": "pe", "label": "PE估值", "flag": "⚠️", "detail": "无PE数据（可能停牌/新股）"})
    elif pe <= 0:
        checks.append({"key": "pe", "label": "PE估值", "flag": "🟡", "detail": f"亏损股 PE={pe:.0f}（无盈利支撑，谨慎）"})
    elif pe > 100:
        checks.append({"key": "pe", "label": "PE估值", "flag": "🔴", "detail": f"PE={pe:.1f} 显著偏高，注意估值泡沫"})
    elif pe > 60:
        checks.append({"key": "pe", "label": "PE估值", "flag": "🟡", "detail": f"PE={pe:.1f} 偏高，需业绩兑现"})
    else:
        checks.append({"key": "pe", "label": "PE估值", "flag": "✅", "detail": f"PE={pe:.1f} 处于合理区间"})

    # 2) 盘中换手率
    if turn is None:
        checks.append({"key": "turn", "label": "盘中换手率", "flag": "⚠️", "detail": "无换手数据"})
    elif turn > 15:
        checks.append({"key": "turn", "label": "盘中换手率", "flag": "🔴", "detail": f"换手{turn:.2f}% 异常放量，警惕高位派发"})
    elif turn > 8:
        checks.append({"key": "turn", "label": "盘中换手率", "flag": "🟡", "detail": f"换手{turn:.2f}% 偏高，关注持续性"})
    else:
        checks.append({"key": "turn", "label": "盘中换手率", "flag": "✅", "detail": f"换手{turn:.2f}% 正常"})

    # 3) 减持 / 利空公告（新闻源真实复检，可插拔 Verifier）
    nr = get_news_risk(code, market, llm=llm)
    checks.append({"key": "news", "label": "减持/利空公告", "flag": nr["flag"],
                   "detail": nr["detail"], "links": nr.get("links", []),
                   "verdict": nr.get("verdict"), "has_key": nr.get("has_key"),
                   "ai_summary": nr.get("ai_summary")})

    # 4) 交易权限（用户仅主板）
    if board["main_board"]:
        checks.append({"key": "perm", "label": "交易权限", "flag": "✅", "detail": "主板·可直接交易"})
    else:
        checks.append({"key": "perm", "label": "交易权限", "flag": "🔴",
                       "detail": f"{board['board_name']}·你仅持主板权限，无法在此下单"})

    overall = max((c["flag"] for c in checks), key=lambda k: _RANK.get(k, 0))
    return {
        "checks": checks,
        "pe": pe, "turnover": turn, "pb": qe.get("pb"),
        "mv": qe.get("mv"), "board": board, "overall": overall,
    }


# ---------------------------------------------------------------------------
# 一句话总结（原因 + 买点 + 卖点）
# ---------------------------------------------------------------------------
def _weekly_state_word(ev_extra: dict) -> str:
    return "主升" if ev_extra.get("weekly_up") else "未主升"


def _extract_verdict(raw: str) -> str:
    """从推理模型的输出中提取最终结论行。

    推理模型（如GLM）常输出完整分析过程后再给结论。
    策略：从文本后半段提取，拼接 建议+理由+时机 为一行。
    """
    import re
    # 1) 精确匹配格式化输出（理想情况：单行）
    m = re.search(r'【建议】(买|观望|不买)\s*｜【理由】.+?｜【时机】.+', raw)
    if m:
        return m.group(0).strip()

    # 1.5) 三段齐全但被拆成多行/分隔符不标准 → 各自捞出后重组
    vm = None
    for vm in re.finditer(r'【建议】\s*(买|观望|不买)', raw):
        pass  # 取最后一次出现（结论区）
    if vm:
        tail = raw[vm.end():]
        rm = re.search(r'【理由】\s*([^\n【｜|]+)', tail)
        tm = re.search(r'【时机】\s*([^\n【｜|]+)', tail)
        parts = f"【建议】{vm.group(1)}"
        if rm:
            parts += f" ｜【理由】{rm.group(1).strip()}"
        if tm:
            parts += f" ｜【时机】{tm.group(1).strip()}"
        if rm or tm:
            return parts

    # 2) 从文本后半段（通常是结论区）提取分散的建议/理由/时机
    half = raw[len(raw) // 2:]  # 只看后半段，避免命中分析过程中的中间结论

    verdict = reason = timing = ""

    vm = re.search(r'建议[设为：:\s]*(买|观望|不买)', half)
    if vm:
        verdict = vm.group(1)
        after = half[vm.end():]  # 从建议位置往后找理由和时机
        rm = re.search(r'理由[：:\s]*([^\n*]+)', after)
        if rm:
            reason = rm.group(1).strip()
        tm = re.search(r'时机[：:\s]*([^\n*]+)', after)
        if tm:
            timing = tm.group(1).strip()

    if verdict:
        parts = f"【建议】{verdict}"
        if reason:
            parts += f" ｜【理由】{reason}"
        if timing:
            parts += f" ｜【时机】{timing}"
        return parts

    # 3) 兜底：取最后一段有实质内容的文本
    paragraphs = [p.strip() for p in raw.split('\n') if p.strip()]
    for p in reversed(paragraphs):
        clean = re.sub(r'^[\s\*\d\.\-\+>]+', '', p).strip()
        if len(clean) > 8 and not clean.startswith(('```', '|', '- **')):
            return clean
    return raw.strip()[:200]


def _llm_stock_summary(ev: dict, risk: dict, kctx: dict) -> str | None:
    """调用 LLM 生成个股一句话总结（30-50字理由 + K线形态引用）。
    返回原始文本（由 stock_summary 经 _extract_verdict 提取结构化字段）。"""
    lvl = ev.get("level", "无")
    score = ev.get("score", 0)
    signals = ev.get("signals", [])
    extra = ev.get("extra", {})

    # 拼接信号摘要
    sig_lines = [s.get("label", "") for s in signals if s.get("passed")]
    sig_text = "、".join(sig_lines) if sig_lines else "无明显信号"

    # 形态事实
    shape_parts = []
    if kctx.get("in_box"):
        shape_parts.append(f"近60日箱体运行(现处{kctx.get('pos_pct', 50):.0f}%位)")
    if kctx.get("trend") and kctx["trend"] != "均线数据不足":
        shape_parts.append(kctx["trend"])
    vr = kctx.get("vol_ratio")
    if vr is not None and vr >= 1.5:
        shape_parts.append("今日放量≥1.5倍")
    elif vr is not None and vr <= 0.7:
        shape_parts.append("今日缩量≤0.7倍")
    shape_text = "；".join(shape_parts) if shape_parts else "形态数据不足"

    # 风险摘要
    board = risk.get("board", {})
    pe = risk.get("pe")
    turnover = risk.get("turnover")
    risk_parts = []
    if pe is not None:
        risk_parts.append(f"PE{pe:.1f}")
    if turnover is not None:
        risk_parts.append(f"换手{turnover:.1f}%")
    news = risk.get("news_summary", "")
    if news:
        risk_parts.append(f"公告:{news[:30]}")
    risk_text = "，".join(risk_parts) if risk_parts else "风险数据不足"

    user = (
        f"股票评估结果：\n"
        f"- 综合评级：{lvl}（得分{score}/100）\n"
        f"- 通过信号：{sig_text}\n"
        f"- K线形态：{shape_text}\n"
        f"- 风险数据：{risk_text}"
    )
    sys = (
        '你是A股投研助手。基于给出的个股技术面数据，输出一句话中文总结（30-50字理由）。'
        '\n\n格式要求：'
        '\n【建议】买/观望/不卖 ｜【理由】xxx（必须引用给定形态事实，如箱体位置/量能/均线排列）｜【时机】xxx'
        '\n\n要求：'
        '- 客观引用给定数据，不编造点位和涨幅'
        '- 理由部分必须包含K线形态特征'
        '- 只输出这一句话，不要分析过程'
    )
    return call_llm(sys, user, max_tokens=1200, timeout=STOCK_LLM_TIMEOUT)


def stock_summary(ev: dict, risk: dict, llm: bool = True, kctx: dict = None) -> dict:
    """ev: eval_to_dict 结果；risk: compute_risk 结果；kctx: kline_context 形态事实。返回 {mode, text}。"""
    kctx = kctx or {}
    if llm:
        text = _llm_stock_summary(ev, risk, kctx)
        if text:
            return {"mode": "model", "text": text}
    # 规则模板（兜底）——与 LLM 输出格式对齐
    lvl = ev.get("level", "无")
    score = ev.get("score", 0)
    extra = ev.get("extra", {})
    board = risk.get("board", {})
    pe = risk.get("pe")
    turn = risk.get("turnover")
    ws = _weekly_state_word(extra)
    # 形态短语（箱体/均线/量能），拼进理由让文本更具体
    shape_bits = []
    if kctx.get("in_box"):
        shape_bits.append(f"仍在箱体运行(现处{kctx.get('pos_pct', 50):.0f}%位置)")
    elif kctx.get("pos_pct") is not None:
        shape_bits.append(f"处近60日{kctx['pos_pct']:.0f}%分位")
    if kctx.get("trend") and kctx["trend"] != "均线数据不足":
        shape_bits.append(kctx["trend"])
    vr = kctx.get("vol_ratio")
    if vr is not None and vr >= 1.5:
        shape_bits.append("今日放量")
    elif vr is not None and vr <= 0.7:
        shape_bits.append("量能萎缩")
    shape = "，".join(shape_bits)
    if lvl == "触发":
        verdict = "买"
        reason = f"技术面{score}分多信号共振" + (f"，{shape}" if shape else "")
        timing = "当前可小仓试错，放量上攻可加仓，止损日K MA20下方"
    elif lvl == "观察":
        verdict = "观望"
        reason = f"技术面{score}分部分信号通过但缺关键确认" + (f"，{shape}" if shape else "")
        timing = "等缩量回踩MA20不破或放量突破箱体上沿再入场"
    else:
        verdict = "不买"
        reason = f"技术面{score}分信号偏弱" + (f"，{shape}" if shape else "")
        timing = "继续等待趋势修复，不抄底不追高"
    perm = "" if board.get("main_board") else f"【{board.get('board_name','')}无交易权限】"
    text = (f"{perm}【建议】{verdict} ｜【理由】{reason} ｜【时机】{timing}"
            f"（周K{ws}·PE{pe if pe is not None else '?'}·换手{turn if turn is not None else '?'}%）")
    return {"mode": "rule", "text": text}


# ---- 板块 LLM 总结缓存（token 节流核心）----
# 节流策略（双管齐下，用户 Natsu 设计）：
#   1) 异动即时：板块涨跌幅相对上次 LLM 时点变动 ≥ _LLM_BIG_MOVE（突变），或前端
#      透传 force（突变即时刷新）→ 立即重算，不走定时。
#   2) 常规定时：仅当「交易时段内的定时分析槽」已到达且比上次 LLM 更新时才重算；
#      其余常规刷新复用缓存，不再烧 token。
#      时间表（本地时间，仅交易时段内生效）：
#        开盘日 10:30 前：每 10 分钟（9:40/9:50/10:00/10:10/10:20/10:30）
#        10:31-11:30：每 30 分钟（11:00/11:30，抓午市休市结论）
#        13:00 开市后：每 30 分钟（13:30/14:00/14:30/15:00），其中 13:10 额外一次
#        （开盘成交高峰）。
#   3) 非交易时段（盘后/午休/周末）：盘后数据不变，不调 LLM（_is_trading_time 门控）。
_LLM_BIG_MOVE = 1.5    # %：板块涨跌幅相对上次 LLM 时点的突变阈值（与前端 1.5% 一致），达成即立即重算
_SECTOR_LLM_SCHEDULE = [   # 定时分析槽（时, 分）；仅交易时段内、且非异动时生效
    (9, 40), (9, 50), (10, 0), (10, 10), (10, 20), (10, 30),
    (11, 0), (11, 30),
    (13, 10), (13, 30), (14, 0), (14, 30), (15, 0),
]


def _llm_slot_key(now=None):
    """返回当前已到达的最近定时槽键（如 '2026-07-31_09:40'）；未到首个槽返回 None。
    含日期前缀：跨日重启时昨日槽 key ≠ 今日，自然触发新分析；同日重启则 key 相同，
    → 与磁盘缓存里「已分析的槽」一致 → 判定不必重跑 → 零 token（重启容灾核心）。"""
    now = now or datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    key = None
    for (h, m) in _SECTOR_LLM_SCHEDULE:
        s = h * 60 + m
        if cur >= s:
            key = f"{now.year:04d}-{now.month:02d}-{now.day:02d}_{h:02d}:{m:02d}"
        else:
            break
    return key


def _llm_slot_due(cached_slot_key, now=None):
    """当前定时槽键 ≠ 上次 LLM 时槽键 → 该重新分析了（跨日/跨槽均安全）。"""
    cur = _llm_slot_key(now)
    if cur is None:
        return False
    return cur != cached_slot_key


_SECTOR_SUMMARY_CACHE: dict = {}   # name -> (text, ts, slot_key, mode)
_SECTOR_PREV_PCT: dict = {}        # name -> 上次 LLM 时的涨跌幅（突变判定用）

# ---- LLM 缓存磁盘持久化（重启容灾）----
# 问题：原 _SECTOR_SUMMARY_CACHE 纯内存，进程被杀后清空 → 重启后首请求发现「无缓存」
#       又调一遍 LLM，等于把刚分析过的内容用 token 重算。
# 修复：把 sector + market 两级 LLM 缓存落盘 JSON，进程启动时载入。
#       同日重启时当前槽 key（带日期）== 磁盘里已分析槽 key → 判为「已分析过」→
#       直接喂回原文，零 token。跨日 key 不同 → 自然触发新分析。
import json as _json
_LLM_CACHE_FILE = paths.data_path("_llm_cache.json")
_LLM_CACHE_DIRTY = False   # 任一缓存有更新则置位，请求末尾落盘一次（原子写防损坏）

def _load_llm_cache(market_cache: dict | None = None):
    """进程启动时调用一次：从磁盘恢复 sector/market 的 LLM 缓存。
    重启发生在同一交易日的某个槽内 → 当前槽 key == 磁盘已分析槽 key → 不重跑。"""
    global _SECTOR_SUMMARY_CACHE, _LLM_CACHE_DIRTY
    try:
        with open(_LLM_CACHE_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    secs = data.get("sectors")
    if isinstance(secs, dict):
        for k, v in secs.items():
            if isinstance(v, list) and len(v) == 4:
                _SECTOR_SUMMARY_CACHE[k] = (str(v[0]), float(v[1]), str(v[2]), str(v[3]))
    if isinstance(market_cache, dict):
        m = data.get("market")
        if isinstance(m, dict):
            market_cache.update(m)

def _mark_llm_cache_dirty():
    global _LLM_CACHE_DIRTY
    _LLM_CACHE_DIRTY = True

def _flush_llm_cache(market_cache: dict | None = None):
    """请求末尾调用一次：有更新才落盘（原子写，防半截损坏）。"""
    global _LLM_CACHE_DIRTY
    # 无 sector 更新 且 未提供 market → 跳过；否则落盘（market 变更也写一次，轻量）
    if not _LLM_CACHE_DIRTY and market_cache is None:
        return
    sectors = {k: [v[0], v[1], v[2], v[3]] for k, v in _SECTOR_SUMMARY_CACHE.items()}
    data = {"sectors": sectors}
    if isinstance(market_cache, dict):
        data["market"] = {kk: market_cache.get(kk) for kk in ("text", "ts", "pct", "slot")}
    tmp = _LLM_CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _LLM_CACHE_FILE)   # 原子替换，避免写一半被读到
        _LLM_CACHE_DIRTY = False
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass



def _is_trading_time(dt=None) -> bool:
    """A股交易时段判定（本地时间）：周一~周五，上午 9:15-11:30，下午 13:00-15:00。
    非交易时段盘面不变，此间调用 LLM 属空烧 token，故在 LLM 路径上加门控。"""
    dt = dt or datetime.datetime.now()
    if dt.weekday() >= 5:           # 5=周六，6=周日
        return False
    t = dt.time()
    morning = datetime.time(9, 15) <= t <= datetime.time(11, 30)
    afternoon = datetime.time(13, 0) <= t <= datetime.time(15, 0)
    return morning or afternoon


def sector_summary(sector: dict, llm: bool = False, force: bool = False) -> dict:
    """sector: 板块矩阵中的一项（含 pct / state / weekly_up / leader / main_net）。
    输出4段式模板：定调 / 分析 / 资金 / 建议（v2 统一）。
    ★板块层零 LLM：llm 参数仅保留为向后兼容签名，本函数永远走免费规则模板，
    不再内嵌大模型异动分析（战略判断统一由 §6.8 叠加层承担）。
    force: 突变即时刷新时前端透传，绕过 3 分钟地板立即重算。"""
    name = sector.get("name", "")
    pct = sector.get("pct") or 0
    state = sector.get("state", "未知")
    up = sector.get("weekly_up")
    leader = sector.get("leader")
    net = sector.get("main_net")

    # 1) 定调（着色加粗）
    if up and pct >= 2:
        tone = "可持续跟踪找机会买入"
    elif up:
        tone = "主升中可逢低布局"
    elif pct > 0:
        tone = "短线反弹不追高"
    elif pct <= -2:
        tone = "下降趋势注意止损"
    else:
        tone = "弱势整理观望"

    # 2) 分析（规则硬推：为什么涨/跌 —— 左/右二合一，结合龙头+资金方向）
    if up:
        analysis = f"技术面多头排列（周K主升），今日{pct:+.2f}%"
    else:
        analysis = f"周K未主升，今日{pct:+.2f}%，反弹持续性待观察"
    if leader:
        analysis += f"，龙头{leader['name']}({leader.get('pct', 0):+.2f}%)带动"
    else:
        analysis += "，无明显龙头带动"
    if net is not None:
        analysis += "，资金净流入支撑" if net >= 0 else "，资金流出承压"

    # 3) 资金
    if net is not None:
        net_yi = net / 1e8
        fund = f"{'净流入' if net >= 0 else '流出'}{abs(net_yi):.1f}亿"
    else:
        fund = "数据暂缺"

    # 4) 建议（规则硬推）
    if up and pct >= 2:
        advice = "可持续跟踪，逢回调找买点"
    elif up:
        advice = "主升中可逢低布局"
    elif pct > 0:
        advice = "短线反弹不追高，观察持续性"
    elif pct <= -2:
        advice = "下降趋势注意风险控制"
    else:
        advice = "弱势整理，观望为主"

    lines = [f"【定调】{tone}", f"【分析】{analysis}",
             f"【资金】{fund}", f"【建议】{advice}"]
    text = chr(10).join(lines)
    # 缓存规则模板结果：避免 LLM 持续返回脏文本时反复空烧（纯 llm=False 不缓存）
    if llm:
        _SECTOR_SUMMARY_CACHE[name] = (text, time.time(), _llm_slot_key(), "rule")
        _SECTOR_PREV_PCT[name] = pct
        _mark_llm_cache_dirty()
    return {"mode": "rule", "text": text}






def _llm_market_commentary(indices_detail: dict) -> str | None:
    """调用 LLM 生成大盘走势评述（增强版）。"""
    parts = []
    for name in ["上证指数", "深证成指", "创业板指", "科创50"]:
        d = indices_detail.get(name, {})
        if not d or "pattern" not in d:
            continue
        parts.append(f"{name}：现价{d.get('close','-')}，今日{d.get('pct_today',0):+.2f}%，"
                     f"{d.get('pattern','')}，RSI{d.get('rsi','-')}")
    if not parts:
        return None
    user = "四大指数技术面数据：\n" + "\n".join(parts)
    sys = (
        "你是一位资深A股市场分析师。基于给出的四大指数（上证/深证成指/创业板/科创50）"
        "的技术面数据，写一段80-150字的大盘走势评述。\n\n"
        "必须覆盖：\n"
        "1. 主板 vs 双创强弱对比\n"
        "2. 量价形态判断（量价齐升/放量滞涨/缩量阴跌等）\n"
        "3. 关键位提示（上方阻力位、下方支撑位）\n"
        "4. 短线操作建议（仓位/方向）\n\n"
        "要求：\n"
        "- 客观引用给定数据，不编造点位\n"
        "- 不输出分析过程，直接给评述正文\n"
        "- 语气简洁专业，像券商晨会纪要"
    )
    raw = call_llm(sys, user, max_tokens=800)
    if raw:
        # 取前3段有效文本（模型可能带前后缀）
        lines = [l.strip() for l in raw.strip().split('\n') if l.strip()]
        # 过滤掉明显的元信息行
        filtered = [l for l in lines if not l.startswith(('#', '```', '分析', '评述', '-'))]
        return '\n'.join(filtered[:4]) if filtered else raw.strip()[:300]
    return None
