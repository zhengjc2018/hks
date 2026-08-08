"""A股机会雷达 —— 后端服务（多周期共振框架 / 多数据源容灾）。

数据源策略（按稳定性分层）：
  · 指数实时 / 大盘定调  → 腾讯 gtimg（稳）
  · K线（日/周/60分/15分）→ 新浪财经（稳，立即可用）
  · 板块列表 / 成分股 / 涨跌家数 → 东方财富（功能最全；临时限流时降级，恢复后自动可用）

启动： python server.py   浏览器打开 http://127.0.0.1:5000
依赖： flask, requests（venv 已装）
"""
from __future__ import annotations
import time
import json
import threading
import re
import os
import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import requests
from flask import Flask, send_from_directory, request, jsonify, Response

from analysis import (parse_kline, ma, ma_series, macd, rsi, kdj,
                      evaluate_stock, eval_to_dict, kline_context,
                      index_technical, classify_sector)
from board import classify_board
from risk import compute_risk, stock_summary, sector_summary, _llm_market_commentary, _is_trading_time, _llm_slot_due, _llm_slot_key, _load_llm_cache, _flush_llm_cache, _mark_llm_cache_dirty
from llm_client import public_config, save_config, call_llm, load_config
import tdx_source as tdx
from easy_tdx import BoardType, Period
from overlay import apply_overlay, board_ok_for_date   # ★v2.1 §6.8 战略叠加层（用户 8-2 授权实施）
import picks   # ★选股买卖点实时扫描（配套 选股买卖点SOP.md v1；后台预计算，/api/picks 秒回缓存）
import sell     # ★持仓卖出信号（高位十字星/长下影→清仓、盈利≥30%→减仓；读 positions.json）
import lifecycle # ★个股全生命周期状态机（双通道进自选→观察池→盘中即时提示/盘尾确认→上车→持仓→卖出三层；挂钩≥1天已废弃）
import t_trade  # ★做 T 信号 / T 仓状态（移植 a-trade，数据源沿用 HKS 统一 K 线入口）
import gap_pick  # ★次日高开候选（移植 a-trade，数据源沿用 HKS 统一 K 线 / 东方财富入口）
import gap_model  # ★次日高开排序模型推理（纯 numpy，无模型时回退规则评分）
import app_update  # ★Windows EXE 自动更新（检查/下载/退出后替换重启）
import paths
_TDX_LOCK = threading.Lock()   # easy_tdx 非线程安全：并发 kline/board_members 互相踩坏连接→静默返回 None。全局串行化 TDX 调用。

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
EM_BASE = "https://push2.eastmoney.com"
EM_HIS = "https://push2his.eastmoney.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://quote.eastmoney.com/"}
SINA_HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
# 行情/外部数据源一律绕过系统代理直连（开发代理 3002 当前不可用，会被它拦截）
NO_PROXY = {"http": None, "https": None}
TENCENT_IDX = [("sh000001", "上证指数"), ("sz399001", "深证成指"),
               ("sz399006", "创业板指"), ("sh000300", "沪深300"),
               ("sh000688", "科创50")]

# 大盘评述 LLM 节流（与板块总结同一思路：交易时段定时槽 + 指数突变即时）
_MARKET_LLM_CACHE: dict = {"text": None, "ts": 0.0, "pct": None, "slot": None}
_MARKET_LLM_BIG_MOVE = 0.8    # %：任一指数涨跌幅相对上次变化达此值即视为突变
# 启动时从磁盘恢复 sector+market LLM 缓存（重启容灾：不补跑已过的槽，省 token）
_load_llm_cache(_MARKET_LLM_CACHE)

# 板块 LLM 分析「资金参与度门槛」（百分比，前端可调，自适应板块大小）。
# 含义：板块 |主力净流入| 占「自身成交额」比例 ≥ 此值，才调大模型持续监控；
# 例：小盘(成交30亿)净流入5亿→占比16.7% 敏感度远高于 大盘(成交200亿)净流入10亿→5%。
# 其余占比低的板块 → 只渲染免费规则模板，零 token。
# 默认 2.0(%)。前端「监控门槛」输入框实时下发 ?net_pct= 覆盖此值；想更宽松改 1.0，更严格改 3.0。
SECTOR_LLM_MIN_NET_PCT = 2.0

# 板块日级涨跌幅滚动窗口（给 LLM 提供历史趋势上下文，防“单日暴涨=强势”模板误判）。
# 结构：{bk: [(date_str, pct), ...]}，每个交易日保留一条当日最终快照。
# 用途：记录板块日级涨跌滚动窗口，作为趋势上下文（★板块层零 LLM 后仅供规则模板/前端趋势展示参考）。
_SECTOR_DAILY_PCTS: dict[str, list[tuple[str, float]]] = {}
_SECTOR_DAILY_MAX = 15          # 保留最近15个交易日（约3周）

app = Flask(__name__, static_folder=paths.bundle_path("frontend"), static_url_path="/assets")
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0   # 开发期禁用静态文件缓存，避免前端改完强刷仍看到旧版

# ---------------------------------------------------------------------------
# 缓存 + 全局速率限制
# ---------------------------------------------------------------------------
_cache: dict = {}
_cache_lock = threading.Lock()
_last_req = 0.0
_req_lock = threading.Lock()


def cache_get(key, ttl):
    with _cache_lock:
        if key in _cache:
            ts, val = _cache[key]
            if time.time() - ts < ttl:
                return val
    return None


def cache_set(key, val):
    with _cache_lock:
        _cache[key] = (time.time(), val)


def _rate_limit(gap=0.06):
    global _last_req
    with _req_lock:
        now = time.time()
        wait = gap - (now - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.time()


def _isnan(v):
    try:
        return v is None or (isinstance(v, float) and v != v)
    except Exception:
        return True


def _call_timeout(fn, seconds, default=None, label=""):
    """在独立线程跑 fn，超时（seconds）则放弃并返回 default。用于无硬超时的外部源（通达信本地客户端）。
    注意：必须用 shutdown(wait=False)，否则 hung worker 会让 executor __exit__ 永远等待，导致调用方也挂死。
    """
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=seconds)
        except FuturesTimeout:
            print(f"[timeout] {label or getattr(fn,'__name__','?')} 超过 {seconds}s，放弃")
            return default
        except Exception as e:
            print(f"[err] {label}: {e}")
            return default
    finally:
        ex.shutdown(wait=False)


def em_get(base, path, params, timeout=5, retries=2):
    """东方财富请求（重试 + 指数退避）。封禁期间会返回 None。"""
    delay = 0.3
    for _ in range(retries):
        _rate_limit()
        try:
            r = requests.get(base + path, params=params,
                             headers=HEADERS, timeout=timeout, proxies=NO_PROXY)
            if r.status_code != 200:
                time.sleep(delay); delay *= 2; continue
            return r.json()
        except Exception:
            time.sleep(delay); delay *= 2
    return None


# ---------------------------------------------------------------------------
# 新浪 K线（日/周/60分/15分）
# ---------------------------------------------------------------------------
def _sina_symbol(secid: str) -> str:
    m, code = secid.split(".")
    return ("sh" if m == "1" else "sz") + code


