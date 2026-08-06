"""新闻/公告抓取 + 利空关键词粗筛（零 token）+ 可插拔精判（LLM 可选）。

数据源：巨潮网（cninfo）公告，经 easy_tdx 直连（证监会旗下官方披露平台，
非东方财富、不依赖 TDX 行情服务器）。失败/受限则降级为 ⚠️ + 原文链接。
架构（token 极小）：
  ① 本地抓取（零 token，6h 缓存）  ② 本地关键词粗筛（零 token）
  ③ 精判层（可插拔 Verifier）：
      有用户自填 LLM key → 调模型解读影响（仅发命中项标题，极小 token）
      无 key            → 直接给报告原文超链接，不本地标注，界面明示「功能受限」
      （不做 ollama）

key 管理：作者 key 仅本机；分发包 = 无 key 骨架 + config.example.json + 说明文档；
朋友各自填自己的 key（存项目目录外隔离位置）。本文件不含任何硬编码 key。
"""
from __future__ import annotations

import os
import time
import json
import threading

import paths
from llm_client import load_config
from easy_tdx.cninfo import CninfoClient
_CACHE_DIR = os.path.join(paths.data_dir(), ".news_cache")
_lock = threading.Lock()
_TTL = 3600 * 6  # 同票 6 小时内不重抓、不重发
_NEWS_SUMMARY_CACHE: dict = {}   # code -> str|None（AI 公告解读缓存，进程级）

# 利空关键词（可在 config.json news.neg_keywords 覆盖）
NEG_DEFAULT = ["减持", "退市", "立案", "业绩预减", "商誉减值", "重大诉讼",
               "问询函", "警示函", "终止上市", "预亏", "业绩变脸", "被ST"]


def _cache_get(code):
    try:
        p = os.path.join(_CACHE_DIR, code + ".json")
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < _TTL:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _cache_set(code, data):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_CACHE_DIR, code + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _cninfo_announcements(code, market, limit):
    """巨潮网(cninfo)个股公告，经 easy_tdx。返回 [{title,date,url,source}]。

    数据源为证监会旗下巨潮披露平台，非东方财富、亦不依赖 TDX 行情服务器。
    code 传纯代码（如 688017），巨潮据此检索；market 仅用于生成原文总览链接。
    """
    try:
        df = CninfoClient().get_announcements(code, count=limit)
    except Exception as e:
        return [], f"巨潮公告源异常：{e}"
    if df is None or getattr(df, "empty", True):
        return [], "巨潮公告源暂无可读数据"
    items = []
    for _, r in df.iterrows():
        title = str(r.get("title") or "")
        if not title:
            continue
        date = str(r.get("date") or "")[:10]
        link = r.get("pdf_url") or r.get("url") or ""
        items.append({"title": title, "date": date, "url": link, "source": "巨潮公告"})
    return items, None


def fetch_announcements(code, market=1, limit=30, provider="cninfo"):
    """返回 (items, note)。note 非空表示抓取失败。

    数据源：巨潮网(cninfo) via easy_tdx（原东方财富源已移除）。
    provider 仅保留签名兼容，实际固定走 cninfo。
    """
    return _cninfo_announcements(code, market, limit)


def scan(items, keywords):
    """本地关键词粗筛（零 token）。命中返回带 kw 的副本。"""
    hits = []
    for it in items:
        title = it.get("title") or ""
        for kw in keywords:
            if kw and kw in title:
                hits.append({**it, "kw": kw})
                break
    return hits


def _llm_news_verdict(hits):
    """仅把命中项标题发给模型，给出实质性影响研判（不是套模板话术）。"""
    from llm_client import call_llm
    bullets = "\n".join(f"- 《{h['title']}》（{h.get('date','')}，命中：{h['kw']}）"
                         for h in hits[:8])
    sys = (
        "你是A股投研助手。下面是个股近期公告中命中利空关键词的条目（仅标题）。\n"
        "请根据这些标题的实际内容，用一句话（60~120字）给出实质性影响判断。\n\n"
        "要求：\n"
        "- 必须结合具体事件说明（如：涉及金额、诉讼进展、监管措施类型、业绩影响等），\n"
        "  不要输出「重大利空」「影响程度中」这种放之四海皆准的空话。\n"
        "- 区分「已落地」与「潜在风险」（如：立案调查 vs 问询函；实际减持 vs 预披露）。\n"
        "- 末尾标注：【严重】【中等】【轻微】【待观察】之一。\n"
        "- 直接输出结论，不要分析过程。"
    )
    user = f"命中利空公告（共{len(hits)}条）：\n" + bullets
    raw = call_llm(sys, user, max_tokens=400, timeout=45)
    if not raw:
        return None
    # 后处理：提取含等级标记的实质内容
    import re
    clean = re.sub(r'```.*?```', '', raw, flags=re.DOTALL).strip()
    m = re.search(r'.+【(严重|中等|轻���|待观察)】', clean, re.DOTALL)
    if m:
        return m.group(0).strip()
    # 兜底
    paragraphs = [p.strip() for p in clean.split('\n') if p.strip()]
    best = ""
    for p in paragraphs:
        c = re.sub(r'^[\s\*\d\.\-\+>●◆■]+', '', p).strip()
        if len(c) > len(best) and not c.startswith(('```', '|', '- **', '输出格式')):
            best = c
    return best if len(best) > 15 else clean[:200]


