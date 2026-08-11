# -*- coding: utf-8 -*-
"""a-stock-data 高价值端点移植（A股全栈数据工具包子集）。

端口：涨停/炸板/跌停/昨涨停、打板情绪、板块资金流、龙虎榜、融资融券、
大宗交易、股东户数、分红送转、120日资金流、研报、互动易、同花顺热榜、
东财人气榜、财联社电报、东财全球资讯/个股新闻。所有东财请求统一走
em_get() 限流，失败返回空结构，不影响主流程。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_API = "https://reportapi.eastmoney.com/report/list"
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_HOT_BODY = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38"}
_NO_PROXY = {"http": None, "https": None}

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})
try:
    _em_adapter = HTTPAdapter(max_retries=Retry(
        total=2,
        connect=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    ))
    _EM_SESSION.mount("https://", _em_adapter)
    _EM_SESSION.mount("http://", _em_adapter)
except Exception:
    pass
_EM_LOCK = threading.Lock()
_EM_LAST = [0.0]
EM_MIN_INTERVAL = 0.8

_EXTRA_LOCK = threading.Lock()
_EXTRA_CACHE = {}
EXTRA_TTL = 6 * 3600


def em_get(url, params=None, headers=None, timeout=15):
    """东财统一请求入口：串行限流 + 会话复用 + 直连。"""
    with _EM_LOCK:
        wait = EM_MIN_INTERVAL - (time.time() - _EM_LAST[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.05, 0.25))
        try:
            return _EM_SESSION.get(
                url, params=params, headers=headers, timeout=timeout, proxies=_NO_PROXY
            )
        finally:
            _EM_LAST[0] = time.time()


def _get(url, params=None, headers=None, timeout=10):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout, proxies=_NO_PROXY)
        r.raise_for_status()
        return r
    except Exception:
        return None


def _post(url, params=None, data=None, json_body=None, headers=None, timeout=10):
    try:
        r = requests.post(
            url,
            params=params,
            data=data,
            json=json_body,
            headers=headers,
            timeout=timeout,
            proxies=_NO_PROXY,
        )
        r.raise_for_status()
        return r
    except Exception:
        return None


def cn_today(fmt="%Y%m%d"):
    return datetime.now(timezone(timedelta(hours=8))).strftime(fmt)


def eastmoney_datacenter(
    report_name,
    filter_str="",
    page_size=50,
    sort_columns="",
    sort_types="-1",
):
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    try:
        r = em_get(DATACENTER_URL, params=params, timeout=15)
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except Exception:
        return []
    return []


def _fmt_zt_time(t):
    s = str(t or "").zfill(6)
    if len(s) < 6:
        return ""
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _em_zt_api(endpoint, sort, date):
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params = {
        "ut": ZTB_UT,
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 10000,
        "sort": sort,
        "date": date,
    }
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    try:
        r = em_get(url, params=params, headers=headers, timeout=10)
        return (r.json().get("data") or {}).get("pool") or []
    except Exception:
        return []


def _zt_stat(p):
    zttj = p.get("zttj") or {}
    return f'{zttj.get("days", "?")}天{zttj.get("ct", "?")}板'


def em_zt_pool(date):
    out = []
    for p in _em_zt_api("getTopicZTPool", "fbt:asc", date):
        try:
            out.append({
                "code": str(p.get("c", "")).zfill(6),
                "name": p.get("n", ""),
                "price": (p.get("p") or 0) / 1000,
                "pct": round(p.get("zdp") or 0, 2),
                "amount": p.get("amount") or 0,
                "turnover": round(p.get("hs") or 0, 2),
                "limit_days": p.get("lbc") or 0,
                "first_seal": _fmt_zt_time(p.get("fbt")),
                "last_seal": _fmt_zt_time(p.get("lbt")),
                "seal_fund": p.get("fund") or 0,
                "break_times": p.get("zbc") or 0,
                "industry": p.get("hybk", ""),
                "zt_stat": _zt_stat(p),
            })
        except Exception:
            continue
    return out


def em_zb_pool(date):
    out = []
    for p in _em_zt_api("getTopicZBPool", "fbt:asc", date):
        try:
            out.append({
                "code": str(p.get("c", "")).zfill(6),
                "name": p.get("n", ""),
                "price": (p.get("p") or 0) / 1000,
                "limit_price": (p.get("ztp") or 0) / 1000,
                "pct": round(p.get("zdp") or 0, 2),
                "turnover": round(p.get("hs") or 0, 2),
                "first_seal": _fmt_zt_time(p.get("fbt")),
                "break_times": p.get("zbc") or 0,
                "amplitude": round(p.get("zf") or 0, 2),
                "speed": round(p.get("zs") or 0, 2),
                "industry": p.get("hybk", ""),
                "zt_stat": _zt_stat(p),
            })
        except Exception:
            continue
    return out


def em_dt_pool(date):
    out = []
    for p in _em_zt_api("getTopicDTPool", "fund:asc", date):
        try:
            out.append({
                "code": str(p.get("c", "")).zfill(6),
                "name": p.get("n", ""),
                "price": (p.get("p") or 0) / 1000,
                "pct": round(p.get("zdp") or 0, 2),
                "turnover": round(p.get("hs") or 0, 2),
                "seal_fund": p.get("fund") or 0,
                "last_seal": _fmt_zt_time(p.get("lbt")),
                "dt_days": p.get("days") or 0,
                "open_times": p.get("oc") or 0,
                "industry": p.get("hybk", ""),
            })
        except Exception:
            continue
    return out


def em_yzt_pool(date):
    out = []
    for p in _em_zt_api("getYesterdayZTPool", "zs:desc", date):
        try:
            out.append({
                "code": str(p.get("c", "")).zfill(6),
                "name": p.get("n", ""),
                "price": (p.get("p") or 0) / 1000,
                "pct": round(p.get("zdp") or 0, 2),
                "turnover": round(p.get("hs") or 0, 2),
                "amplitude": round(p.get("zf") or 0, 2),
                "speed": round(p.get("zs") or 0, 2),
                "y_first_seal": _fmt_zt_time(p.get("yfbt")),
                "y_limit_days": p.get("ylbc") or 0,
                "industry": p.get("hybk", ""),
                "zt_stat": _zt_stat(p),
            })
        except Exception:
            continue
    return out


def limit_up_sentiment(date=None):
    date = date or cn_today()
    zt = em_zt_pool(date)
    zb = em_zb_pool(date)
    dt = em_dt_pool(date)
    ladder = {}
    for s in zt:
        ladder[s["limit_days"]] = ladder.get(s["limit_days"], 0) + 1
    return {
        "date": date,
        "zt_count": len(zt),
        "zb_count": len(zb),
        "dt_count": len(dt),
        "break_rate": round(len(zb) / (len(zt) + len(zb)) * 100, 1) if (zt or zb) else 0,
        "max_height": max((s["limit_days"] for s in zt), default=0),
        "ladder": dict(sorted(ladder.items())),
    }


def market_sentiment():
    date = cn_today()
    data = limit_up_sentiment(date)
    yzt = em_yzt_pool(date)
    data["yzt_count"] = len(yzt)
    data["promotion_rate"] = None
    if yzt:
        promoted = sum(1 for x in yzt if (x.get("pct") or 0) >= 9.8)
        data["promotion_rate"] = round(promoted / len(yzt) * 100, 1)
    return data


_BOARD_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3", "region": "m:90+t:1"}
_BOARD_PERIOD = {
    "today": ("f62", "f62", "f184", "f3", "f204"),
    "5d": ("f164", "f164", "f165", "f109", "f257"),
    "10d": ("f174", "f174", "f175", "f160", None),
}


def board_fund_flow(board_type="industry", period="today", top_n=20):
    if board_type not in _BOARD_FS:
        return {"error": f"board_type 须为 {list(_BOARD_FS)}"}
    if period not in _BOARD_PERIOD:
        return {"error": f"period 须为 {list(_BOARD_PERIOD)}"}
    fid, f_main, f_pct, f_chg, f_leader = _BOARD_PERIOD[period]
    fields = ["f12", "f14", f_chg, f_main, f_pct]
    if f_leader:
        fields.append(f_leader)
    if period == "today":
        fields += ["f66", "f72", "f78", "f84"]
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    base = {
        "pz": "200",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": fid,
        "fs": _BOARD_FS[board_type],
        "fields": ",".join(dict.fromkeys(fields)),
    }

    def _page(pn):
        try:
            r = em_get(url, params={**base, "pn": str(pn)}, headers={"User-Agent": UA}, timeout=15)
            d = r.json().get("data") or {}
            return (d.get("diff") or []), int(d.get("total") or 0)
        except Exception:
            return [], 0

    items, total = _page(1)
    pn = 2
    while len(items) < top_n:
        if total and len(items) >= total:
            break
        more, _ = _page(pn)
        if not more:
            break
        items += more
        pn += 1
        if len(more) < 200:
            break
    total = max(total, len(items))
    rows = []
    for i, it in enumerate(items):
        row = {
            "rank": i + 1,
            "name": it.get("f14", ""),
            "code": it.get("f12", ""),
            "change_pct": it.get(f_chg, 0),
            "main_net": it.get(f_main, 0),
            "main_pct": it.get(f_pct, 0),
            "leader": it.get(f_leader, "") if f_leader else "",
        }
        if period == "today":
            row.update({
                "super_large_net": it.get("f66", 0),
                "large_net": it.get("f72", 0),
                "medium_net": it.get("f78", 0),
                "small_net": it.get("f84", 0),
            })
        rows.append(row)
    return {"board_type": board_type, "period": period, "total": total, "rows": rows[:top_n]}


def dragon_tiger_board(code, trade_date=None, look_back=30):
    code = str(code).zfill(6)
    trade_date = trade_date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)).strftime("%Y-%m-%d")
    except Exception:
        start = trade_date
    records = []
    data = eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{code}\")",
        page_size=50,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )
    for row in data:
        records.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "reason": row.get("EXPLANATION", ""),
            "net_buy": round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
            "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
        })
    buy_data, sell_data = [], []
    seats = {"buy": [], "sell": []}
    if records:
        latest = records[0]["date"]
        buy_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
            page_size=10,
            sort_columns="BUY",
            sort_types="-1",
        )
        for row in buy_data[:5]:
            seats["buy"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                "net": round((row.get("NET") or 0) / 10000, 1),
            })
        sell_data = eastmoney_datacenter(
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
            page_size=10,
            sort_columns="SELL",
            sort_types="-1",
        )
        for row in sell_data[:5]:
            seats["sell"].append({
                "name": row.get("OPERATEDEPT_NAME", ""),
                "buy_amt": round((row.get("BUY") or 0) / 10000, 1),
                "sell_amt": round((row.get("SELL") or 0) / 10000, 1),
                "net": round((row.get("NET") or 0) / 10000, 1),
            })
    institution = {"buy_amt": 0, "sell_amt": 0, "net_amt": 0}
    for detail_data, side in [(buy_data, "buy"), (sell_data, "sell")]:
        for row in detail_data:
            if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                amt = (row.get("BUY") or 0) if side == "buy" else (row.get("SELL") or 0)
                institution["buy_amt" if side == "buy" else "sell_amt"] += amt
    institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
    institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
    institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)
    return {"records": records, "seats": seats, "institution": institution}


def margin_trading(code, page_size=30):
    data = eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{str(code).zfill(6)}")',
        page_size=page_size,
        sort_columns="DATE",
        sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
            "rzye": row.get("RZYE", 0),
            "rzmre": row.get("RZMRE", 0),
            "rzche": row.get("RZCHE", 0),
            "rqye": row.get("RQYE", 0),
            "rzrqye": row.get("RZRQYE", 0),
        })
    return rows


def block_trade(code, page_size=20):
    data = eastmoney_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{str(code).zfill(6)}")',
        page_size=page_size,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )
    rows = []
    for row in data:
        close = row.get("CLOSE_PRICE") or 0
        deal_price = row.get("DEAL_PRICE") or 0
        premium = ((deal_price / close - 1) * 100) if close else 0
        rows.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "price": deal_price,
            "close": close,
            "premium_pct": round(premium, 2),
            "vol": row.get("DEAL_VOLUME", 0),
            "amount": row.get("DEAL_AMT", 0),
            "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        })
    return rows


def holder_num_change(code, page_size=10):
    data = eastmoney_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{str(code).zfill(6)}")',
        page_size=page_size,
        sort_columns="END_DATE",
        sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("END_DATE", ""))[:10],
            "holder_num": row.get("HOLDER_NUM", 0),
            "change_num": row.get("HOLDER_NUM_CHANGE", 0),
            "change_ratio": row.get("HOLDER_NUM_RATIO", 0),
            "avg_shares": row.get("AVG_FREE_SHARES", 0),
        })
    return rows


def dividend_history(code, page_size=20):
    data = eastmoney_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{str(code).zfill(6)}")',
        page_size=page_size,
        sort_columns="EX_DIVIDEND_DATE",
        sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("EX_DIVIDEND_DATE", ""))[:10],
            "bonus_rmb": row.get("PRETAX_BONUS_RMB", 0),
            "transfer_ratio": row.get("TRANSFER_RATIO", 0),
            "bonus_ratio": row.get("BONUS_RATIO", 0),
            "plan": row.get("ASSIGN_PROGRESS", ""),
        })
    return rows


def stock_fund_flow_120d(code):
    code = str(code).zfill(6)
    market_code = 1 if code.startswith("6") else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": f"{market_code}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": "120",
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Origin": "https://quote.eastmoney.com",
    }
    try:
        d = em_get(url, params=params, headers=headers, timeout=15).json()
        klines = d.get("data", {}).get("klines", [])
    except Exception:
        return []
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "date": parts[0],
            "main_net": float(parts[1]) if parts[1] != "-" else 0,
            "small_net": float(parts[2]) if parts[2] != "-" else 0,
            "mid_net": float(parts[3]) if parts[3] != "-" else 0,
            "large_net": float(parts[4]) if parts[4] != "-" else 0,
            "super_net": float(parts[5]) if parts[5] != "-" else 0,
        })
    return rows


def _norm_code(code):
    digits = re.sub(r"\D", "", str(code))
    if len(digits) != 6:
        raise ValueError(f"代码无法归一化：{code}")
    return digits


def eastmoney_reports(code, max_pages=2):
    code = _norm_code(code)
    all_records = []
    for page in range(1, max_pages + 1):
        params = {
            "industryCode": "*",
            "pageSize": "100",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": "2000-01-01",
            "endTime": "2030-01-01",
            "pageNo": str(page),
            "fields": "",
            "qType": "0",
            "orgCode": "",
            "code": code,
            "rcode": "",
            "p": str(page),
            "pageNum": str(page),
            "pageNumber": str(page),
        }
        try:
            r = em_get(REPORT_API, params=params, headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
            rows = d.get("data") or []
        except Exception:
            break
        if not rows:
            break
        all_records.extend(rows)
        if page >= int(d.get("TotalPage") or 1):
            break
    out = []
    for row in all_records:
        out.append({
            "date": str(row.get("publishDate", ""))[:10],
            "title": row.get("title", ""),
            "org": row.get("orgSName", ""),
            "rating": row.get("emRatingName", ""),
            "industry": row.get("indvInduName", ""),
            "eps_this": row.get("predictThisYearEps"),
            "eps_next": row.get("predictNextYearEps"),
        })
    return out


def cninfo_irm(code, page_size=30, page_num=1):
    code = str(code).zfill(6)
    try:
        r1 = _post(
            "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
            data={"keyWord": code},
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r1 is None:
            return []
        d1 = r1.json().get("data") or []
        if not d1:
            return []
        org_id = d1[0].get("secid")
        params = {
            "_t": 1,
            "stockcode": code,
            "orgId": org_id,
            "pageSize": page_size,
            "pageNum": page_num,
            "keyWord": "",
            "startDay": "",
            "endDay": "",
        }
        r2 = _post(
            "https://irm.cninfo.com.cn/newircs/company/question",
            params=params,
            headers={"User-Agent": UA},
            timeout=10,
        )
        rows = []
        if r2 is not None:
            rows = r2.json().get("rows") or []
    except Exception:
        return []
    out = []
    for it in rows:
        pd = it.get("pubDate")
        out.append({
            "code": it.get("stockCode"),
            "company": it.get("companyShortName"),
            "question": it.get("mainContent"),
            "answer": it.get("attachedContent"),
            "answerer": it.get("attachedAuthor"),
            "ask_time": datetime.fromtimestamp(pd / 1000).strftime("%Y-%m-%d %H:%M") if pd else "",
        })
    return out


def ths_hot_list(period="hour"):
    try:
        r = _get(
            "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
            params={"stock_type": "a", "type": period, "list_type": "normal"},
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r is None:
            return []
        lst = (r.json().get("data") or {}).get("stock_list") or []
    except Exception:
        return []
    out = []
    for it in lst:
        tag = it.get("tag") or {}
        out.append({
            "rank": it.get("order"),
            "code": it.get("code"),
            "name": it.get("name"),
            "heat": it.get("rate"),
            "pct": it.get("rise_and_fall"),
            "rank_chg": it.get("hot_rank_chg"),
            "concepts": tag.get("concept_tag") or [],
            "tag": tag.get("popularity_tag", ""),
        })
    return out


def em_hot_rank(top=50):
    try:
        r = _post(
            "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
            json_body={**EM_HOT_BODY, "marketType": "", "pageNo": 1, "pageSize": top},
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r is None:
            return []
        data = r.json().get("data") or []
        if not data:
            return []
        secids = [
            ("0." if str(it.get("sc", "")).startswith("SZ") else "1.") + str(it.get("sc", ""))[2:]
            for it in data
        ]
        u = _get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params={
                "ut": "f057cbcbce2a86e2866ab8877db1d059",
                "fltt": 2,
                "invt": 2,
                "fields": "f14,f3,f12,f2",
                "secids": ",".join(secids),
            },
            headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=10,
        )
        diff = (u.json().get("data") or {}).get("diff") or [] if u is not None else []
        if isinstance(diff, dict):
            diff = list(diff.values())
        nm = {x.get("f12"): (x.get("f14"), x.get("f2"), x.get("f3")) for x in diff}
    except Exception:
        return []
    out = []
    for it in data:
        code = str(it.get("sc", ""))[2:]
        name, price, pct = nm.get(code, ("", None, None))
        out.append({
            "rank": it.get("rk"),
            "code": code,
            "name": name,
            "price": price,
            "pct": pct,
            "rank_chg": it.get("hisRc"),
        })
    return out


def em_hot_concept(code):
    code = str(code).zfill(6)
    prefix = "SH" if code.startswith("6") else "SZ"
    try:
        r = _post(
            "https://emappdata.eastmoney.com/stockrank/getHotStockRankList",
            json_body={**EM_HOT_BODY, "srcSecurityCode": prefix + code},
            headers={"User-Agent": UA},
            timeout=10,
        )
        if r is None:
            return []
        data = r.json().get("data") or []
    except Exception:
        return []
    return [{
        "concept": x.get("conceptName"),
        "bk": x.get("conceptId"),
        "hit": x.get("hitCount"),
    } for x in data]


def eastmoney_stock_news(code, page_size=20):
    code = str(code).zfill(6)
    inner = json.dumps({
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default",
            "sort": "default",
            "pageIndex": 1,
            "pageSize": page_size,
            "preTag": "",
            "postTag": "",
        }},
    }, separators=(",", ":"))
    try:
        r = em_get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "jQuery_news", "param": inner},
            headers={"User-Agent": UA, "Referer": "https://so.eastmoney.com/"},
            timeout=15,
        )
        text = r.text
        if "(" not in text or ")" not in text:
            return []
        d = json.loads(text[text.index("(") + 1:text.rindex(")")])
        articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
    except Exception:
        return []
    out = []
    for a in articles:
        out.append({
            "title": re.sub(r"<[^>]+>", "", a.get("title", "")),
            "content": re.sub(r"<[^>]+>", "", a.get("content", ""))[:200],
            "time": a.get("date", ""),
            "source": a.get("mediaName", ""),
            "url": a.get("url", ""),
        })
    return out


def cls_telegraph(page_size=50):
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "last_time": "",
        "refresh_type": "1",
        "rn": str(page_size),
    }
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.cls.cn/"}, timeout=10, proxies=_NO_PROXY)
        r.raise_for_status()
        d = r.json()
        items = d.get("data", {}).get("roll_data", []) or []
    except Exception:
        return []
    out = []
    for item in items:
        ts = item.get("ctime")
        t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        out.append({
            "title": item.get("title", "") or item.get("brief", ""),
            "content": item.get("content", "") or item.get("brief", ""),
            "time": t,
        })
    return out


def eastmoney_global_news(page_size=50):
    params = {
        "client": "web",
        "biz": "web_724",
        "fastColumn": "102",
        "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    try:
        r = em_get(
            "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
            params=params,
            headers={"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"},
            timeout=10,
        )
        d = r.json()
        items = d.get("data", {}).get("fastNewsList", []) or []
    except Exception:
        return []
    return [{
        "title": item.get("title", ""),
        "summary": item.get("summary", "")[:200],
        "time": item.get("showTime", ""),
    } for item in items]


def tencent_quote(codes):
    """腾讯财经批量行情：PE/PB/市值/换手/涨跌停价等，带前缀路由。"""
    sh_index = {"000300", "000905", "000016", "000688", "000852", "000010"}
    prefixed = []
    key_of = {}
    for c in codes:
        low = str(c).lower()
        if low.startswith(("sh", "sz", "bj")):
            p = low
        elif str(c).startswith("92"):
            p = f"bj{c}"
        elif str(c) in sh_index or str(c).startswith(("5", "6", "9")):
            p = f"sh{c}"
        elif str(c).startswith(("4", "8")):
            p = f"bj{c}"
        else:
            p = f"sz{c}"
        prefixed.append(p)
        key_of[p] = str(c)
    try:
        r = requests.get(
            "https://qt.gtimg.cn/q=" + ",".join(prefixed),
            headers={"User-Agent": UA},
            timeout=10,
            proxies=_NO_PROXY,
        )
        r.encoding = "gbk"
        text = r.text
    except Exception:
        return {}
    result = {}
    for line in text.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key_of.get(key, key[2:])

        def _f(idx):
            try:
                return float(vals[idx])
            except (TypeError, ValueError, IndexError):
                return 0.0

        result[code] = {
            "name": vals[1] if len(vals) > 1 else "",
            "price": _f(3),
            "last_close": _f(4),
            "open": _f(5),
            "change_pct": _f(32),
            "high": _f(33),
            "low": _f(34),
            "amount_wan": _f(37),
            "turnover_pct": _f(38),
            "pe_ttm": _f(39),
            "float_mcap_yi": _f(44),
            "mcap_yi": _f(45),
            "pb": _f(46),
            "limit_up": _f(47),
            "limit_down": _f(48),
            "vol_ratio": _f(49),
            "pe_static": _f(52),
        }
    return result


def baidu_kline(code, lmt=240):
    """百度股市通日K线兜底（自带 MA，这里只取 OHLCV）。"""
    code = str(code).zfill(6)
    url = "https://finance.pae.baidu.com/selfselect/getstockquotation"
    params = {
        "all": "1",
        "isIndex": "false",
        "isBk": "false",
        "isBlock": "false",
        "isFutures": "false",
        "isStock": "true",
        "newFormat": "1",
        "group": "quotation_kline_ab",
        "finClientType": "pc",
        "code": code,
        "start_time": "",
        "ktype": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": "https://gushitong.baidu.com",
        "Referer": "https://gushitong.baidu.com/",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10, proxies=_NO_PROXY)
        r.raise_for_status()
        d = r.json()
        md = (d.get("Result") or {}).get("newMarketData") or {}
        keys = md.get("keys", [])
        raw = str(md.get("marketData", ""))
    except Exception:
        return []
    rows = []
    for line in raw.split(";"):
        parts = line.split(",")
        if len(parts) < 7:
            continue
        item = dict(zip(keys, parts))
        try:
            rows.append({
                "day": str(item.get("time", ""))[:10],
                "open": float(item.get("open", 0)),
                "high": float(item.get("high", 0)),
                "low": float(item.get("low", 0)),
                "close": float(item.get("close", 0)),
                "volume": float(item.get("volume", 0) or 0),
                "amount": float(item.get("amount", 0) or 0),
            })
        except (TypeError, ValueError):
            continue
    return rows[-lmt:]


def stock_extra(code):
    code = str(code).zfill(6)
    key = f"{code}:{cn_today('%Y-%m-%d')}"
    with _EXTRA_LOCK:
        hit = _EXTRA_CACHE.get(key)
        if hit and time.time() - hit["ts"] < EXTRA_TTL:
            return hit["data"]

    def _safe(fn, default):
        try:
            return fn() or default
        except Exception:
            return default

    tasks = {
        "dragon_tiger": lambda: _safe(lambda: dragon_tiger_board(code), {}),
        "margin": lambda: _safe(lambda: margin_trading(code, 10), []),
        "block_trade": lambda: _safe(lambda: block_trade(code, 10), []),
        "holder": lambda: _safe(lambda: holder_num_change(code, 8), []),
        "dividend": lambda: _safe(lambda: dividend_history(code, 10), []),
        "fund_flow": lambda: _safe(lambda: stock_fund_flow_120d(code)[-20:], []),
        "reports": lambda: _safe(lambda: eastmoney_reports(code, max_pages=2), []),
        "irm": lambda: _safe(lambda: cninfo_irm(code, 10), []),
        "hot_concept": lambda: _safe(lambda: em_hot_concept(code), []),
        "news": lambda: _safe(lambda: eastmoney_stock_news(code, 10), []),
    }
    result = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {name: ex.submit(fn) for name, fn in tasks.items()}
        for name, fut in futures.items():
            try:
                result[name] = fut.result(timeout=20)
            except Exception:
                result[name] = {}
    with _EXTRA_LOCK:
        _EXTRA_CACHE[key] = {"ts": time.time(), "data": result}
        if len(_EXTRA_CACHE) > 200:
            _EXTRA_CACHE.clear()
    return result