def _sina_to_internal(rows):
    out, prev = [], None
    for r in rows or []:
        try:
            o, c = float(r["open"]), float(r["close"])
            h, l, v = float(r["high"]), float(r["low"]), float(r["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        pct = (c - prev) / prev * 100 if prev else 0.0
        amp = (h - l) / c * 100 if c else 0.0
        out.append({"date": r["day"], "open": o, "close": c, "high": h, "low": l,
                    "vol": v, "amount": 0.0, "amp": round(amp, 2),
                    "pct": round(pct, 2), "change": round(c - prev, 2) if prev else 0.0,
                    "turnover": 0.0})
        prev = c
    return out


def _agg_weekly(daily):
    wk, buf = [], []
    for d in daily:
        buf.append(d)
        if len(buf) == 5:
            wk.append({"date": buf[-1]["date"], "open": buf[0]["open"],
                       "close": buf[-1]["close"],
                       "high": max(x["high"] for x in buf),
                       "low": min(x["low"] for x in buf),
                       "vol": sum(x["vol"] for x in buf)})
            buf = []
    if buf:
        wk.append({"date": buf[-1]["date"], "open": buf[0]["open"],
                   "close": buf[-1]["close"],
                   "high": max(x["high"] for x in buf),
                   "low": min(x["low"] for x in buf),
                   "vol": sum(x["vol"] for x in buf)})
    return wk


def _sina_k(sym, scale, lmt):
    _rate_limit()
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData")
    try:
        r = requests.get(url, params={"symbol": sym, "scale": scale,
                                      "ma": 5, "datalen": lmt},
                         headers=SINA_HDR, timeout=10, proxies=NO_PROXY)
        return r.json() or []
    except Exception:
        return []


_PERIOD = {101: Period.DAILY, 102: Period.WEEKLY, 5: Period.MIN_5,
           15: Period.MIN_15, 60: Period.MIN_60}


def _tdx_klines(secid, klt, lmt):
    """通达信 K线 → 内部统一格式（与 _sina_to_internal 输出同构）。"""
    m, code = secid.split(".")
    df = _call_timeout(lambda: tdx.kline(m, code, period=_PERIOD.get(klt, Period.DAILY), count=lmt),
                       8, None, "tdx.kline")
    if df is None:
        raise RuntimeError("tdx.kline 超时/无数据")
    rows, prev = [], None
    for _, r in df.iterrows():
        o = float(r["open"]); c = float(r["close"])
        h = float(r["high"]); l = float(r["low"])
        v = float(r["vol"]); amt = float(r.get("amount") or 0.0)
        row = {"date": str(r["datetime"])[:10], "open": o, "close": c,
               "high": h, "low": l, "vol": v, "amount": amt,
               "amp": round((h - l) / c * 100, 2) if c else 0.0,
               "pct": 0.0, "change": 0.0, "turnover": 0.0}
        if prev:
            row["pct"] = round((c - prev) / prev * 100, 2)
            row["change"] = round(c - prev, 2)
        prev = c
        rows.append(row)
    return rows


def _klines(secid, klt, lmt):
    """统一 K线入口。klt: 101日 102周 60=60分 15=15分。
    通达信优先，失败回退新浪。"""
    try:
        if tdx.available():
            return _tdx_klines(secid, klt, lmt)
    except Exception as e:
        print("[tdx] klines fallback -> sina:", e)
    sym = _sina_symbol(secid)
    if klt == 102:  # 周K：拉日K聚合（新浪无直接周K）
        rows = _sina_k(sym, 240, max(lmt * 5, 250))
        return _agg_weekly(_sina_to_internal(rows))
    scale = {101: 240, 5: 5, 60: 60, 15: 15}.get(klt, 240)
    return _sina_to_internal(_sina_k(sym, scale, lmt))


def _fetch_all_klines(secid):
    """并行拉取 4 条 K 线（日/周/60分/15分）。TDX 可用时本地快、串行即可；
    TDX 熔断走新浪兜底时并发拉取，消除 4 次串行累加延迟（单条 timeout=10）。"""
    specs = [(101, 120), (102, 60), (60, 60), (15, 60)]
    if tdx.available():
        return tuple(_klines(secid, k, l) for k, l in specs)
    with ThreadPoolExecutor(max_workers=4) as ex:
        fut = {ex.submit(_klines, secid, k, l): (k, l) for k, l in specs}
        res = {}
        for f in fut:
            k, l = fut[f]
            try:
                res[(k, l)] = f.result()
            except Exception:
                res[(k, l)] = None
    return res[(101, 120)], res[(102, 60)], res[(60, 60)], res[(15, 60)]


# ---------------------------------------------------------------------------
# 数据聚合
# ---------------------------------------------------------------------------
def _indices_tencent_fallback():
    q = ",".join(c for c, _ in TENCENT_IDX)
    _rate_limit()
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + q,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10, proxies=NO_PROXY)
        txt = r.text.replace("\n", ";")
    except Exception:
        return []
    out = []
    for code, name in TENCENT_IDX:
        for seg in txt.split(";"):
            if "v_" + code + "=" in seg:
                f = seg.split("=", 1)[1].strip().strip('"').split("~")
                # f[3]=现价 f[4]=昨收 f[31]=涨跌(点) f[32]=涨跌(%)
                # 直接用 (现价-昨收)/昨收 计算百分比，避免字段位错
                if len(f) > 4:
                    price = float(f[3])
                    prev = float(f[4]) if f[4] not in ("", "-") else None
                    pct = round((price - prev) / prev * 100, 2) if prev else (
                        float(f[32]) if len(f) > 32 and f[32] not in ("", "-") else 0.0)
                    out.append({"code": code[2:], "name": name,
                                "price": price, "pct": pct,
                                "amount": None, "main_net": None})
                break
    return out


def get_indices_tencent():
    """指数实时。通达信优先，失败回退腾讯 gtimg。"""
    try:
        if tdx.available():
            idx_map = [("1", "000001", "上证指数"), ("0", "399001", "深证成指"),
                       ("0", "399006", "创业板指"), ("1", "000300", "沪深300"),
                       ("1", "000688", "科创50")]
            df = _call_timeout(lambda: tdx.quotes([(m, c) for m, c, _ in idx_map]),
                               8, None, "tdx.quotes")
            if df is None:
                print("[tdx] quotes 超时/失败，转腾讯回退")
            else:
                out = []
                for _, r in df.iterrows():
                    price = float(r["close"])
                    prev = float(r["pre_close"]) if not _isnan(r.get("pre_close")) else None
                    pct = round((price - prev) / prev * 100, 2) if prev else 0.0
                    out.append({"code": str(r["code"]), "name": str(r.get("name", "")),
                                "price": price, "pct": pct,
                                "amount": None, "main_net": None})
                if out:
                    return out
    except Exception as e:
        print("[tdx] indices fallback -> tencent:", e)
    return _indices_tencent_fallback()


def get_index_trend():
    kl = _klines("1.000001", 101, 120)
    if not kl or len(kl) < 60:
        return {"state": "未知"}
    closes = [x["close"] for x in kl]
    m20, m60 = ma(closes, 20), ma(closes, 60)
    dif, dea, _ = macd(closes)
    up = m20 is not None and m60 is not None and m20 > m60
    golden = dif[-1] is not None and dea[-1] is not None and \
        dif[-1] > dea[-1] and dif[-2] <= dea[-2]
    return {"state": "主升" if up else "调整",
            "ma20": round(m20, 2) if m20 else None,
            "ma60": round(m60, 2) if m60 else None, "golden": golden}


# ---- 大盘走势评述（多指数技术面 + 支撑阻力 + 量价形态） ----
_INDEX_KEYS = [
    ("1", "000001", "上证指数"),
    ("0", "399001", "深证成指"),
    ("0", "399006", "创业板指"),
    ("1", "000688", "科创50"),
]


def market_commentary(force: bool = False) -> dict:
    """拉取4大指数日K，逐个做技术分析，输出结构化评述。

    返回:
      indices_detail: {name: index_technical结果}
      summary:        拼好的中文评述文本（含主板/双创对比、资金面、量能、关键位）
      llm_text:       若配置了模型，额外生成LLM版评述（可选）
    """
    detail = {}
    for mkt, code, name in _INDEX_KEYS:
        try:
            kl = _klines(f"{mkt}.{code}", 101, 120)
            if kl and len(kl) >= 20:
                detail[name] = index_technical(kl)
            else:
                detail[name] = {"pattern": f"{name}K线数据不足"}
        except Exception as e:
            detail[name] = {"pattern": f"{name}获取失败({e})"}

    # 组装规则版评述（多行结构化，每行一个主题）
    sh = detail.get("上证指数", {})
    sz = detail.get("深证成指", {})
    cy = detail.get("创业板指", {})
    kc = detail.get("科创50", {})

    sh_pct = sh.get("pct_today", 0)
    cy_pct = cy.get("pct_today", 0)
    kc_pct = kc.get("pct_today", 0)

    # ---- 第1行：【定调】（前端着色加粗） ----
    # 判断依据：上证趋势 + 涨跌家数(从get_market传入的adv) + 各指数红绿
    all_red = sh_pct > 0 and cy_pct > 0 and kc_pct > 0
    all_green = sh_pct < 0 and cy_pct < 0 and kc_pct < 0
    sh_ma = sh.get("ma_arrange", "")
    sh_macd = sh.get("macd_state", "")

    if "多头" in sh_ma and ("金叉" in sh_macd or "多头" in sh_macd):
        tone = "可持续跟踪找机会买入"
    elif "空头" in sh_ma or ("死叉" in sh_macd or "空头" in sh_macd):
        if abs(sh_pct) > 2:
            tone = "下降趋势注意止损"
        else:
            tone = "未主升不追高，轻仓观望"
    elif sh_pct > 0.5:
        tone = "震荡偏强，可寻找机会"
    elif sh_pct < -0.5:
        tone = "弱势调整，控仓等待"
    else:
        tone = "窄幅震荡，观望为主"

    lines = [f"【定调】{tone}"]

    # ---- ���2-N行：各指数分别一行 ----
    for nm, d in [("上证指数", sh), ("创业板指", cy), ("科创50", kc)]:
        c = d.get("close")
        pct = d.get("pct_today", 0)
        if not c or "数据不足" in d.get("pattern", ""):
            lines.append(f"{nm}：数据获取中...")
            continue
        ma = d.get("ma_arrange", "")
        macd = d.get("macd_state", "")
        vp = d.get("vol_price", "量能平稳")
        sup = d.get("support")
        res = d.get("resistance")
        parts = [f"{nm}({c}，{pct:+.2f}%)", f"{ma}", f"MACD{macd}", vp]
        if sup:
            parts.append(f"支撑≈{sup}")
        if res:
            parts.append(f"阻力≈{res}")
        lines.append("；".join(parts))

    # ---- 量能总结行 ----
    vol_states = [d.get("vol_price", "") for d in [sh, sz, cy, kc]
                  if d.get("vol_price") and d["vol_price"] != "量能平稳"]
    if vol_states:
        from collections import Counter
        top_vol = Counter(vol_states).most_common(1)[0][0]
        lines.append(f"【量能】{top_vol}")
    else:
        lines.append("【量能】平稳")

    rule_text = "\n".join(lines)

    # LLM 增强版（可选，不在前台展示，仅后端记录用于调试）
    # 节流：3 分钟地板 + 任一指数涨跌幅突变(≥0.8%)即时；规则版评述仍实时刷新
    llm_text = None
    if public_config().get("enabled") and _is_trading_time():
        now = time.time()
        sig = (round(sh_pct, 1), round(cy_pct, 1), round(kc_pct, 1))
        prev = _MARKET_LLM_CACHE.get("pct")
        big = prev is not None and any(
            abs(a - b) >= _MARKET_LLM_BIG_MOVE for a, b in zip(sig, prev))
        if _MARKET_LLM_CACHE.get("text") and \
                not big and not force and not _llm_slot_due(_MARKET_LLM_CACHE.get("slot")):
            llm_text = _MARKET_LLM_CACHE["text"]
        else:
            try:
                llm_text = _llm_market_commentary(detail)
            except Exception:
                pass
            if llm_text:
                _MARKET_LLM_CACHE["text"] = llm_text
                _MARKET_LLM_CACHE["ts"] = now
                _MARKET_LLM_CACHE["pct"] = sig
                _MARKET_LLM_CACHE["slot"] = _llm_slot_key()
                _mark_llm_cache_dirty()

    _flush_llm_cache(_MARKET_LLM_CACHE)
    return {
        "indices_detail": detail,
        "summary": rule_text,
        "llm_summary": llm_text,
    }


def get_breadth():
    """涨跌家数（东方财富）。封禁时返回 None。首屏关键路径，用短超时。"""
    up = down = flat = zt = dt = total = 0
    pct_sum = 0.0
    pn = 1
    while True:
        data = em_get(EM_BASE, "/api/qt/clist/get", {
            "pn": pn, "pz": 1000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f3"},
            timeout=3, retries=1)
        if not data or not data.get("data") or not data["data"].get("diff"):
            return None
        diff = data["data"]["diff"]
        for d in diff:
            v = d.get("f3")
            if isinstance(v, (int, float)):
                total += 1
                pct_sum += v
                if v > 0: up += 1
                elif v < 0: down += 1
                else: flat += 1
                if v >= 9.5: zt += 1
                if v <= -9.5: dt += 1
        if len(diff) < 1000:
            break
        pn += 1
    adv = round(up / (up + down) * 100, 1) if (up + down) else 50.0
    return {"up": up, "down": down, "flat": flat, "zt": zt, "dt": dt,
            "total": total, "adv_ratio": adv,
            "avg_pct": round(pct_sum / total, 2) if total else 0.0}


def get_market(force: bool = False):
    idx = get_indices_tencent()
    # 涨跌家数 / 指数趋势 任一失败都不应拖垮整轮大盘请求
    try:
        breadth = get_breadth()
    except Exception as e:
        print("[market] breadth err:", e)
        breadth = None
    try:
        trend = get_index_trend()
    except Exception as e:
        print("[market] trend err:", e)
        trend = {"state": "未知"}
    adv = breadth.get("adv_ratio", 50) if breadth else 50
    if trend.get("state") == "主升" and adv >= 55:
        posture = "可操作（轻仓上）"
    elif trend.get("state") == "主升":
        posture = "主升但分化，轻仓"
    elif adv < 40:
        posture = "控仓 / 不介入"
    else:
        posture = "轻仓观望"
    posture_strong = (trend.get("state") == "主升" and adv >= 55)
    # 大盘走势评述（多指数技术面）——异常时降级为空，不影响大盘主数据
    try:
        comm = market_commentary(force)
    except Exception as e:
        print("[market] commentary err:", e)
        comm = {"indices_detail": {}, "summary": "", "llm_summary": None}
    return {"indices": idx, "breadth": breadth or {}, "trend": trend,
            "posture": posture, "posture_strong": posture_strong, "ts": int(time.time()),
            "note": "" if breadth else "涨跌家数暂不可达",
            "commentary": comm}


def _snapshot_sector_daily(sectors: list[dict]):
    """将当前板块涨跌幅存入日级滚动窗口（每个交易日保留一条最新快照）。
    应在 get_sector_list() 返回后、sector_matrix() 使用前调用。
    """
    from datetime import date
    today = date.today().isoformat()
    for s in sectors:
        bk = s.get("bk", "")
        if not bk:
            continue
        pct = s.get("pct", 0)
        hist = _SECTOR_DAILY_PCTS.get(bk, [])
        # 同一天去重：更新今日快照（盘中多次刷新只留最新的）
        if hist and hist[-1][0] == today:
            hist[-1] = (today, pct)
        else:
            hist.append((today, pct))
        # 截断
        if len(hist) > _SECTOR_DAILY_MAX:
            hist = hist[-_SECTOR_DAILY_MAX:]
        _SECTOR_DAILY_PCTS[bk] = hist


def _sector_trend(bk: str) -> dict:
    """从日级窗口计算板块趋势指标。返回 dict 或空字典（数据不足时）。
    字段：
      'd3'   : 近3日累计涨幅%（正=连涨/负=连跌）
      'd5'   : 近5日累计涨幅%
      'dir'  : 趋势方向 ('持续上行'|'持续下行'|'震荡反弹'|'高位回落')
      'note' : 给 LLM 的自然语言描述（如“近5日累计-12%后今日单日+8.9%，疑似超跌反弹”）
    """
    hist = _SECTOR_DAILY_PCTS.get(bk, [])
    if len(hist) < 2:
        return {}
    today_pct = hist[-1][1]
    result = {}

    # N日累计：取最近N天的pct求和（单日pct已是%单位，累加≈N日总变化）
    for label, n in [("d3", 3), ("d5", 5)]:
        recent = [p for _, p in hist[-n:]]
        result[label] = round(sum(recent), 2)

    # 趋势方向：看最近几日的涨跌符号分布
    signs = [1 if p > 0 else (-1 if p < 0 else 0) for _, p in hist[-5:]]
    up_days = sum(1 for s in signs if s > 0)
    down_days = sum(1 for s in signs if s < 0)
    total = len(signs)

    if up_days >= total - 1 and today_pct > 2:
        result["dir"] = "持续上行"
    elif down_days >= total - 1 and today_pct < -1:
        result["dir"] = "持续下行"
    # 超跌反弹：近N-1日多数下跌（排除今日）+ 今日暴涨 → 模式匹配而非累计值判定
    # （累计值会被单日暴涨拉正，导致 d5<-5 条件漏判）
    elif total >= 3:
        prev_signs = signs[:-1]              # 排除今日，看前几天
        prev_down = sum(1 for s in prev_signs if s < 0)
        if prev_down >= len(prev_signs) - 1 and today_pct > 3:
            result["dir"] = "超跌反弹"
        prev_up = sum(1 for s in prev_signs if s > 0)
        if prev_up >= len(prev_signs) - 1 and today_pct < -2:
            result["dir"] = "高位回落"
    if "dir" not in result:
        result["dir"] = "震荡"

    # 自然语言备注——这是给 prompt 的核心上下文
    d5 = result.get("d5", 0)
    d3 = result.get("d3", 0)
    parts = []
    if abs(d5) >= 3:
        parts.append(f"近5日累计{'+' if d5>=0 else ''}{d5}%")
    if abs(d3) >= 2:
        parts.append(f"近3日{'+' if d3>=0 else ''}{d3}%")
    if result["dir"] == "超跌反弹":
        parts.append(f"注意：{result['dir']}，今日大涨不等于趋势反转")
    elif result["dir"] == "高位回落":
        parts.append(f"注意：{result['dir']}，需警惕资金出逃")
    elif result["dir"] in ("持续上行", "持续下行"):
        parts.append(f"趋势{result['dir']}")
    result["note"] = "；".join(parts) if parts else ""
    return result


def _parse_ranking(df):
    """board_ranking DataFrame -> 板块列表项。空则返 []. """
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    for _, r in df.iterrows():
        pct = float(r["change_pct"]) if not _isnan(r.get("change_pct")) else 0.0
        net = (float(r["main_net_amount"])
               if not _isnan(r.get("main_net_amount")) else 0.0)
        amount = (float(r["amount"])
                  if not _isnan(r.get("amount")) else 0.0)
        out.append({"bk": str(r["code"]), "name": str(r["name"]),
                    "price": None, "pct": pct, "main_net": net, "amount": amount})
    return out


def _em_sector_list(fs_code):
    """东方财富板块列表通用抓取；fs_code 如 'm:90+t:2'(行业) / 'm:90+t:3'(概念)。"""
    out, pn = [], 1
    while True:
        data = em_get(EM_BASE, "/api/qt/clist/get", {
            "pn": pn, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fs": fs_code,
            "fields": "f12,f13,f14,f2,f3,f6,f62,f184"},
            timeout=5, retries=2)
        if not data or not data.get("data") or not data["data"].get("diff"):
            break
        diff = data["data"]["diff"]
        for d in diff:
            out.append({"bk": d.get("f12"), "name": d.get("f14"),
                        "price": d.get("f2"), "pct": d.get("f3"),
                        "main_net": d.get("f62"), "amount": d.get("f6")})
        if len(diff) < 500:
            break
        pn += 1
    return out


def get_sector_list():
    """板块列表（行业板 HY + 概念板 GN）。通达信优先，空/失败/超时都回退东方财富。"""
    out = []
    try:
        if tdx.available():
            hy = _call_timeout(lambda: tdx.board_ranking(board_type=BoardType.HY, top_n=200),
                               15, default=None, label="sectors HY ranking")
            if hy is not None:
                out = _parse_ranking(hy)
                # 概念板并入候选池
                gn = _call_timeout(lambda: tdx.board_ranking(board_type=BoardType.GN, top_n=200),
                                   15, default=None, label="sectors GN ranking")
                if gn is not None:
                    out += _parse_ranking(gn)
    except Exception as e:
        print("[tdx] sectors ranking err:", e)
        out = []

    if out:
        return out

    # 兜底：东方财富（行业板 + 概念板）
    print("[sectors] TDX 无数据，回退东方财富")
    out = _em_sector_list("m:90+t:2")
    out += _em_sector_list("m:90+t:3")
    if out:
        print(f"[sectors] EM 兜底返回 {len(out)} 个板块")
    return out or None


# ---------------------------------------------------------------------------
# 板块质量判定 board_quality（v2 统一：四态 + 相对强度 + 资金流 → label）
# 结论：唤醒 classify_sector(analysis.py) + 板块指数 K线(tdx.kline) + 沪深基准
#        → label{主推/观察/观望}。缓存 10min，避免每次刷新烧请求。
# ---------------------------------------------------------------------------
_QUALITY_CACHE: dict = {}          # bk -> (ts, result)
_HS300_RS: dict = {"ts": 0.0, "ret": None}


def _df_close_list(df):
    """通达信 K线 DataFrame -> [{'close':float}, ...]（classify_sector 只取 close）。"""
    if df is None:
        return None
    cols = list(df.columns) if hasattr(df, "columns") else []
    if not cols:
        return None
    col = "close" if "close" in cols else cols[-1]
    try:
        return [{"close": float(v)} for v in df[col].tolist()]
    except Exception:
        return None


def _hs300_ret() -> float | None:
    """沪深300 近20日涨幅(%)，缓存10min。用作相对强度基准。"""
    now = time.time()
    if now - _HS300_RS["ts"] < 600 and _HS300_RS["ret"] is not None:
        return _HS300_RS["ret"]
    try:
        df = _call_timeout(lambda: tdx.kline("1", "000300", Period.DAILY, 60),
                           15, default=None, label="hs300 kline")
        cl = _df_close_list(df) if df is not None else None
        if cl and len(cl) >= 20 and cl[-20]["close"]:
            ret = (cl[-1]["close"] / cl[-20]["close"] - 1) * 100
            _HS300_RS.update(ts=now, ret=round(ret, 2))
            return _HS300_RS["ret"]
    except Exception:
        pass
    return _HS300_RS["ret"]


def _rel_strength(close_list, hs300_ret):
    """板块近20日涨幅(%) − 沪深300近20日涨幅(%)。close_list: [{'close':x}]。"""
    if not close_list or len(close_list) < 20:
        return None
    try:
        c0, c1 = close_list[-20]["close"], close_list[-1]["close"]
        if not c0:
            return None
        board_ret = (c1 / c0 - 1) * 100
    except Exception:
        return None
    if hs300_ret is None:
        return round(board_ret, 2)
    return round(board_ret - hs300_ret, 2)


def _quality_label(state, inflow_ratio, main_net, amount):
    """label 合成（从「普适」变「特殊」）。"""
    if state in ("主升", "上涨初期"):
        # 有主力注入 + 占成交额比达标 → 主推；否则观察
        if main_net > 0 and inflow_ratio >= 1.0:
            return "主推"
        return "观察"
    if state == "调整":
        return "观察"  # 左/右二合一：调整态可埋伏，不砍
    # 下降 或 未知
    if amount > 0 and main_net / amount < -0.03:
        return "观望"  # 异常大额净流出
    if main_net < -5e7:
        return "观望"
    return "观察"


def board_quality(board, hs300_ret=None):
    """板块质量判定 → {state(四态), relative_strength, inflow_ratio, left_base_alert, analysis, label}。"""
    bk = board.get("bk")
    if not bk:
        return {"state": "未知", "relative_strength": None, "inflow_ratio": 0,
                "left_base_alert": False, "label": "观望"}
    c = _QUALITY_CACHE.get(bk)
    if c and time.time() - c[0] < 600:
        return c[1]
    pct = board.get("pct") or 0
    main_net = board.get("main_net") or 0
    amount = board.get("amount") or 0
    inflow_ratio = (abs(main_net) / amount * 100) if amount > 0 else 0
    state = "未知"
    rs = None
    left_base_alert = False
    try:
        with _TDX_LOCK:
            # ★卡死修复：tdx.kline 在后台线程/新客户端建连时可能挂起，直接调用会永久占住 _TDX_LOCK。
            # 套 _call_timeout + shutdown(wait=False)：超时就释放锁，继续下一个板块，不让整轮矩阵挂死。
            wk = _call_timeout(lambda: _df_close_list(tdx.kline("1", bk, Period.WEEKLY, 60)), 12, default=None, label=f"board_quality weekly {bk}")
            dk = _call_timeout(lambda: _df_close_list(tdx.kline("1", bk, Period.DAILY, 60)), 12, default=None, label=f"board_quality daily {bk}")
        if wk and dk:
            cs = classify_sector(wk, dk)
            state = cs.get("state", "未知")
            rs = _rel_strength(dk, hs300_ret)
            # 左/右二合一：调整态（筑底）高亮「可埋伏」
            # ★v2 B 项：原仅判 state=="调整"，现改为 调整态 + sector_base_pattern 筑底量化
            left_base_alert = (state == "调整") and _sector_base_pattern(dk, wk)
    except Exception as e:
        print("[board_quality] err", bk, e)
    label = _quality_label(state, inflow_ratio, main_net, amount)
    res = {"state": state, "relative_strength": rs,
           "inflow_ratio": round(inflow_ratio, 2),
           "left_base_alert": left_base_alert, "label": label}
    # ★v2.1 §6.8 战略叠加层：施加 avoid/favor/regime（区间外自动退化纯技术）
    try:
        # ★§6.8 软打分：apply_overlay 只调 overlay_score、绝不覆盖 res["label"]/res["state"]；勿在此回潮硬覆盖
        res, regime, note = apply_overlay(res, bk, board.get("name"))
    except Exception as e:
        print("[board_quality] overlay err", bk, e)
        res.setdefault("regime", None)
        res.setdefault("overlay_note", None)
    _QUALITY_CACHE[bk] = (time.time(), res)
    return res


def _quick_quality(b):
    """非 top40 板块的快速 label 兜底（不拉板指 K线，仅靠涨跌+资金方向粗判）。"""
    pct = b.get("pct") or 0
    main_net = b.get("main_net") or 0
    amount = b.get("amount") or 0
    inflow = (abs(main_net) / amount * 100) if amount > 0 else 0
    if pct >= 2 and main_net > 0 and inflow >= 1.0:
        label = "主推"
    elif pct > 0:
        label = "观察"
    else:
        label = "观望"
    res = {"state": "未知", "relative_strength": None, "inflow_ratio": round(inflow, 2),
           "left_base_alert": False, "label": label}
    # ★v2.1 §6.8 战略叠加层对全矩阵生效（仅 bk 查表，零 TDX 开销）
    try:
        # ★§6.8 软打分：apply_overlay 只调 overlay_score、绝不覆盖 label；勿回潮硬覆盖
        res, _, _ = apply_overlay(res, b.get("bk"), b.get("name"))
    except Exception as e:
        print("[_quick_quality] overlay err", b.get("bk"), e)
        res.setdefault("regime", None); res.setdefault("overlay_note", None)
    return res


# ── 左/右二合一：调整态筑底量化（§6.6 阈值待定 → 预设 + 可调口子）──
# 阈值集中在此，后续按真实盘面调参（思路同前端 FLOW_TH：预设但留编辑口子）。
# 逻辑：日线双底(类双底) + 周线箱体(区间震荡且贴近下沿) → 左侧「可埋伏」成立。
BASE_DB_TOL   = 0.05   # 日线双底：两低点最大允许偏差（5%）
BASE_BOX_BAND = 0.15   # 周线箱体：区间振幅上限（≤15% 视为箱体震荡）
BASE_WK_LOOK  = 12     # 周线回看周数
BASE_DK_LOOK  = 40     # 日线回看交易日


def _sector_base_pattern(dk, wk):
    """筑底确认：日线双底(类双底) + 周线箱体(区间震荡且近下沿) → 左侧「可埋伏」成立。
    返回 True 表示调整态板块出现可埋伏基底。阈值均为预设，按盘面调参。"""
    try:
        if not dk or len(dk) < BASE_DK_LOOK:
            return False
        # 周线箱体：回看窗口内振幅收敛 + 当前贴近下沿
        box_ok = False
        if wk and len(wk) >= BASE_WK_LOOK:
            wh = wk[-BASE_WK_LOOK:]
            lo, hi = min(wh), max(wh)
            band = (hi - lo) / (lo + 1e-9)
            box_ok = band <= BASE_BOX_BAND and (hi - wh[-1]) / (hi - lo + 1e-9) >= 0.5
        # 日线双底：最近窗口取两个显著低点，第二低≈第一低且中间有反弹
        rd = dk[-BASE_DK_LOOK:]
        i_last_low = len(rd) - 1 - rd[::-1].index(min(rd))
        first = rd[:i_last_low] if i_last_low > 2 else []
        lo2 = min(first) if first else min(rd)
        lo1 = rd[i_last_low]
        db_ok = abs(lo1 - lo2) / (lo2 + 1e-9) <= BASE_DB_TOL and i_last_low >= 3
        return box_ok and db_ok
    except Exception:
        return False


def _find_sector_leader(bk: str) -> dict | None:
    """从板块成分股中找今日龙头（涨幅最大且成交活跃的个股）。
    返回 {code, market, name, pct} 或 None。
    """
    try:
        if tdx.available():
            with _TDX_LOCK:
                # ★卡死修复：board_members 套线程级超时。否则派生线程里 from_best_host() 建连挂起时，
                # 会永久占用 _TDX_LOCK，导致所有 TDX 调用连锁卡死。超时即放弃、释放锁、继续推进。
                df = _call_timeout(lambda: tdx.board_members(bk, count=80), 30, default=None, label=f"leader_members {bk}")
            if df is None or df.empty:
                return None
            best = None
            best_score = -999
            for _, r in df.iterrows():
                close = float(r["close"]) if not _isnan(r.get("close")) else 0
                pre = float(r["pre_close"]) if not _isnan(r.get("pre_close")) else 0
                pct = round((close - pre) / pre * 100, 2) if pre > 0 else 0
                vol = float(r["vol"]) if not _isnan(r.get("vol")) else 0
                # 龙头评分：涨幅权重60% + 量能活跃度40%（避免无量涨停的虚假龙头）
                score = pct * 0.6 + (min(vol, 1e8) / 1e8) * 40 * (1 if pct > 0 else 0)
                if score > best_score:
                    best_score = score
                    best = {
                        "code": str(r.get("code", "")).strip(),
                        "market": int(r["market"]) if not _isnan(r.get("market")) else 1,
                        "name": str(r.get("name", "")),
                        "pct": round(pct, 2),
                    }
            return best if best and best["pct"] > -6 else None  # 跌停的不算龙头
    except Exception:
        pass
    return None


def sector_matrix(force: bool = False, net_pct: float = None):
    """板块矩阵。通达信 board_ranking 驱动；含龙头识别+增强总结。
    net_pct: 板块 LLM 监控的「资金流动占比门槛(%)」；为 None 时用默认 SECTOR_LLM_MIN_NET_PCT。
    """
    # ★卡死修复：sector_matrix 在后台派生线程跑时，tdx_source 的 from_best_host() 建连可能无限挂起
    # （socket.setdefaulttimeout 对连接探针未必生效）。所有 TDX 密集调用必须套线程级 _call_timeout，
    # 单次卡死就放弃该板块、继续推进，不让整轮板块矩阵挂死。
    lst = _call_timeout(get_sector_list, 60, default=None, label="get_sector_list")
    if not lst:
        return None
    # 日级快照：存入滚动窗口（供 LLM 趋势判断用，不影响本次输出）
    _snapshot_sector_daily(lst)
    hs300_ret = _call_timeout(_hs300_ret, 20, default=None, label="hs300_ret")        # 相对强度基准（沪深300近20日涨幅），缓存10min
    llm_on = public_config().get("enabled")

    # 基础字段
    for b in lst:
        pct = b.get("pct") or 0
        b["weekly_up"] = pct >= 2.0
        b["state"] = ("强势" if pct >= 2 else "上涨" if pct > 0
                      else "调整" if pct >= -2 else "弱势")
    # 按涨幅排序后，对前40名查龙头（节省请求）
    lst.sort(key=lambda x: -(x.get("pct") or 0))
    top40 = lst[:40]

    # 板块 LLM 监控门槛：板块 |主力净流入| 占「自身成交额」的比例(%)。
    # 例：小盘(成交30亿)净流入5亿→占比16.7%，敏感度远高于 大盘(成交200亿)净流入10亿→5%。
    # 大盘冷热自适应——成交额随行情缩放、门槛绝对值自动校准；比固定市值更贴合资金参与度。
    thr = net_pct if net_pct is not None else SECTOR_LLM_MIN_NET_PCT

    # ★v2.1 重调：重层(board_quality 拉K线算四态+RS) 先跑——抢占 TDX 行情预算（单窗口约80次安全），
    #   确保 label 判定拿到准四态；轻层(龙头) 后跑，失败仅影响展示不影响 label。
    heavy_bks = {b.get("bk") for b in top40}

    def _enrich_heavy(b):
        if b.get("bk") not in heavy_bks:
            return
        try:
            q = board_quality(b, hs300_ret)
            b["quality"] = q
            b["state"] = q["label"]
        except Exception as e:
            print("[enrich] heavy quality err", b.get("bk"), e)

    # ★v2.1 / easy_tdx 线程不安全：MacClient 连接线程亲和，派生线程碰它必失败（静默返回 None）。
    #   故重层串行跑在请求线程内（不派生线程），确保 TDX 只被单一线程接触 → 四态/RS 稳定算全。
    for b in top40:
        _call_timeout(lambda: _enrich_heavy(b), 45, default=None, label=f"board_quality {b.get('bk')}")

    # 轻层：龙头 + 趋势 + 快速 label 兜底（仅当重层未覆盖，保护重层四态结果）
    def _enrich_light(b):
        bk_code = b.get("bk", "")
        if bk_code:
            leader = _find_sector_leader(bk_code)
            b["leader"] = leader
        if "quality" not in b:
            q = _quick_quality(b)
            b["quality"] = q
            b["state"] = q["label"]       # ★v2 角标改用 label，非四态
        # ★板块层零 LLM：原"达标才调大模型总结"已移除——战略判断统一由 §6.8 叠加层(冻结JSON)承担，
        # 板块层只做机械筛选与免费规则模板展示，不再内嵌 LLM 异动分析。
        smry = sector_summary(b, llm=False)
        b["summary"] = smry["text"]
        b["summary_mode"] = "rule"

    # 轻层同样串行（leader/summary 非 TDX 密集，但统一串行避免跨线程碰 MacClient）
    for b in top40:
        _call_timeout(lambda: _enrich_light(b), 45, default=None, label=f"enrich_light {b.get('bk')}")

    # 剩余板块用轻量总结（无龙头）+ 快速 label 兜底（不拉板指 K线）
    for b in lst[40:]:
        smry = sector_summary(b, llm=False)
        b["summary"] = smry["text"]
        b["summary_mode"] = "rule"
        b["leader"] = None
        if "quality" not in b:
            b["quality"] = _quick_quality(b)
            b["state"] = b["quality"]["label"]

    # ★v2.1 排序：标签优先级（主推>观察>观望）；同档内按相对强度降序→涨幅降序，
    #   落实 §6.3③ 相对强度作为 soft-priority（"先上谁"的依据，避免摊大饼）。
    #   相对强度缺省按 0 计（仅粗判板块），不影响主排序。
    _LABEL_RANK = {"主推": 0, "观察": 1, "观望": 2}

    def _sort_key(x):
        rs = (x.get("quality") or {}).get("relative_strength")
        return (_LABEL_RANK.get(x.get("state"), 3),
                -(rs if rs is not None else 0),
                -(x.get("pct") or 0))

    lst.sort(key=_sort_key)

    # ★v2.1 修正：标签重排会把「仅粗判(_quick_quality)」的板块顶进展示用 top40，
    #   这些板块缺 relative_strength/四态（重层未覆盖）→ soft-priority 失效。
    #   对最终展示用 top40 内仍缺 RS 的做重层 board_quality 回填，保证前台 top40 全部带 RS。
    display = lst[:40]
    for b in display:
        if (b.get("quality") or {}).get("relative_strength") is None:
            try:
                q = _call_timeout(lambda: board_quality(b, hs300_ret), 45, default=None, label=f"backfill {b.get('bk')}")
                if q:
                    b["quality"] = q
                    b["state"] = q["label"]
            except Exception as e:
                print("[enrich] backfill quality err", b.get("bk"), e)
    lst[:40] = sorted(display, key=_sort_key)

    _flush_llm_cache(_MARKET_LLM_CACHE)
    return lst


# ---------------------------------------------------------------------------
# 板块轻量接口（秒级返回，不拉板指 K线；首屏 24 + 折叠 + 搜索联想 + 关注置顶）
# ---------------------------------------------------------------------------
# ★2026-08-06 非阻塞：板块列表 TDX 拉取很慢(~22s)，改为「有缓存立即返回 + 后台异步刷新」，
# 避免首屏 /api/sectors_lite 阻塞 22s 让用户以为崩了；启动预热让首个请求即命中热缓存。
_SECTOR_LIST_CACHE = {"ts": 0.0, "data": None, "refreshing": False}
_WATCHED_FILE = paths.data_path("watched_boards.json")
_LABEL_RANK = {"主推": 0, "观察": 1, "观望": 2}


def _bg_refresh_sectors():
    """后台线程刷新板块缓存（带 in-flight 锁，避免并发重复烧 TDX）。"""
    if _SECTOR_LIST_CACHE.get("refreshing"):
        return
    _SECTOR_LIST_CACHE["refreshing"] = True
    def _run():
        try:
            d = get_sector_list()
            if d:
                _SECTOR_LIST_CACHE.update(ts=time.time(), data=d)
        except Exception as e:
            print("[sectors] bg refresh err:", e)
        finally:
            _SECTOR_LIST_CACHE["refreshing"] = False
    threading.Thread(target=_run, daemon=True).start()


def get_sector_list_cached(ttl=60, max_staleness=1800):
    """带短缓存的板块基础列表，避免首屏 lite + 精算两次重复烧 TDX。
    非阻塞策略：热缓存(<ttl)直接返回；有过期但尚可用的缓存(<max_staleness)立即返回旧值并触发后台刷新；
    仅首次(无缓存)或缓存过旧(>max_staleness)才同步阻塞一次。启动已预热，故正常首屏不阻塞。
    """
    now = time.time()
    cached = _SECTOR_LIST_CACHE["data"]
    if cached is not None and now - _SECTOR_LIST_CACHE["ts"] < ttl:
        return cached
    if cached is not None and now - _SECTOR_LIST_CACHE["ts"] < max_staleness:
        _bg_refresh_sectors()          # 旧数据先顶上，后台慢慢刷新
        return cached
    d = get_sector_list()              # 首次或过期过久：同步取一次
    if d:
        _SECTOR_LIST_CACHE.update(ts=now, data=d)
    return _SECTOR_LIST_CACHE["data"]


def load_watched():
    try:
        if os.path.exists(_WATCHED_FILE):
            with open(_WATCHED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data]
    except Exception:
        pass
    return []


def save_watched(lst):
    try:
        paths.ensure_data_dir()
        with open(_WATCHED_FILE, "w", encoding="utf-8") as f:
            json.dump([str(x) for x in lst], f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("[watched] save err", e)
        return False


def sector_lite():
    """轻量板块列表：board_ranking 基础字段 + 快速 label（含战略软打分），不拉 K线 → 秒级。
    排序：关注板块置顶（保持保存顺序）→ 标签(主推>观察>观望) → 资金净流入占比 → 涨幅。
    """
    lst = get_sector_list_cached()
    if not lst:
        return None, None
    watched = load_watched()
    wset = set(watched)
    out = []
    for b in lst:
        pct = b.get("pct") or 0
        main_net = b.get("main_net") or 0
        amount = b.get("amount") or 0
        inflow_ratio = (abs(main_net) / amount * 100) if amount > 0 else 0.0
        q = _quick_quality(b)
        label = q.get("label", "观望")
        note = q.get("overlay_note")
        if isinstance(note, str) and note.startswith("战略回避"):
            overlay_tag = "avoid"
        elif isinstance(note, str) and note.startswith("战略推荐"):
            overlay_tag = "favor"
        else:
            overlay_tag = None
        out.append({
            "bk": b.get("bk"), "name": b.get("name"),
            "pct": pct, "main_net": main_net, "amount": amount,
            "inflow_ratio": round(inflow_ratio, 2),
            "label": label, "state": label,
            "overlay_tag": overlay_tag,
            "watched": b.get("bk") in wset,
            "summary": None, "leader": None, "quality": None,
        })
    # 关注置顶（保持保存顺序），其余按 标签 → 资金净流入占比 → 涨幅
    wmap = {bk: i for i, bk in enumerate(watched)}
    out.sort(key=lambda x: (
        0 if x["watched"] else 1,
        wmap.get(x["bk"], 1 << 30) if x["watched"] else 0,
        _LABEL_RANK.get(x["label"], 3),
        -x["inflow_ratio"],
        -x["pct"],
    ))
    try:
        from overlay import load_overlay
        ov = load_overlay()
        regime = (ov.get("meta") or {}).get("regime") if ov else None
    except Exception:
        regime = None
    return out, regime


@app.route("/api/sectors_lite")
def api_sectors_lite():
    try:
        lst, regime = sector_lite()
        if lst is None:
            return jsonify({"sectors": [], "note": "板块数据暂不可达，恢复后自动加载"})
        return jsonify({"sectors": lst, "regime": regime, "ts": int(time.time())})
    except Exception as e:  # noqa
        return jsonify({"sectors": [], "note": "板块数据源暂不可达：" + str(e)})


@app.route("/api/sector_enrich", methods=["POST"])
def api_sector_enrich():
    try:
        body = request.get_json(force=True) or {}
        bks = (body.get("bks") or [])[:60]
        if not bks:
            return jsonify({"data": {}, "ts": int(time.time())})
        base = {b.get("bk"): b for b in (get_sector_list_cached() or [])}
        hs300_ret = _hs300_ret()
        result = {}
        for bk in bks:
            b = base.get(bk)
            if not b:
                continue
            try:
                q = board_quality(b, hs300_ret)
                b["quality"] = q
                b["state"] = q.get("label")
                # 注：leader 不在卡片上展示，省去 board_members 的 TDX 调用以加速首屏回填
                smry = sector_summary(b, llm=False)
                smry_text = smry.get("text") if isinstance(smry, dict) else str(smry)
                result[bk] = {
                    "state": q.get("label"),
                    "label": q.get("label"),
                    "quality": {k: q.get(k) for k in (
                        "state", "relative_strength", "inflow_ratio",
                        "left_base_alert", "overlay_note", "overlay_score")},
                    "leader": None,
                    "summary": smry_text,
                }
            except Exception as e:
                print("[enrich] err", bk, e)
        return jsonify({"data": result, "ts": int(time.time())})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/watched_boards", methods=["GET", "POST"])
def api_watched_boards():
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        lst = body.get("bks") or []
        save_watched(lst)
        return jsonify({"ok": True, "bks": load_watched()})
    return jsonify({"bks": load_watched()})


# ---------------------------------------------------------------------------
# 板块系统评述（LLM 一句话总结，按时段缓存）
# ---------------------------------------------------------------------------
_COMMENTARY_CACHE_FILE = paths.data_path("_sec_commentary_cache.json")
_SEC_COMMENTARY_CACHE: dict = {"text": None, "ts": 0.0, "slot": None, "bk_hash": None}
_COMMENTARY_BG_JOBS: set = set()          # 当前正在后台生成的 bk_hash，避免重复触发
_COMMENTARY_BG_LOCK = threading.Lock()    # 保护 _COMMENTARY_BG_JOBS

# 定时槽：早10:00 / 午后13:30（用户指定，抓早盘开盘和午后开始两拨动向）
_COMMENTARY_SLOTS = [(10, 0), (13, 30)]


def _commentary_slot_key(now=None):
    """返回当前已到达的最近定时槽键；未到首个槽返回 None。"""
    now = now or datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    key = None
    for (h, m) in _COMMENTARY_SLOTS:
        s = h * 60 + m
        if cur >= s:
            key = f"{now.year:04d}-{now.month:02d}-{now.day:02d}_{h:02d}:{m:02d}"
        else:
            break
    return key


def _load_commentary_cache():
    """启动时从磁盘恢复板块评述缓存。"""
    global _SEC_COMMENTARY_CACHE
    try:
        with open(_COMMENTARY_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _SEC_COMMENTARY_CACHE = {
            "text": data.get("text"),
            "ts": data.get("ts", 0.0),
            "slot": data.get("slot"),
            "bk_hash": data.get("bk_hash"),
            "briefs": data.get("briefs") or {},
        }
    except (FileNotFoundError, ValueError, OSError):
        pass


def _save_commentary_cache():
    """缓存更新后落盘（原子写）。"""
    tmp = _COMMENTARY_CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SEC_COMMENTARY_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _COMMENTARY_CACHE_FILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# 启动恢复
_load_commentary_cache()


def _rule_sector_commentary(sectors):
    """LLM 不可用时的规则兜底：聚合首屏板块的 sector_summary 红框内容，
    拼一句总评（谁强谁弱 + 资金面 + 最强板块的一句话分析）。保证蓝框永远有内容。
    注意：只用前端传入的 sectors 字段，不回查 get_sector_list（盘后 TDX/EM 慢会拖垮接口）。"""
    ups = inflow = 0
    lead = None
    top = []   # (pct, name, brief)
    for s in sectors[:48]:
        pct = s.get("pct") or 0
        net = s.get("main_net") or 0
        if pct > 0:
            ups += 1
        if net > 0:
            inflow += 1
        if lead is None or pct > lead.get("pct", 0):
            lead = s
        base = s
        brief = ""
        try:
            sm = sector_summary(base, llm=False).get("text", "")
            if "【分析】" in sm:
                brief = sm.split("【分析】", 1)[1].split("【", 1)[0].strip()
        except Exception:
            brief = ""
        top.append((pct, s.get("name", ""), brief))
    top.sort(key=lambda x: x[0], reverse=True)
    head = (f"首屏{len(sectors)}板块中{ups}涨、{inflow}个主力净流入；"
            f"领涨{lead.get('name', '') if lead else ''}"
            f"({(lead.get('pct') or 0):+.2f}%)")
    extras = "；".join(f"{n}：{b[:38]}" for _, n, b in top[:3] if b)
    txt = head + ("。" + extras if extras else "。")
    return txt[:140]


@app.route("/api/sector_commentary", methods=["POST"])
def api_sector_commentary():
    """接收当前显示的板块列表，返回 LLM 一句话系统评述（按时段缓存）。"""
    body = request.get_json(force=True) or {}
    sectors = body.get("sectors") or []
    if not sectors:
        return jsonify({"text": None, "slot": None, "cached": False})

    now = datetime.datetime.now()
    cur_slot = _commentary_slot_key(now)
    cache = _SEC_COMMENTARY_CACHE

    # 切换定时槽时重置累计缓存
    if cur_slot and cache.get("slot") != cur_slot:
        cache.update({"text": None, "ts": 0.0, "slot": cur_slot, "briefs": {}})

    # 未到评述时段：沿用上一档总览，briefs 置空
    if cur_slot is None:
        if cache.get("text"):
            return jsonify({
                "text": cache["text"],
                "slot": cache.get("slot"),
                "briefs": {},
                "cached": True,
                "stale": True,
                "note": "上一档缓存·非实时",
            })
        return jsonify({"text": None, "slot": None, "cached": False,
                        "note": "尚未到达评述生成时间（10:00 / 13:30）"})

    # ★B（2026-08-05）：每板块一句 LLM 简评，按「时段」累计缓存。
    # minimax-m3 对单条长提示词生成很慢（2 板块可达 25s），因此：
    # 1. 首次请求立即返回已缓存的真实简评 + 规则兜底；
    # 2. 对缺失的板块触发后台 LLM 生成，结果合并到缓存；
    # 3. 前端轮询，缓存命中越来越多，模板逐渐被真实简评替换。
    _all = sectors[:48]
    cached_briefs = cache.get("briefs") or {}
    text = cache.get("text")
    if not text:
        text = _rule_sector_commentary(sectors)

    # 即时返回：已缓存的用 LLM，缺失的用规则兜底
    briefs = {}
    missing = []
    base_secs = get_sector_list_cached() or []
    base_map = {b.get("bk"): b for b in base_secs}
    for s in _all:
        bk = s.get("bk", "")
        if bk in cached_briefs:
            briefs[bk] = cached_briefs[bk]
        else:
            missing.append(s)
            base = base_map.get(bk) or s
            try:
                smry = sector_summary(base, llm=False).get("text", "")
            except Exception:
                smry = ""
            am = (smry.split("【分析】", 1)[1].split("【", 1)[0].strip().replace("\n", " ")
                  if "【分析】" in smry else (smry.split("\n")[0] if smry else ""))
            briefs[bk] = am[:60] if am else ""

    # 触发后台生成缺失板块（按当前时段合并到缓存）
    background = bool(missing)
    if missing:
        with _COMMENTARY_BG_LOCK:
            if cur_slot not in _COMMENTARY_BG_JOBS:
                _COMMENTARY_BG_JOBS.add(cur_slot)
                threading.Thread(target=_bg_generate_commentary,
                                 args=(_all, cur_slot),
                                 daemon=True).start()

    return jsonify({
        "text": text,
        "briefs": briefs,
        "slot": cur_slot,
        "cached": False,
        "background": background,
        "note": "LLM 简评后台生成中，已缓存板块直接显示" if background else "",
    })


def _generate_commentary_briefs(sectors, cur_slot):
    """调用 LLM 为 sectors 生成单板块简评；返回 (briefs, is_llm)。"""
    sys_text = "A股板块分析师"
    CHUNK = 1
    chunks = [sectors[i:i + CHUNK] for i in range(0, len(sectors), CHUNK)] or [sectors]

    def _gen_chunk(chunk):
        ctx = "\n".join(f"{i}.{s.get('name','')}|{s.get('state') or s.get('label') or ''}|{s.get('pct') or 0:+.2f}%"
                        for i, s in enumerate(chunk, 1))
        ut = f"对以下A股板块各给一句25字内点评，格式：`编号. 点评`\n\n{ctx}\n"
        raw = call_llm(sys_text, ut, max_tokens=180)
        out = {}
        if raw:
            for line in raw.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d+)\.\s*(.+)$", line)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(chunk):
                        out[chunk[idx].get("bk", "")] = m.group(2).strip()
        return out

    briefs = {}
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as ex:
            futs = [ex.submit(_gen_chunk, c) for c in chunks]
            for f in futs:
                try:
                    briefs.update(f.result())
                except Exception:
                    pass
    except Exception:
        pass
    return briefs, bool(briefs)


def _generate_commentary_overview(sectors, cur_slot):
    """调用 LLM 生成整体格局总览；返回 (text, is_llm)。"""
    sys_text = "A股板块分析师"
    names = "、".join(f"{s.get('name','')}({s.get('pct') or 0:+.2f}%)" for s in sectors[:12])
    ut = f"以下A股板块：{names}。用一行`总览：`开头，≤50字给出整体格局一句话。只输出总览行。"
    ov = call_llm(sys_text, ut, max_tokens=120)
    if ov:
        line = ov.strip().split("\n")[0].strip()
        if line.startswith("总览："):
            line = line[3:].strip()
        elif line.startswith("总览:"):
            line = line.split(":", 1)[1].strip()
        if line:
            return line[:60], True
    return None, False


def _bg_generate_commentary(sectors, cur_slot):
    """后台生成评述；对当前时段缓存做增量合并，避免板块集合变动导致缓存失效。"""
    cache = _SEC_COMMENTARY_CACHE
    try:
        # 1) 只生成当前缓存中还没有的板块
        cached_briefs = cache.get("briefs") or {}
        missing = [s for s in sectors if s.get("bk") not in cached_briefs]
        if missing:
            briefs, is_llm = _generate_commentary_briefs(missing, cur_slot)
            if is_llm:
                cached_briefs.update(briefs)
                # 简评已出，立即落盘（总览可后补，先让卡片看到真实简评）
                cache.update({
                    "text": cache.get("text") or _rule_sector_commentary(sectors),
                    "ts": time.time(),
                    "slot": cur_slot,
                    "briefs": cached_briefs,
                })
                _save_commentary_cache()

        # 2) 总览：若缓存没有或不是 LLM 生成，尝试补一个
        text = cache.get("text")
        ov_is_llm = False
        if not text or text.startswith("首屏") or text.startswith("模拟总览"):
            text, ov_is_llm = _generate_commentary_overview(sectors, cur_slot)
            if ov_is_llm:
                cache.update({
                    "text": text,
                    "ts": time.time(),
                    "slot": cur_slot,
                    "briefs": cached_briefs,
                })
                _save_commentary_cache()
    except Exception as e:
        print(f"[commentary bg] exception: {e}")
    finally:
        with _COMMENTARY_BG_LOCK:
            _COMMENTARY_BG_JOBS.discard(cur_slot)


@app.route("/api/commentary_status", methods=["GET"])
def api_commentary_status():
    """调试用：返回当前评述缓存 hash 与后台正在生成的 hash集合。"""
    with _COMMENTARY_BG_LOCK:
        jobs = list(_COMMENTARY_BG_JOBS)
    return jsonify({
        "cache_hash": _SEC_COMMENTARY_CACHE.get("bk_hash"),
        "cache_slot": _SEC_COMMENTARY_CACHE.get("slot"),
        "cache_briefs_count": len(_SEC_COMMENTARY_CACHE.get("briefs") or {}),
        "background_jobs": jobs,
    })


# ---------------------------------------------------------------------------
# 持仓股标记（本地文件 holdings.json，不走账号；极简 {code,market,name,ts}）
# ---------------------------------------------------------------------------
HOLDINGS_FILE = paths.data_path("holdings.json")


def load_holdings():
    try:
        if os.path.exists(HOLDINGS_FILE):
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def save_holdings(lst):
    try:
        with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[holdings] save error:", e)


def holdings_keys():
    return set((str(h.get("code", "")), int(h.get("market", 1))) for h in load_holdings())


def scan_sector(bk: str):
    """板块成分股扫描。通达信 board_members 优先，失败回退东方财富。"""
    stocks = []
    try:
        if tdx.available():
            df = _call_timeout(lambda: tdx.board_members(bk, count=300), 30, default=None, label=f"scan_sector members {bk}")
            for _, r in df.iterrows():
                code = str(r.get("code", "")).strip()
                if not code:
                    continue
                mkt = int(r["market"]) if not _isnan(r.get("market")) else 1
                name = str(r.get("name", ""))
                close = float(r["close"]) if not _isnan(r.get("close")) else None
                pre = float(r["pre_close"]) if not _isnan(r.get("pre_close")) else None
                pct = round((close - pre) / pre * 100, 2) if (close and pre) else None
                stocks.append({"code": code, "market": mkt, "name": name,
                               "price": close, "pct": pct, "turnover": 0.0})
    except Exception as e:
        print("[tdx] scan members fallback -> em:", e)
        stocks = []
    if not stocks:
        # 兜底：东方财富成分股
        stocks, pn = [], 1
        while True:
            data = em_get(EM_BASE, "/api/qt/clist/get", {
                "pn": pn, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fs": f"b:{bk}",
                "fields": "f12,f13,f14,f2,f3,f10,f8,f6,f62"})
            if not data or not data.get("data") or not data["data"].get("diff"):
                break
            diff = data["data"]["diff"]
            for d in diff:
                stocks.append({"code": d.get("f12"), "market": d.get("f13"),
                               "name": d.get("f14"), "price": d.get("f2"),
                               "pct": d.get("f3"), "turnover": (d.get("f8") or 0)})
            if len(diff) < 500:
                break
            pn += 1
    if not stocks:
        return {"bk": bk, "board_label": "观望", "stocks": [],
                "note": "板块成分股暂不可达，恢复后自动可用", "ts": int(time.time())}

    # pct 取不到（tdx 成员未带涨跌幅）时不过滤，交由 K线评估
    cand = [s for s in stocks
            if s.get("pct") is None
            or (isinstance(s["pct"], (int, float)) and s["pct"] > -6)]

    def eval_one(s):
        secid = f"{s['market']}.{s['code']}"
        dkl = _klines(secid, 101, 120)
        wkl = _klines(secid, 102, 60)
        if not dkl or len(dkl) < 20:
            return None
        ev = evaluate_stock(dkl, wkl, code=s["code"], name=s["name"])
        ev.market = s["market"] if isinstance(s["market"], int) else 1
        if isinstance(s["price"], (int, float)):
            ev.close = s["price"]
        if isinstance(s["pct"], (int, float)):
            ev.pct = s["pct"]
        return ev

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(eval_one, cand))
    results = [r for r in results if r]
    order = {"触发": 0, "观察": 1, "无": 2}
    hset = holdings_keys()
    results.sort(key=lambda e: (order.get(e.level, 3), -e.score))
    # ★v2 接 board_quality：附所属板块 label（主推/观察/观望），前端可作板块级标记；
    #   单板块扫描时 label 为常数，故个股排序仍按自身 level/score（不强行按常数 label 重排）
    board_info = {"bk": bk}
    try:
        for s in (get_sector_list() or []):
            if s.get("bk") == bk:
                board_info = s
                break
    except Exception:
        pass
    try:
        bq = board_quality(board_info, _hs300_ret()) if tdx.available() else None
        board_label = bq["label"] if bq else "观望"
    except Exception:
        board_label = "观望"
    return {"bk": bk, "board_label": board_label,
            "stocks": [eval_to_dict(e, holding=(str(e.code), int(e.market)) in hset) for e in results],
            "ts": int(time.time())}


# —— 单票 LLM 异步缓存（先骨架后补，避免详情页卡「加载中」）——
_STOCK_LLM_CACHE = {}        # (code, market) -> {news_detail, news_ai, summary_text, summary_mode, done, ts}
_STOCK_LLM_LOCK = threading.Lock()
_STOCK_LLM_RUNNING = set()

def _bg_stock_llm(code, market, name, ev_dict, kctx):
    key = (str(code), int(market))
    try:
        risk_llm = compute_risk(code, market, name, llm=True)
        summ_llm = stock_summary(ev_dict, risk_llm, llm=True, kctx=kctx)
        news_detail = None
        news_ai = False
        for c in (risk_llm.get("checks") or []):
            if c.get("key") == "news":
                news_detail = c.get("detail")
                news_ai = c.get("ai_summary")
                break
        with _STOCK_LLM_LOCK:
            _STOCK_LLM_CACHE[key] = {
                "news_detail": news_detail, "news_ai": news_ai,
                "summary_text": summ_llm.get("text"), "summary_mode": summ_llm.get("mode"),
                "done": True, "ts": time.time(),
            }
    except Exception as e:
        print(f"[stock_llm] bg error {code}.{market}: {e}", flush=True)
    finally:
        with _STOCK_LLM_LOCK:
            _STOCK_LLM_RUNNING.discard(key)

def _trigger_stock_llm(code, market, name, ev_dict, kctx):
    key = (str(code), int(market))
    with _STOCK_LLM_LOCK:
        if key in _STOCK_LLM_RUNNING:
            return
        if _STOCK_LLM_CACHE.get(key, {}).get("done"):
            return
        _STOCK_LLM_RUNNING.add(key)
    t = threading.Thread(target=_bg_stock_llm, args=(code, market, name, ev_dict, kctx), daemon=True)
    t.start()

def stock_detail(code: str, market: int, name: str = ""):
    secid = f"{market}.{code}"
    # ★2026-08-06：直接打开 /preview?code=xxx 时前端拿不到 name，这里服务端回填，
    # 否则详情页标题渲染成「undefined undefined」。
    if not (name or "").strip():
        name = _resolve_name(secid) or ""
    _t0 = time.time()
    dkl, wkl, m60, m15 = _fetch_all_klines(secid)
    print(f"[stock_detail] klines {(time.time()-_t0):.2f}s dkl={len(dkl) if dkl else 0} tdx={tdx.available()}", flush=True)
    if not dkl:
        return {"error": "无K线数据"}
    hset = holdings_keys()
    ev = evaluate_stock(dkl, wkl, m60 if m60 else None,
                        m15 if m15 else None, code=code, name=name)
    ev.market = market if isinstance(market, int) else 1
    closes = [x["close"] for x in dkl]
    ma5 = ma_series(closes, 5)
    ma10 = ma_series(closes, 10)
    ma20 = ma_series(closes, 20)
    ma40 = ma_series(closes, 40)
    dif, dea, hist = macd(closes)
    kdata = []
    for i in range(len(dkl)):
        kdata.append({
            "date": dkl[i]["date"], "o": dkl[i]["open"], "c": dkl[i]["close"],
            "h": dkl[i]["high"], "l": dkl[i]["low"], "v": dkl[i]["vol"],
            "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i], "ma40": ma40[i],
            "dif": dif[i], "dea": dea[i], "hist": hist[i]})
    # —— AlphaLoop 增强：板块权限 + 风险独立复检 + 一句话总结 ——
    board = classify_board(code)
    risk = compute_risk(code, market, name, llm=False)
    print(f"[stock_detail] risk(rule) {(time.time()-_t0):.2f}s", flush=True)
    kctx = kline_context(dkl)
    ev_dict = eval_to_dict(ev, holding=(str(code), int(market)) in hset)
    summ = stock_summary(ev_dict, risk, llm=False, kctx=kctx)
    # 触发后台 LLM 生成（新闻复检 LLM 版 + 一句话总结 LLM 版），前端轮询 /api/stock_fill 补填
    _trigger_stock_llm(code, market, name, ev_dict, kctx)
    # 序贯状态机：①②③④⑤ 状态 + ④ 触发时间（用于个股详情弹窗信号明细）
    seq_info = None
    try:
        seq_info = picks.scan_stock(secid, name)
        if seq_info:
            seq_info = {
                "c1": seq_info.get("c1"),
                "c2": seq_info.get("c2"),
                "c3": seq_info.get("c3"),
                "c3a": seq_info.get("c3a"),
                "c3b": seq_info.get("c3b"),
                "c4": seq_info.get("c4"),
                "c5": seq_info.get("c5"),
                "trend_type": seq_info.get("trend_type"),   # ★2026-08-05 双路径：main/early/none
                "stage": seq_info.get("stage"),
                "wk_rsi": seq_info.get("wk_rsi"),
                "d_rsi": seq_info.get("d_rsi"),
                "first_golden_date": seq_info.get("first_golden_date"),  # ★初期票第一个金叉买点
                "first_golden_close": seq_info.get("first_golden_close"),
                "c4_date": seq_info.get("c4_date"),
                "last_c4_date": seq_info.get("last_c4_date"),
                "fired_date": seq_info.get("fired_date"),
                "days_left": seq_info.get("days_left"),
                "days_waited": seq_info.get("days_waited"),
                "entry_ok": seq_info.get("entry_ok"),          # ★入场过滤：⑤信号日 close≤MA5
                "entry_ma5_ratio": seq_info.get("entry_ma5_ratio"),  # 信号日 close/MA5-1(%)
            }
    except Exception as e:
        print(f"[stock_detail] scan_stock err {secid}: {e}", flush=True)
    print(f"[stock_detail] skeleton done {(time.time()-_t0):.2f}s", flush=True)
    return {"eval": ev_dict,
            "holding": (str(code), int(market)) in hset,
            "kdata": kdata, "name": name, "code": code,
            "board": board, "risk": risk, "summary": summ, "kctx": kctx,
            "llm_pending": True, "seq": seq_info}


def _resolve_secid(q):
    """代码/名称/拼音 → secid(market.code)。6位代码按市场前缀推断；名称先查 picks 缓存，再走东方财富联想。"""
    q = (q or "").strip()
    if re.fullmatch(r"\d{6}", q):
        m = "1" if q[0] in "689" else "0"   # 沪市 6/688/8/9；深市 0/3
        return f"{m}.{q}"
    if re.fullmatch(r"[01]\.\d{6}", q):
        return q
    pc = picks.get_cache()
    if pc:
        for grp in pc.values():
            if isinstance(grp, list):
                for r in grp:
                    if r.get("name") and q in r.get("name"):
                        return r.get("secid")
    hit = _search_stock(q)
    if hit:
        return hit[0]
    return None


def _search_stock(q):
    """东方财富联想接口：名称/拼音/代码 → (secid, name)，带短缓存。"""
    key = re.sub(r"\s+", "", str(q or "")).strip()
    if not key:
        return None
    cache_key = "stock_suggest:" + key.lower()
    cached = cache_get(cache_key, 3600)
    if cached:
        return tuple(cached)
    try:
        data = em_get("https://searchapi.eastmoney.com", "/api/suggest/get", {
            "input": key,
            "type": 14,
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": 8,
        }, timeout=5, retries=1)
        table = (data or {}).get("QuotationCodeTable") or {}
        for row in table.get("Data") or []:
            if row.get("Classify") != "AStock":
                continue
            if str(row.get("MktNum")) not in ("0", "1"):
                continue
            secid = str(row.get("QuoteID") or "").strip()
            if re.fullmatch(r"[01]\.\d{6}", secid):
                name = str(row.get("Name") or "").strip()
                out = (secid, name)
                cache_set(cache_key, out)
                return out
    except Exception as e:
        print("[stock_suggest] err:", e)
    return None


def _resolve_name(secid, q=None):
    """secid → 股票名称。三级兜底：picks 缓存 → 通达信 quotes → 腾讯 gtimg。

    ★2026-08-06：原实现只查 picks 缓存，缓存里没有的票（如直接在地址栏打开
    /preview?code=600919）拿不到名称，详情页标题渲染成 `undefined undefined`。
    """
    pc = picks.get_cache()
    if pc:
        for grp in pc.values():
            if isinstance(grp, list):
                for r in grp:
                    if r.get("secid") == secid and r.get("name"):
                        return r.get("name")
    mk, _, code = str(secid or "").partition(".")
    if not code:
        return None
    # 2) 通达信实时报价带 name
    try:
        if tdx.available():
            df = tdx.quotes([(mk, code)])
            if df is not None and len(df):
                nm = str(df.iloc[0].get("name") or "").strip()
                if nm:
                    return nm
    except Exception:
        pass
    # 3) 腾讯 gtimg（f[1] 为名称），TDX 熔断时兜底
    try:
        sym = {"1": "sh", "0": "sz", "2": "bj"}.get(mk, "sh") + code
        r = requests.get("https://qt.gtimg.cn/q=" + sym,
                         headers={"User-Agent": "Mozilla/5.0"},
                         timeout=8, proxies=NO_PROXY)
        for seg in r.text.replace("\n", ";").split(";"):
            if "v_" + sym + "=" in seg:
                f = seg.split("=", 1)[1].strip().strip('"').split("~")
                if len(f) > 1 and f[1].strip():
                    return f[1].strip()
    except Exception:
        pass
    return None


@app.route("/api/stock_seq", methods=["POST"])
def api_stock_seq():
    """实时计算单票五条件序贯状态（不在 picks 缓存也行）。q=代码或名称。"""
    body = request.get_json(force=True) or {}
    q = (body.get("q") or "").strip()
    if not q:
        return jsonify({"error": "空查询"}), 400
    secid = _resolve_secid(q)
    if not secid:
        return jsonify({"error": "无法识别，请输入6位代码、个股名称或拼音"}), 404
    try:
        r = picks.scan_stock(secid, _resolve_name(secid, q))
    except Exception as e:
        return jsonify({"error": f"扫描失败：{e}"}), 500
    if not r:
        return jsonify({"error": "无K线数据或样本不足"}), 404
    return jsonify({"row": r})


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/game")
@app.route("/game/")
def game_index():
    return send_from_directory(app.static_folder, "game/index.html")


@app.route("/preview")
def preview():
    resp = send_from_directory(app.static_folder, "preview.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/shot/<path:filename>")
def shot(filename):
    # 专用截图路由：绝对路径直发，规避静态路由异常
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_shots")
    return send_from_directory(d, filename)


@app.route("/echarts.js")
def echarts_js():
    # 预压缩下发：规避本地代理对明文响应 ~960KB 的截断上限
    gz = os.path.join(app.static_folder, "assets", "echarts.min.js.gz")
    raw = os.path.join(app.static_folder, "assets", "echarts.min.js")
    if os.path.exists(gz):
        data = open(gz, "rb").read()
        r = Response(data, mimetype="application/javascript")
        r.headers["Content-Encoding"] = "gzip"
        r.headers["Content-Length"] = str(len(data))
        r.headers["Cache-Control"] = "public, max-age=3600"
        return r
    # 兜底：无压缩包时直接发原文件
    return send_from_directory(app.static_folder, "assets/echarts.min.js")


@app.route("/api/market")
def api_market():
    try:
        force = request.args.get("force") == "1"
        return jsonify(get_market(force=force))
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/sectors")
def api_sectors():
    try:
        force = request.args.get("force") == "1"
        np_ = request.args.get("net_pct")
        net_pct = float(np_) if np_ not in (None, "",) else None
        lst = sector_matrix(force=force, net_pct=net_pct)
        if lst is None:
            return jsonify({"sectors": [], "note": "板块数据暂不可达，恢复后自动加载"})
        # ★v2.1 §6.8 regime 透传前端作背景语境
        try:
            from overlay import load_overlay
            ov = load_overlay()
            regime = (ov.get("meta") or {}).get("regime") if ov else None
        except Exception:
            regime = None
        return jsonify({"sectors": lst, "regime": regime, "ts": int(time.time())})
    except Exception as e:  # noqa
        return jsonify({"sectors": [], "note": "板块数据源暂不可达：" + str(e)})


@app.route("/api/scan")
def api_scan():
    bk = request.args.get("sector", "")
    if not bk:
        return jsonify({"error": "缺少 sector 参数"}), 400
    try:
        return jsonify(scan_sector(bk))
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/picks")
def api_picks():
    """选股买卖点实时扫描结果（后台预计算缓存）。?refresh=1 触发后台重算。"""
    refresh = request.args.get("refresh") == "1"
    if refresh:
        started = picks.trigger_refresh()
        if not started and not picks.is_computing():
            # 极端情况下 computing 与 trigger 竞态：保险起见再试一次
            picks.trigger_refresh()
    return jsonify({
        "ts": picks.cache_ts(),
        "computing": picks.is_computing(),
        "progress": picks.progress(),
        "last_err": picks.last_err(),
        "note": "选股买卖点为机械纪律枷锁（非Alpha引擎），回测扣费后净亏；仅供研究参考，不构成投资建议。",
        "data": picks.get_cache(),
    })


@app.route("/api/next_day_gap")
def api_next_day_gap():
    """次日高开候选：有缓存秒回，未缓存/过期时后台计算。"""
    refresh = request.args.get("refresh") == "1"
    scope = {
        "main": request.args.get("main", "1") != "0",
        "chi_next": request.args.get("chi_next", "1") != "0",
        "st": request.args.get("st", "0") == "1",
        "price_min": request.args.get("price_min") or None,
        "price_max": request.args.get("price_max") or None,
        "mcap": request.args.get("mcap") or None,
    }
    data = gap_pick.get_cache(scope)
    if refresh:
        gap_pick.trigger_refresh(scope)
    return jsonify({
        "ok": True,
        "computing": gap_pick.is_computing(),
        "ts": gap_pick.cache_ts(),
        "last_err": gap_pick.last_err(),
        "model": gap_model.meta(),
        "scope": scope,
        "data": data,
    })


@app.route("/api/update/check")
def api_update_check():
    """检查 GitHub release 是否有更新。"""
    return jsonify(app_update.check_update())


@app.route("/api/update/download", methods=["POST"])
def api_update_download():
    """下载对应平台安装包到本地数据目录（后台下载，轮询 status）。"""
    force = request.args.get("force") == "1"
    ok, msg = app_update.download_update(force=force)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/update/status")
def api_update_status():
    return jsonify(app_update.update_status())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    """Windows 打包版：启动更新器，退出后替换 exe 并重启。"""
    return jsonify(app_update.apply_update())


@app.route("/api/sells")
def api_sells():
    """持仓卖出信号（后台预计算缓存，读 positions.json）。?refresh=1 触发后台重算。"""
    refresh = request.args.get("refresh") == "1"
    if refresh:
        sell.trigger_refresh()
    return jsonify({
        "ts": sell.cache_ts(),
        "computing": sell.is_computing(),
        "last_err": sell.last_err(),
        "note": "卖点信号为纪律提醒（非自动下单）：高位十字星/长下影→当日或次日清仓，盈利≥30%→减仓；仅供研究参考，不构成投资建议。",
        "data": sell.get_cache(),
    })


@app.route("/api/stock")
def api_stock():
    code = request.args.get("code", "")
    market = int(request.args.get("market", "1"))
    name = request.args.get("name", "")
    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400
    try:
        return jsonify(stock_detail(code, market, name))
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/stock_fill")
def api_stock_fill():
    """单票 LLM 补充端点：前端首屏拿到骨架(llm_pending)后轮询此接口，
    后台 LLM（新闻复检 + 一句话总结）算完即返回，避免详情页长期卡「加载中」。"""
    code = request.args.get("code", "")
    market = int(request.args.get("market", "1"))
    if not code:
        return jsonify({"ready": False, "error": "缺少 code"})
    key = (str(code), int(market))
    with _STOCK_LLM_LOCK:
        c = _STOCK_LLM_CACHE.get(key)
    if c and c.get("done"):
        return jsonify({"ready": True,
                        "news_detail": c.get("news_detail"),
                        "news_ai": c.get("news_ai"),
                        "summary_text": c.get("summary_text"),
                        "summary_mode": c.get("summary_mode")})
    return jsonify({"ready": False})


@app.route("/api/mktcap")
def api_mktcap():
    """批量总市值（亿），供设置面板「股票市值」筛选**按需**调用。

    入参 secids=1.600519,0.000001（逗号分隔）。走腾讯 gtimg 批量接口（一次可查
    数十只），只在用户真的选了市值档位时才被前端调用，不参与 /api/picks 默认
    渲染路径，避免给每轮扫描叠加数百次行情请求。
    """
    raw = (request.args.get("secids") or "").strip()
    if not raw:
        return jsonify({"ok": True, "mv": {}})
    secids = [s for s in (x.strip() for x in raw.split(",")) if s][:600]
    pref = {"1": "sh", "0": "sz", "2": "bj"}
    sym2secid = {}
    for sid in secids:
        mk, _, code = sid.partition(".")
        if not code:
            continue
        sym = pref.get(mk, "sh") + code
        sym2secid[sym] = sid
    out = {}
    syms = list(sym2secid.keys())
    for i in range(0, len(syms), 60):          # 分批，单条 URL 不过长
        batch = syms[i:i + 60]
        try:
            _rate_limit()
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(batch),
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=10, proxies=NO_PROXY)
            txt = r.text.replace("\n", ";")
        except Exception:
            continue
        for seg in txt.split(";"):
            if "v_" not in seg or "=" not in seg:
                continue
            sym = seg.split("=", 1)[0].strip()[2:]
            f = seg.split("=", 1)[1].strip().strip('"').split("~")
            if sym in sym2secid and len(f) > 44:
                try:
                    out[sym2secid[sym]] = float(f[44])      # 总市值(亿)
                except Exception:
                    pass
    return jsonify({"ok": True, "mv": out})


@app.route("/api/holdings", methods=["GET"])
def api_holdings_get():
    """持仓列表（极简本地文件）+ 每只当前状态/最新价，供持仓视图使用。"""
    raw = load_holdings()
    out = []
    for h in raw:
        code = str(h.get("code", "")); market = int(h.get("market", 1)); name = h.get("name", "")
        secid = f"{market}.{code}"
        st = {"key": "-", "code": "NONE", "severity": "normal", "advice": "", "signals": []}
        close = None; pct = None
        dkl = _klines(secid, 101, 120); wkl = _klines(secid, 102, 60)
        m60 = _klines(secid, 60, 60); m15 = _klines(secid, 15, 60)
        if dkl:
            ev = evaluate_stock(dkl, wkl, m60 if m60 else None, m15 if m15 else None,
                                code=code, name=name)
            ev.market = market
            dd = eval_to_dict(ev, holding=True)
            st = dd.get("state", st); close = dd.get("close"); pct = dd.get("pct")
        out.append({"code": code, "market": market, "name": name,
                    "ts": h.get("ts"), "state": st, "close": close, "pct": pct})
    return jsonify({"holdings": out})


@app.route("/api/holdings", methods=["POST"])
def api_holdings_post():
    """增删持仓标记（极简，仅本地文件，不走账号）。"""
    try:
        body = request.get_json(force=True) or {}
        action = body.get("action")  # add / remove
        code = str(body.get("code", ""))
        market = int(body.get("market", 1))
        name = str(body.get("name", ""))
        if not code:
            return jsonify({"error": "缺少 code"}), 400
        lst = load_holdings()
        if action == "add":
            if not any(str(x.get("code")) == code and int(x.get("market", 1)) == market for x in lst):
                lst.append({"code": code, "market": market, "name": name, "ts": int(time.time())})
        elif action == "remove":
            lst = [x for x in lst if not (str(x.get("code")) == code and int(x.get("market", 1)) == market)]
        else:
            return jsonify({"error": "未知 action"}), 400
        save_holdings(lst)
        return jsonify({"ok": True, "holdings": lst})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 做 T 信号（移植 a-trade；仅研究提示，不自动下单）
# ---------------------------------------------------------------------------
_T_STATE_STORE = t_trade.TStateStore()
_T_TRAILING_CFG = t_trade.TrailingConfig()


def _t_universe():
    """扫描对象：positions.json 明细持仓 + holdings.json 星标持仓。"""
    items, seen = [], set()
    for p in sell.load_positions():
        secid = str(p.get("secid") or "")
        if not secid:
            code = str(p.get("code") or "")
            market = int(p.get("market") or (1 if str(code).startswith("6") else 0))
            secid = f"{market}.{code}"
        if not secid or secid in seen:
            continue
        seen.add(secid)
        code = secid.split(".")[-1]
        items.append({
            "secid": secid, "code": code, "name": p.get("name") or code,
            "cost_price": p.get("entry_price"), "shares": p.get("shares"),
        })
    for h in load_holdings():
        code = str(h.get("code", ""))
        if not code:
            continue
        market = int(h.get("market", 1))
        secid = f"{market}.{code}"
        if secid in seen:
            continue
        seen.add(secid)
        items.append({
            "secid": secid, "code": code, "name": h.get("name") or code,
            "cost_price": None, "shares": None,
        })
    return items


def _signal_dict(item, sig, source="signal"):
    d = {
        "secid": item["secid"], "code": item["code"], "name": item["name"],
        "signal_type": sig.signal_type if isinstance(sig, t_trade.TrailingAction) else sig.signal_type.value,
        "signal_name": sig.name if hasattr(sig, "name") else "T仓追踪",
        "strength": getattr(sig, "strength", "strong"),
        "reason": sig.reason,
        "trigger_price": sig.trigger_price,
        "target_price": getattr(sig, "target_price", None),
        "stop_loss": getattr(sig, "stop_loss", None),
        "factor_hits": getattr(sig, "factor_hits", []) or [],
        "source": source,
    }
    if isinstance(getattr(sig, "strength", None), t_trade.SignalStrength):
        d["strength"] = sig.strength.value
    return d


def _t_scan(items):
    out = []
    for item in items:
        try:
            rows = _klines(item["secid"], 5, 180)
            if not rows or len(rows) < 30:
                continue
            signals, df = t_trade.scan_rows(item["code"], rows)
            current_price = float(df.iloc[-1]["close"]) if len(df) else None
            state = _T_STATE_STORE.get(item["code"])
            trailing = None
            if state.status == "holding" and current_price:
                state = _T_STATE_STORE.update_peak(item["code"], current_price)
                trailing = t_trade.check_trailing(
                    state, current_price, _T_TRAILING_CFG
                )
            item_signals = [_signal_dict(item, s) for s in signals]
            if trailing:
                item_signals.insert(0, _signal_dict(item, trailing, source="t_state"))
            out.append({
                "secid": item["secid"], "code": item["code"],
                "name": item["name"], "close": current_price,
                "signals": item_signals,
                "state": {
                    "status": state.status,
                    "entry_price": state.entry_price,
                    "entry_time": state.entry_time,
                    "entry_signal": state.entry_signal,
                    "peak_price": state.peak_price,
                    "lots": state.lots,
                },
                "ts": int(time.time()),
            })
        except Exception as e:
            print("[t_scan] err", item.get("secid"), e)
    return out


@app.route("/api/t_scan", methods=["POST"])
def api_t_scan():
    try:
        body = request.get_json(force=True) or {}
        q = str(body.get("q") or "").strip()
        secids = [str(x).strip() for x in (body.get("secids") or []) if str(x).strip()]
        if q and not secids:
            secid = _resolve_secid(q)
            if not secid:
                return jsonify({"error": "无法识别，请输入6位代码、个股名称或拼音"}), 404
            code = secid.split(".", 1)[1]
            items = [{
                "secid": secid, "code": code,
                "name": _resolve_name(secid, q) or code,
                "cost_price": None, "shares": None,
            }]
        elif secids:
            items = []
            for secid in secids:
                parts = secid.split(".")
                code = parts[-1]
                market = int(parts[0]) if len(parts) > 1 else (
                    1 if str(code).startswith("6") else 0
                )
                items.append({
                    "secid": f"{market}.{code}", "code": code,
                    "name": body.get("name") or code,
                    "cost_price": None, "shares": None,
                })
        else:
            items = _t_universe()
        if not items:
            return jsonify({
                "ok": True, "items": [], "note": "暂无持仓，可传入 secids 指定股票扫描",
            })
        return jsonify({"ok": True, "items": _t_scan(items)})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/t_state", methods=["POST"])
def api_t_state():
    """记录/平掉当日 T 仓状态。action: buy / exit。"""
    try:
        body = request.get_json(force=True) or {}
        code = str(body.get("code", "")).zfill(6)
        action = str(body.get("action", ""))
        if not code or not action:
            return jsonify({"error": "缺少 code 或 action"}), 400
        if action == "buy":
            _T_STATE_STORE.mark_buy(
                code,
                float(body.get("price", 0)),
                float(body.get("lots", 1.0)),
                str(body.get("signal_name", "手动低吸")),
            )
        elif action == "exit":
            _T_STATE_STORE.mark_exit(
                code, status=str(body.get("status", "empty"))
            )
        else:
            return jsonify({"error": "action 只支持 buy / exit"}), 400
        return jsonify({"ok": True, "state": _T_STATE_STORE.get(code).__dict__})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def api_positions_get():
    """持仓列表（含实时卖点信号）。前端「我的持仓」面板数据源。"""
    try:
        positions = sell.load_positions()
        cache = sell.get_cache()
        signals = (cache or {}).get("signals") or []
        sig_map = {str(s.get("secid")): s for s in signals if s.get("secid")}
        exits = []
        for s in signals:
            act = s.get("action")
            if act in ("清仓", "减仓"):
                exits.append({
                    "secid": s.get("secid"),
                    "code": (s.get("secid") or "").split(".", 1)[1] if "." in (s.get("secid") or "") else (s.get("secid") or ""),
                    "name": s.get("name"),
                    "exit_reason": " · ".join(s.get("reasons") or []),
                    "meta": f"现价 {s.get('last_close', '-')} ｜ 浮盈 {s.get('pnl_pct') if s.get('pnl_pct') is not None else '-'}% ｜ 持有 {s.get('hold_days', '-')} 日",
                })
        # 为持仓卡片补齐实时 meta / action
        for p in positions:
            secid = str(p.get("secid", ""))
            s = sig_map.get(secid)
            if s:
                p["meta"] = f"现价 {s.get('last_close', '-')} ｜ 浮盈 {s.get('pnl_pct') if s.get('pnl_pct') is not None else '-'}% ｜ 持有 {s.get('hold_days', '-')} 日"
                p["action"] = s.get("action", "持有")
        return jsonify({
            "ok": True,
            "positions": positions,
            "signals": signals,
            "exits": exits,
            "ts": (cache or {}).get("ts"),
            "computing": sell.is_computing(),
        })
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["POST"])
def api_positions_post():
    """增/改/删持仓（写 positions.json + 同步 holdings.json 星标）。成本价/日期/股数均可留空。

    字段：action(add/update/remove), secid(market.code 如 1.601939), name,
          entry_price, entry_date, shares（留空串或省略即存 null）。
    """
    try:
        body = request.get_json(force=True) or {}
        action = body.get("action")
        secid = str(body.get("secid", "")).strip()
        if not secid or "." not in secid:
            return jsonify({"error": "缺少/非法 secid（应为 market.code，如 1.601939）"}), 400
        name = str(body.get("name", ""))
        try:
            market, code = secid.split(".", 1)
            market = int(market); code = str(code)
        except Exception:
            return jsonify({"error": "secid 格式错误"}), 400

        positions = sell.load_positions()
        idx = next((i for i, p in enumerate(positions) if str(p.get("secid")) == secid), None)

        if action == "remove":
            if idx is not None:
                positions.pop(idx)
        elif action in ("add", "update"):
            ep = body.get("entry_price"); ed = body.get("entry_date"); sh = body.get("shares")
            rec = {
                "secid": secid,
                "name": name,
                "entry_price": (float(ep) if ep not in (None, "") else None),
                "entry_date": (str(ed) if ed not in (None, "") else None),
                "shares": (int(sh) if sh not in (None, "") else None),
            }
            if idx is not None:
                positions[idx] = rec
            else:
                positions.append(rec)
        else:
            return jsonify({"error": "未知 action（应为 add/update/remove）"}), 400

        # 写 positions.json（持仓+成本，卖点信号源）
        sell.save_positions(positions)
        # 同步 holdings.json（极简星标，供扫描列表高亮）
        try:
            hlst = load_holdings()
            hi = next((i for i, h in enumerate(hlst)
                       if str(h.get("code")) == code and int(h.get("market", 1)) == market), None)
            if action == "remove":
                if hi is not None:
                    hlst.pop(hi)
            else:
                rec_h = {"code": code, "market": market, "name": name, "ts": int(time.time())}
                if hi is not None:
                    hlst[hi] = rec_h
                else:
                    hlst.append(rec_h)
            save_holdings(hlst)
        except Exception as e:
            print("[positions] sync holdings err:", e)

        # 触发后台重算卖点信号（读刚写的 positions.json）
        sell.trigger_refresh()
        return jsonify({"ok": True,
                        "positions": positions,
                        "signals": (sell.get_cache() or {}).get("signals")})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ts": int(time.time())})