def _llm_news_summary(items):
    """API 启用时，对近3个月公告做实质性解读（一句话结论）。

    不是笼统的「未发现利空」，而是提炼关键事件：
    业绩表现、融资/定增/回购、重大合同/中标、股东变动（增持/减持/
    质押）、分红送转、监管问询、诉讼仲裁等。
    """
    from llm_client import call_llm
    # 取最近 20 条标题（覆盖 ~3 个月核心公告；巨潮按时间倒排）
    titles = "\n".join(f"- {it['title']}（{it.get('date','')}）" for it in items[:20])
    sys = (
        "你是A股投研助手。下面是一只股票近3个月的公告标题列表（仅标题，无正文）。\n"
        "请仔细阅读所有标题，用一句话（80~120字）给出实质性结论，而非泛泛而谈。\n\n"
        "要求：\n"
        "- 提炼关键事件：业绩（同比增减）、融资（定增/发债/可转债）、回购/增持/减持、\n"
        "  重大合同/中标/投资、分红送转、监管函件（问询/警示/立案）、诉讼仲裁等。\n"
        "- 若近期无特别事件则写明「近期以常规披露为主，无重大事项」。\n"
        "- 末尾标注整体倾向：【偏多】/【中性】/【偏空】。\n"
        "- 直接输出结论，不要分析过程。"
    )
    user = f"股票公告（近3个月，共{len(items)}条）：\n" + titles
    raw = call_llm(sys, user, max_tokens=600, timeout=45)
    if not raw:
        return None
    # 后处理：提取有实质内容的段落
    import re
    raw = re.sub(r'```.*?```', '', raw, flags=re.DOTALL).strip()
    # 优先提取含【偏多/中性/偏空】标记的行
    m = re.search(r'.+【(偏多|中性|偏空)】', raw, re.DOTALL)
    if m:
        return m.group(0).strip()
    # 兜底：取最长的一段有意义文本
    paragraphs = [p.strip() for p in raw.split('\n') if p.strip()]
    best = ""
    for p in paragraphs:
        clean = re.sub(r'^[\s\*\d\.\-\+>●◆■]+', '', p).strip()
        if len(clean) > len(best) and not clean.startswith(('```', '|', '- **', '输出格式')):
            best = clean
    return best if len(best) > 15 else raw.strip()[:200]


def verify(hits, items=None, code=None, llm=True):
    """精判层（可插拔 Verifier）。

    items: 全部公告列表（用于无命中时的 AI 概括）。
    code: 股票代码（用于 AI 解读缓存）。
    """
    llm_cfg = load_config().get("llm", {})
    has_key = bool(llm_cfg.get("enabled") and llm_cfg.get("api_key")) and llm
    if not hits:
        # 无利空命中 → API 启用时让模型解读近3个月公告
        summary = None
        if has_key and items:
            # 先查缓存（同进程内不重复调 LLM）
            if code:
                summary = _NEWS_SUMMARY_CACHE.get(code)
            if summary is None:
                summary = _llm_news_summary(items)
                if code:
                    _NEWS_SUMMARY_CACHE[code] = summary  # None 也缓存，避免反复空烧
        if summary:
            return {"flag": "✅", "detail": summary,
                    "links": [], "verdict": None, "has_key": has_key,
                    "ai_summary": True}
        # 兜底：无 key 或 LLM 失败
        fallback = "公告源正常但AI解读暂不可用" if has_key else "未配置AI模型"
        detail = f"{fallback}（查看原文自行研判）"
        return {"flag": "✅", "detail": detail,
                "links": [], "verdict": None, "has_key": has_key,
                "ai_summary": False}
    links = [{"title": h["title"], "url": h["url"], "kw": h["kw"], "date": h.get("date", "")}
             for h in hits]
    if has_key:
        verdict = _llm_news_verdict(hits)
        return {"flag": "🔴", "detail": f"命中利空关键词 {len(hits)} 条（详见原文链接）",
                "links": links, "verdict": verdict, "has_key": True,
                "ai_summary": bool(verdict)}
    # 无 key：直接给原文超链接，不本地标注（用户自己看）
    return {"flag": "🔴", "detail": f"命中利空关键词 {len(hits)} 条 · 未配置模型，请点击原文自行研判",
            "links": links, "verdict": None, "has_key": False,
            "ai_summary": False}


def get_news_risk(code, market=1, cfg=None, llm=True):
    """对外：返回风险复检第3项片段。无论成败都附带『查看原文』超链接。"""
    cfg = cfg or load_config()
    news_cfg = cfg.get("news", {})
    if news_cfg.get("enabled") is False:
        return {"flag": "⚠️", "detail": "新闻源未启用（config.news.enabled=false）", "links": []}
    keywords = news_cfg.get("neg_keywords") or NEG_DEFAULT
    limit = int(news_cfg.get("limit_days") or 90)   # 默认90条≈近3个月
    provider = news_cfg.get("provider") or "cninfo"
    center = f"https://www.cninfo.com.cn/new/disclosure/stock?stockCode={code}"

    cached = _cache_get(code)
    if cached is not None:
        items, note = cached.get("items", []), cached.get("note")
    else:
        items, note = fetch_announcements(code, market, limit, provider)
        _cache_set(code, {"items": items, "note": note})

    if note:
        return {"flag": "⚠️", "detail": f"新闻源暂不可达（{note}）；可点原文自行核查",
                "links": [{"title": "查看该股公告原文 ↗", "url": center, "kw": "", "date": ""}]}
    hits = scan(items, keywords)
    res = verify(hits, items=items, code=code, llm=llm)
    if not res["links"]:  # 无命中时也给原文链接，便于人工核查
        res = {**res, "links": [{"title": "查看该股公告原文 ↗", "url": center, "kw": "", "date": ""}]}
    return res
