# -*- coding: utf-8 -*-
"""次日高开候选（移植 a-trade next_day_candidates 逻辑，数据源走 HKS 统一入口）。

口径：筛选今日未涨停、通过主板/ST/价格/基本面/行业硬过滤的个股，用
量价与板块热度因子等权评分，输出次日 T+1 开盘高开概率较高的 Top N。
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache

import pandas as pd
import requests

import paths

# 延迟导入 server：与 picks 相同，运行时 server 已完全加载。
import server

MAIN_BOARD_PREFIXES = ("000", "001", "002", "600", "601", "603", "605")

BLOCKED_INDUSTRY_KEYWORDS = (
    "白酒",
    "证券",
    "地产",
    "消费",
    "房地产",
    "食品",
    "饮料",
    "零售",
    "商贸",
    "家电",
)

MAX_PRICE = 80.0
MAX_PE_TTM = 100.0
MAX_PB = 5.0
NOT_LIMIT_PCT = 9.8

SCORE_FACTORS_HIGH = (
    "amplitude_pct",
    "pos_ma20",
    "pos_ma60",
    "dist_low60",
    "amount_yi",
    "vol_ratio_5",
    "industry_limit_count",
)
SCORE_FACTORS_LOW = ("dist_high60",)

SNAPSHOT_PAGE_SIZE = 100
SNAPSHOT_MAX_PAGES = 60
HISTORY_BARS = 150
MIN_HISTORY_BARS = 65
CACHE_TTL = 600
TOP_N = 10

_GAP_CACHE = {"ts": 0, "data": None, "computing": False, "last_err": None}
_GAP_LOCK = threading.Lock()


def _scope_key(scope=None):
    scope = scope or {}
    return "|".join(
        str(scope.get(k, ""))
        for k in ("main", "chi_next", "st", "price_min", "price_max", "mcap")
    )


def _to_float(value, default=0.0):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_main_board(code: str) -> bool:
    return str(code).zfill(6).startswith(MAIN_BOARD_PREFIXES)


def _board_of(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
        return "main"
    if code.startswith(("300", "301")) or code.startswith(("688", "689")):
        return "chi_next"
    return "other"


def _is_st_name(name: str) -> bool:
    upper = str(name or "").upper()
    return upper.startswith("ST") or upper.startswith("*ST") or "退" in upper


def _industry_allowed(industry: str) -> bool:
    return not any(keyword in str(industry) for keyword in BLOCKED_INDUSTRY_KEYWORDS)


def _price_ok(price, scope=None) -> bool:
    try:
        price = float(price)
        if not (0.0 < price <= MAX_PRICE):
            return False
        scope = scope or {}
        price_min = scope.get("price_min")
        price_max = scope.get("price_max")
        if price_min not in (None, ""):
            price_min = float(price_min)
            if price < price_min:
                return False
        if price_max not in (None, ""):
            price_max = float(price_max)
            if price > price_max:
                return False
        return True
    except (TypeError, ValueError):
        return False


def _fundamentals_ok(pe_ttm=None, pb=None) -> bool:
    if pe_ttm is not None:
        try:
            if float(pe_ttm) <= 0 or float(pe_ttm) > MAX_PE_TTM:
                return False
        except (TypeError, ValueError):
            return False
    if pb is not None:
        try:
            if float(pb) > MAX_PB:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _not_limit_up(pct_chg) -> bool:
    try:
        return float(pct_chg) < NOT_LIMIT_PCT
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=8192)
def _stock_industry(code: str) -> str:
    """东财 F10 取行业（EM2016），失败返回空串。"""
    code = str(code).zfill(6)
    market = "SH" if code.startswith("6") else "SZ"
    try:
        r = requests.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
            params={"code": f"{market}{code}"},
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://emweb.securities.eastmoney.com/"},
            timeout=6,
            proxies=server.NO_PROXY,
        )
        r.raise_for_status()
        jbzl = (r.json() or {}).get("jbzl") or [{}]
        return str((jbzl[0] or {}).get("EM2016") or "").strip()
    except Exception:
        return ""


def fetch_market_snapshot(max_pages=SNAPSHOT_MAX_PAGES) -> pd.DataFrame:
    """新浪全市场快照（HKS 现有数据源，字段兼容 a-trade 契约）。"""
    rows = []
    for pn in range(1, max_pages + 1):
        try:
            r = requests.get(
                "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData",
                params={
                    "page": pn,
                    "num": SNAPSHOT_PAGE_SIZE,
                    "sort": "symbol",
                    "asc": 1,
                    "node": "hs_a",
                    "symbol": "",
                    "_s_r_a": "page",
                },
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://finance.sina.com.cn/"},
                timeout=10,
                proxies=server.NO_PROXY,
            )
            r.raise_for_status()
            diff = r.json() or []
        except Exception as e:
            print(f"[gap_pick] snapshot page {pn} err: {e}")
            break
        if not diff:
            break
        for it in diff:
            rows.append({
                "code": str(it.get("code", "")).zfill(6),
                "name": it.get("name"),
                "price": _to_float(it.get("trade"), None),
                "pct_chg": _to_float(it.get("changepercent"), None),
                "volume_lots": _to_float(it.get("volume")) / 100,
                "amount": _to_float(it.get("amount")),
                "high": _to_float(it.get("high"), None),
                "low": _to_float(it.get("low"), None),
                "pe_ttm": _to_float(it.get("per"), None),
                "pb": _to_float(it.get("pb"), None),
                "mktcap": _to_float(it.get("mktcap"), None),
            })
        if len(diff) < SNAPSHOT_PAGE_SIZE:
            break
    return pd.DataFrame(rows)


def fetch_zt_pool(trade_date: str) -> pd.DataFrame:
    """东方财富涨停池；失败返回空 DataFrame。"""
    compact = str(trade_date).replace("-", "")
    data = server.em_get(
        "https://push2ex.eastmoney.com",
        "/getTopicZTPool",
        {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 300,
            "sort": "fbt:asc",
            "date": compact,
        },
        timeout=8,
        retries=2,
    )
    payload = (data or {}).get("data") or {}
    pool = payload.get("pool") or []
    rows = []
    for r in pool:
        rows.append({
            "代码": str(r.get("c", "")).zfill(6),
            "名称": r.get("n", ""),
            "涨跌幅": _to_float(r.get("zdp")),
            "连板数": r.get("lbc") or 0,
            "所属行业": r.get("hybk") or "未知行业",
        })
    return pd.DataFrame(rows)


def _add_limit_labels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["pre_close"] = out["close"].shift(1)
    out["is_limit_up"] = (
        (out["close"] >= out["pre_close"] * (1 + 0.099))
        & (out["pre_close"] > 0)
    )
    streak = []
    current = 0
    for flag in out["is_limit_up"].tolist():
        current = current + 1 if flag else 0
        streak.append(current)
    out["limit_streak"] = streak
    return out


def _add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["vol_ratio_5"] = out["volume"] / out["volume"].rolling(5).mean().shift(1)
    prev_close = out["pre_close"] if "pre_close" in out.columns else out["close"].shift(1)
    out["amplitude_pct"] = (out["high"] - out["low"]) / prev_close * 100
    span = out["high"] - out["low"]
    out["body_ratio"] = ((out["close"] - out["open"]) / span).where(span > 0, 1.0)
    out["pos_ma20"] = out["close"] / out["close"].rolling(20).mean() - 1
    out["pos_ma60"] = out["close"] / out["close"].rolling(60).mean() - 1
    high60 = out["high"].rolling(60).max().shift(1)
    low60 = out["low"].rolling(60).min().shift(1)
    out["dist_high60"] = out["close"] / high60 - 1
    out["dist_low60"] = out["close"] / low60 - 1
    amount = out["amount"] if "amount" in out.columns else out["close"] * out["volume"]
    out["amount_yi"] = amount / 1e8
    return out


def _history_df(secid: str, trade_date: str, price, high, low, volume_lots, amount=None) -> pd.DataFrame:
    try:
        with server._TDX_LOCK:
            rows = server._klines(secid, 101, HISTORY_BARS)
    except Exception:
        rows = []
    if not rows or len(rows) < MIN_HISTORY_BARS:
        return None
    records = []
    for r in rows:
        try:
            close = float(r["close"])
            vol = float(r.get("vol") or 0)
            amount = float(r.get("amount") or 0) or close * vol
            records.append({
                "date": str(r["date"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": close,
                "volume": vol,
                "amount": amount,
            })
        except (KeyError, TypeError, ValueError):
            continue
    if len(records) < MIN_HISTORY_BARS:
        return None
    df = pd.DataFrame(records).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    last_date = str(df.iloc[-1]["date"])[:10]
    today = {
        "date": trade_date,
        "open": _to_float(price),
        "high": _to_float(high, price),
        "low": _to_float(low, price),
        "close": _to_float(price),
        "volume": int(_to_float(volume_lots, 0) * 100),
        "amount": _to_float(amount) or _to_float(price) * int(_to_float(volume_lots, 0) * 100),
    }
    if last_date == trade_date:
        for col in ("open", "high", "low", "close", "volume", "amount"):
            df.loc[df.index[-1], col] = today[col]
    else:
        df = pd.concat([df, pd.DataFrame([today])], ignore_index=True)
    df = _add_limit_labels(df)
    df = _add_features(df)
    return df


def build_candidates(
    snapshot_df: pd.DataFrame,
    zt_df: pd.DataFrame,
    trade_date: str,
    scope: dict | None = None,
) -> list[dict]:
    scope = scope or {}
    out = []
    total = len(snapshot_df)
    for idx, (_, row) in enumerate(snapshot_df.iterrows()):
        if idx % 200 == 0:
            print(f"[gap_pick] 扫描 {idx}/{total} …候选 {len(out)}", flush=True)
        code = str(row.get("code", "")).zfill(6)
        name = str(row.get("name", ""))
        board = _board_of(code)
        if board == "other":
            continue
        if board == "main" and not scope.get("main", True):
            continue
        if board == "chi_next" and not scope.get("chi_next", True):
            continue
        if not scope.get("st", False) and _is_st_name(name):
            continue
        if not _price_ok(row.get("price"), scope):
            continue
        if not _fundamentals_ok(row.get("pe_ttm"), row.get("pb")):
            continue
        if not _not_limit_up(row.get("pct_chg")):
            continue
        mcap = _to_float(row.get("mktcap"), None)
        mcap_mode = scope.get("mcap")
        if mcap is not None and mcap_mode:
            if mcap_mode == "small" and not mcap < 100:
                continue
            if mcap_mode == "mid" and not (100 <= mcap <= 500):
                continue
            if mcap_mode == "large" and not mcap > 500:
                continue
        try:
            industry = _stock_industry(code) or "未知行业"
            if not _industry_allowed(industry):
                continue
            hist = _history_df(
                f"{'1' if code[0] in '689' else '0'}.{code}",
                trade_date,
                row.get("price"),
                row.get("high"),
                row.get("low"),
                row.get("volume_lots"),
                row.get("amount"),
            )
            if hist is None:
                continue
            last = hist.iloc[-1]
            features = {
                "vol_ratio_5": _to_float(last.get("vol_ratio_5"), None),
                "amplitude_pct": _to_float(last.get("amplitude_pct"), None),
                "dist_high60": _to_float(last.get("dist_high60"), None),
                "dist_low60": _to_float(last.get("dist_low60"), None),
                "pos_ma20": _to_float(last.get("pos_ma20"), None),
                "pos_ma60": _to_float(last.get("pos_ma60"), None),
                "amount_yi": _to_float(last.get("amount_yi"), None),
            }
            if any(v is None or pd.isna(v) for v in features.values()):
                continue
            out.append({
                "code": code,
                "name": name,
                "price": _to_float(row.get("price")),
                "change_pct": _to_float(row.get("pct_chg")),
                "industry": industry,
                **features,
            })
        except Exception as e:
            print(f"[gap_pick] skip {code}: {e}", flush=True)
            continue
    if not out:
        return []
    heat = {}
    if not zt_df.empty:
        industry_col = "所属行业" if "所属行业" in zt_df.columns else "industry"
        heat = (
            zt_df.assign(_industry=zt_df[industry_col].fillna("未知行业").astype(str))
            .groupby("_industry")
            .size()
            .to_dict()
        )
    for candidate in out:
        candidate["industry_limit_count"] = int(heat.get(candidate["industry"], 0))
    return out


def score_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    df = pd.DataFrame(candidates)
    for column in SCORE_FACTORS_HIGH:
        df[f"{column}_hit"] = df[column] >= df[column].median()
    for column in SCORE_FACTORS_LOW:
        df[f"{column}_hit"] = df[column] <= df[column].median()
    hit_columns = [
        f"{column}_hit"
        for column in SCORE_FACTORS_HIGH + SCORE_FACTORS_LOW
    ]
    df["score"] = df[hit_columns].sum(axis=1).astype(int)
    df["reason"] = df.apply(_recommend_reason, axis=1)
    df = df.sort_values(["score", "industry_limit_count"], ascending=[False, False])
    return df.to_dict("records")


def _recommend_reason(row) -> str:
    parts = []
    if row.get("vol_ratio_5_hit"):
        parts.append(f"量比{row['vol_ratio_5']:.2f}")
    if row.get("amplitude_pct_hit"):
        parts.append(f"振幅{row['amplitude_pct']:.1f}%")
    if row.get("pos_ma20_hit"):
        parts.append("站上MA20")
    if row.get("pos_ma60_hit"):
        parts.append("站上MA60")
    if row.get("dist_low60_hit"):
        parts.append("远离60日低点")
    if row.get("amount_yi_hit"):
        parts.append(f"成交{row['amount_yi']:.0f}亿")
    if row.get("industry_limit_count_hit"):
        parts.append(f"板块涨停{row['industry_limit_count']}家")
    if row.get("dist_high60_hit"):
        parts.append("贴近60日高点")
    if parts:
        return " · ".join(parts)
    return "无明显突出因子" if not row.get("score") else "综合评分靠前"


def _compute(scope=None):
    scope = scope or {}
    if not scope.get("main", True) and not scope.get("chi_next", True):
        return {
            "date": time.strftime("%Y-%m-%d"),
            "ts": int(time.time()),
            "elapsed_sec": 0.0,
            "total": 0,
            "candidates": [],
            "scope_key": _scope_key(scope),
        }
    t0 = time.time()
    trade_date = time.strftime("%Y-%m-%d")
    zt_df = fetch_zt_pool(trade_date)
    print(f"[gap_pick] 涨停池 {len(zt_df)} 只", flush=True)
    snapshot_df = fetch_market_snapshot()
    if snapshot_df.empty:
        raise RuntimeError("全市场快照为空")
    print(f"[gap_pick] 全市场快照 {len(snapshot_df)} 行，开始筛选", flush=True)
    allowed_boards = []
    if scope.get("main", True):
        allowed_boards.append("main")
    if scope.get("chi_next", True):
        allowed_boards.append("chi_next")
    scoped_df = snapshot_df[
        snapshot_df["code"].astype(str).str.zfill(6).map(_board_of).isin(allowed_boards)
    ].copy()
    print(f"[gap_pick] 交易权限范围内 {len(scoped_df)} 行", flush=True)
    # 涨停池接口失败时，用快照中涨幅 >=9.8% 的票近似补行业热度，不阻塞主流程。
    if zt_df.empty:
        approx = snapshot_df[pd.to_numeric(snapshot_df["pct_chg"], errors="coerce") >= 9.8].copy()
        if not approx.empty:
            approx = approx.rename(columns={"code": "代码", "name": "名称", "pct_chg": "涨跌幅"})
            approx["连板数"] = 1
            approx["所属行业"] = approx["代码"].map(_stock_industry).fillna("未知行业")
            zt_df = approx[["代码", "名称", "涨跌幅", "连板数", "所属行业"]]
    candidates = build_candidates(scoped_df, zt_df, trade_date, scope)
    print(f"[gap_pick] 硬过滤后候选 {len(candidates)} 只，开始评分", flush=True)
    scored = score_candidates(candidates)
    return {
        "date": trade_date,
        "ts": int(time.time()),
        "elapsed_sec": round(time.time() - t0, 1),
        "total": len(scored),
        "candidates": scored[:TOP_N],
        "scope_key": _scope_key(scope),
    }


def trigger_refresh(scope=None) -> bool:
    scope = scope or {}
    with _GAP_LOCK:
        if _GAP_CACHE["computing"]:
            return False
        _GAP_CACHE["computing"] = True

    def _run():
        try:
            data = _compute(scope)
            with _GAP_LOCK:
                _GAP_CACHE.update(ts=data["ts"], data=data, last_err=None)
            print(f"[gap_pick] 完成：候选 {data['total']} 只，用时 {data['elapsed_sec']}s", flush=True)
        except Exception as e:
            with _GAP_LOCK:
                _GAP_CACHE["last_err"] = str(e)
            print(f"[gap_pick] err: {e}", flush=True)
        finally:
            with _GAP_LOCK:
                _GAP_CACHE["computing"] = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def get_cache(scope=None):
    key = _scope_key(scope)
    with _GAP_LOCK:
        data = _GAP_CACHE["data"]
        ts = _GAP_CACHE["ts"]
        computing = _GAP_CACHE["computing"]
    if data and data.get("scope_key") == key and time.time() - ts < CACHE_TTL:
        return data
    if not computing:
        trigger_refresh(scope)
    return data if data and data.get("scope_key") == key else None


def cache_ts():
    with _GAP_LOCK:
        return _GAP_CACHE["ts"]


def is_computing():
    with _GAP_LOCK:
        return _GAP_CACHE["computing"]


def last_err():
    with _GAP_LOCK:
        return _GAP_CACHE["last_err"]


if __name__ == "__main__":
    result = _compute()
    for c in result["candidates"]:
        print(c["code"], c["name"], c["score"])