# ---------------------------------------------------------------------------
# 个股生命周期状态机（judge_state）
# ---------------------------------------------------------------------------
@app.route("/api/lifecycle")
def api_lifecycle():
    """状态机漏斗全量快照（按阶段分组，实时 compute 保证阶段新鲜）。"""
    try:
        res = lifecycle.compute_lifecycle(force=True)
        if res is None:
            res = lifecycle.get_snapshot()
        return jsonify({
            "ok": True,
            "ts": res["ts"],
            "computing": lifecycle.is_computing(),
            "groups": res["groups"],
            "stats": res["stats"],
        })
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/lifecycle/add", methods=["POST"])
def api_lifecycle_add():
    """通道B：手动加入观察池。字段：secid(market.code), name。"""
    try:
        body = request.get_json(force=True) or {}
        secid = str(body.get("secid", "")).strip()
        if not secid or "." not in secid:
            return jsonify({"error": "缺少/非法 secid（应为 market.code，如 1.601939）"}), 400
        ok = lifecycle.add_manual(secid, str(body.get("name", "")))
        if not ok:
            return jsonify({"error": "该个股已在漏斗中"}), 409
        lifecycle.trigger_refresh()
        return jsonify({"ok": True, "data": lifecycle.get_snapshot()})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/lifecycle/board", methods=["POST"])
def api_lifecycle_board():
    """手动确认上车：阶段→持仓，并写入 positions.json（卖点信号开始跟踪）。
    字段：secid, entry_price?, entry_date?, shares?。"""
    try:
        body = request.get_json(force=True) or {}
        secid = str(body.get("secid", "")).strip()
        if not secid:
            return jsonify({"error": "缺少 secid"}), 400
        e = lifecycle.confirm_board(secid, body.get("entry_price"), body.get("entry_date"), body.get("shares"))
        if not e:
            return jsonify({"error": "漏斗中无此个股"}), 404
        return jsonify({"ok": True, "data": lifecycle.get_snapshot()})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/lifecycle/exit", methods=["POST"])
def api_lifecycle_exit():
    """手动卖出三层确认：secid, layer(1减仓/2减仓大半/3清仓)。layer=3 清仓→移出 positions.json。"""
    try:
        body = request.get_json(force=True) or {}
        secid = str(body.get("secid", "")).strip()
        layer = body.get("layer")
        if not secid or layer not in (1, 2, 3):
            return jsonify({"error": "缺少 secid 或 layer(1/2/3)"}), 400
        e = lifecycle.confirm_exit(secid, layer)
        if not e:
            return jsonify({"error": "漏斗中无此个股"}), 404
        return jsonify({"ok": True, "data": lifecycle.get_snapshot()})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/lifecycle/remove", methods=["POST"])
def api_lifecycle_remove():
    """从漏斗移除（含持仓同步移出 positions.json）。字段：secid。"""
    try:
        body = request.get_json(force=True) or {}
        secid = str(body.get("secid", "")).strip()
        if not secid:
            return jsonify({"error": "缺少 secid"}), 400
        lifecycle.remove(secid)
        return jsonify({"ok": True, "data": lifecycle.get_snapshot()})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/llm_test", methods=["POST"])
def api_llm_test():
    """用当前已保存配置做一次最小调用，验证自定义模型连通性。"""
    from llm_client import llm_test_details
    return jsonify(llm_test_details())


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(public_config())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    try:
        body = request.get_json(force=True) or {}
        llm = body.get("llm", {})
        # key 留空时保留已保存值，避免保存时误清空
        cur_key = load_config().get("llm", {}).get("api_key", "")
        save_config({"llm": {
            "enabled": bool(llm.get("enabled", False)),
            "endpoint": str(llm.get("endpoint", "")).strip(),
            "api_key": (str(llm.get("api_key", "")).strip() or cur_key),
            "model": str(llm.get("model", "")).strip(),
        }})
        return jsonify({"ok": True, **public_config()})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


@app.route("/api/risk")
def api_risk():
    """个股风险独立复检（可在扫描结果里对某只股单独复检）。"""
    code = request.args.get("code", "")
    market = int(request.args.get("market", "1"))
    name = request.args.get("name", "")
    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400
    try:
        risk = compute_risk(code, market, name)
        llm_on = public_config().get("enabled")
        # 复检总结：若配置了模型则尝试模型，否则规则
        return jsonify({"code": code, "risk": risk,
                        "summary": stock_summary({"level": "无", "score": 0,
                                                  "signals": [], "extra": {}},
                                                 risk, llm=bool(llm_on))})
    except Exception as e:  # noqa
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 板块门控 3 轮调度（v2.1）：早10:00定调 / 午13:30上午总结 / 下午14:30收盘前机会点
# backend 常驻后台线程，到点强制刷新 top40 板块的 board_quality 重层（四态+相对强度）写缓存，
# 供首页实时读；并对主推候选刷新 LLM 四段式（展示层）。门控纯指标，不跑 LLM 板块成因解释（v2.1 已删）。
# 非交易时段/资金占比未达门槛 → sector_summary 内部自动降级规则模板，不烧 token。
# 保留现有槽位接口（_llm_slot_*）备用，若后续扩到 4-5 轮可无缝接入。
# ---------------------------------------------------------------------------
_SCHED_STARTED = False


def _round_refresh():
    """一轮：强制刷新 top40 板块的 board_quality 重层（四态+相对强度）写缓存，供首页实时读；
    并对主推候选刷新 LLM 四段式（展示层）。门控纯指标，不跑 LLM 板块成因解释（v2.1 已删）。"""
    try:
        lst = get_sector_list()
        if not lst:
            return
        lst.sort(key=lambda x: -(x.get("pct") or 0))
        top40 = lst[:40]
        hs = _hs300_ret()
        # 重层刷新缓存（纯指标，无 LLM 验真）
        for b in top40:
            try:
                q = board_quality(b, hs)
                _QUALITY_CACHE[b["bk"]] = (time.time(), q)
            except Exception as e:
                print("[scheduler] heavy err", b.get("name"), e)
        # 主推候选刷新四段式（★板块层零 LLM：纯规则模板，展示层不参与 label 判定）
        for b in top40:
            b["quality"] = b.get("quality") or _quick_quality(b)
        cand = [b for b in top40 if (b.get("quality") or {}).get("label") == "主推"]
        cand.sort(key=lambda x: -(x.get("pct") or 0))
        for b in cand[:10]:
            try:
                sector_summary(b, llm=False)
            except Exception as e:
                print("[scheduler] summary err", b.get("name"), e)
        # 选股买卖点：顺带触发后台预计算（若未在计算中），使 /api/picks 常驻新鲜数据
        try:
            picks.trigger_refresh()
        except Exception as e:
            print("[scheduler] picks trigger err", e)
        # 持仓卖出信号：顺带触发（若未在计算中），使 /api/sells 常驻新鲜数据
        try:
            sell.trigger_refresh()
        except Exception as e:
            print("[scheduler] sells trigger err", e)
        # 个股生命周期状态机：顺带触发（若未在计算中），使漏斗阶段常驻新鲜
        try:
            lifecycle.trigger_refresh()
        except Exception as e:
            print("[scheduler] lifecycle trigger err", e)
    except Exception as e:
        print("[scheduler] round err", e)


def _scheduler_loop():
    times = [(10, 0), (13, 30), (14, 30)]   # ★2026-08-05 笔误修正：午槽原写 12:30，与评述槽(13:30)统一
    while True:
        now = datetime.datetime.now()
        # 周末/非交易日跳过：直接睡到下周一 10:00
        if now.weekday() >= 5:
            monday = now + datetime.timedelta(days=(7 - now.weekday()))
            nxt = monday.replace(hour=10, minute=0, second=0, microsecond=0)
            time.sleep(max(1.0, (nxt - now).total_seconds()))
            continue
        cands = []
        for hh, mm in times:
            cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now:
                cands.append(cand)
        nxt = (min(cands) if cands
               else (now.replace(hour=10, minute=0, second=0, microsecond=0)
                     + datetime.timedelta(days=1)))
        slot = (nxt.hour, nxt.minute) if cands else None
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        # 再次检查：sleep 醒来后若已是周末则不执行
        if datetime.datetime.now().weekday() < 5:
            _round_refresh()
            if slot == (14, 30):
                try:
                    gap_pick.trigger_refresh()
                    print("[scheduler] 14:30 次日高开候选已触发", flush=True)
                except Exception as e:
                    print("[scheduler] gap_pick trigger err", e)


def start_board_scheduler():
    global _SCHED_STARTED
    if _SCHED_STARTED:
        return
    _SCHED_STARTED = True
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    print("[scheduler] 板块3轮调度已启动(10:00/13:30/14:30)，14:30 触发高开候选")


if __name__ == "__main__":
    start_board_scheduler()
    # ★2026-08-06 启动即预热板块缓存（后台 22s 拉取），首个用户请求直接命中热缓存，不阻塞首屏。
    _bg_refresh_sectors()
    host = os.environ.get("APANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("APANEL_PORT") or os.environ.get("PORT") or "5000")
    print(f"A股机会雷达 启动中 -> http://{host}:{port}")
    # threaded=True：前端加载会并发打多个接口，其中 /api/sectors(板块矩阵) 需实时算 50~80s。
    # 单线程(threaded=False)下这一个慢请求会占住整台服务，导致页面白屏/刷新打不开。
    # tdx_source 已用 threading.local() 给每个线程各自建 MacClient，且 _call 包了 socket 超时与自动重连，
    # 故多线程是安全的；同时 server 端 _TDX_LOCK 再串行化重负载 TDX 段。改多线程后慢请求不再阻塞其它请求。
    app.run(host=host, port=port, threaded=True)
