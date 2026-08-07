"""做 T 信号引擎与 T 仓状态管理（移植自 a-trade，数据层由 HKS 后端提供）。

核心思路：
- 低吸（BUY）：波段反弹 / 趋势确认 / 放量突破 / 超卖反弹，因子共振越强越值得做。
- 高抛（SELL）：放量拉升 / MACD 顶背离。
- 止损（STOP_LOSS）：连续 3 根 K 线跌破 MA20。
- T 仓状态：记录当日低吸入场价、手数、峰值，按锁利/止损线给出退出提示。

本模块不自动下单，只输出信号与研究参考。
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

import numpy as np
import pandas as pd

import paths


# ============================================================
# 技术指标（与 a-trade/atrade/indicators 同构，纯 Python 实现）
# ============================================================
def sma(series: pd.Series, n: int = 5) -> pd.Series:
    return series.rolling(n, min_periods=1).mean()


def ema(series: pd.Series, n: int = 12) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    lowest_low = low.rolling(n, min_periods=1).min()
    highest_high = high.rolling(n, min_periods=1).max()
    rsv = (close - lowest_low) / (highest_high - lowest_low) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(n, min_periods=1).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(n, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_val = 100 - 100 / (1 + rs)
    return rsi_val.fillna(50)


def boll(
    close: pd.Series,
    n: int = 20,
    k: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=1).std()
    upper = mid + k * std
    lower = mid - k * std
    return mid, upper, lower


def vol_ma(volume: pd.Series, n: int = 5) -> pd.Series:
    return volume.rolling(n, min_periods=1).mean()


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]
    df["MA5"] = sma(close, 5)
    df["MA10"] = sma(close, 10)
    df["MA20"] = sma(close, 20)
    df["EMA12"] = ema(close, 12)
    df["EMA26"] = ema(close, 26)
    dif, dea, hist = macd(close)
    df["MACD_DIF"] = dif
    df["MACD_DEA"] = dea
    df["MACD_HIST"] = hist
    k, d, j = kdj(high, low, close)
    df["KDJ_K"] = k
    df["KDJ_D"] = d
    df["KDJ_J"] = j
    df["RSI6"] = rsi(close, 6)
    df["RSI14"] = rsi(close, 14)
    mid, upper, lower = boll(close)
    df["BOLL_MID"] = mid
    df["BOLL_UPPER"] = upper
    df["BOLL_LOWER"] = lower
    df["VOL_MA5"] = vol_ma(vol, 5)
    df["VOL_MA10"] = vol_ma(vol, 10)
    return df


class SignalType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    STOP_LOSS = "stop_loss"
    WATCH = "watch"


class SignalStrength(str, Enum):
    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


@dataclass
class Signal:
    symbol: str
    signal_type: SignalType
    strength: SignalStrength
    name: str
    reason: str
    trigger_price: float
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    position_pct: float = 0.33
    factor_hits: list[str] = None
    timestamp: str = ""

    def __post_init__(self):
        if self.factor_hits is None:
            self.factor_hits = []
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")


class SignalEngine:
    """做 T 信号检测引擎（移植 a-trade）。"""

    def scan(self, symbol: str, df: pd.DataFrame) -> list[Signal]:
        if df is None or len(df) < 30:
            return []
        signals: list[Signal] = []
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        price = latest["close"]

        buy_factors: list[tuple[str, str]] = []
        h = self._factor_wave_rebound(df, latest)
        if h:
            buy_factors.append(h)
        h = self._factor_trend_confirm(df, latest, prev)
        if h:
            buy_factors.append(h)
        h = self._factor_breakout(df, latest)
        if h:
            buy_factors.append(h)
        h = self._factor_oversold(df, latest)
        if h:
            buy_factors.append(h)
        if buy_factors:
            n = len(buy_factors)
            strength = (
                SignalStrength.STRONG if n >= 3
                else (SignalStrength.MEDIUM if n == 2 else SignalStrength.WEAK)
            )
            reasons = "；".join(r for _, r in buy_factors)
            factor_names = [f for f, _ in buy_factors]
            signals.append(Signal(
                symbol=symbol,
                signal_type=SignalType.BUY,
                strength=strength,
                name=f"BUY({n}因子共振)",
                reason=f"共振因子: {', '.join(factor_names)}。{reasons}",
                trigger_price=price,
                target_price=price * 1.03,
                stop_loss=price * 0.97,
                position_pct=0.33,
                factor_hits=factor_names,
            ))

        for s in self._signals_sell(df, latest, prev, price, symbol):
            signals.append(s)

        if self._check_stop_loss_strict(df, latest):
            signals.append(Signal(
                symbol=symbol,
                signal_type=SignalType.STOP_LOSS,
                strength=SignalStrength.STRONG,
                name="跌破MA20(连续3日)",
                reason="连续 3 日收盘低于 MA20，趋势走弱",
                trigger_price=price,
                stop_loss=price * 0.95,
                position_pct=0.5,
            ))
        return signals

    def _factor_wave_rebound(
        self, df: pd.DataFrame, latest: pd.Series
    ) -> Optional[tuple[str, str]]:
        if len(df) < 60:
            return None
        close_60d_ago = df["close"].iloc[-61] if len(df) >= 61 else df["close"].iloc[0]
        drop_pct = (latest["close"] - close_60d_ago) / close_60d_ago * 100
        if drop_pct > -15:
            return None
        recent3_vol = df["volume"].iloc[-3:].mean()
        vol_ma5 = latest.get("VOL_MA5", recent3_vol)
        if recent3_vol >= vol_ma5:
            return None
        rsi_val = latest.get("RSI6", 50)
        if not (30 <= rsi_val <= 50):
            return None
        return ("波段反弹",
                f"60日跌幅 {drop_pct:.1f}%，3日均量 {recent3_vol:.0f} < VOL_MA5 {vol_ma5:.0f}，RSI6={rsi_val:.1f}")

    def _factor_trend_confirm(
        self, df: pd.DataFrame, latest: pd.Series, prev: pd.Series
    ) -> Optional[tuple[str, str]]:
        ma5 = latest.get("MA5")
        ma10 = latest.get("MA10")
        prev_ma5 = prev.get("MA5")
        prev_ma10 = prev.get("MA10")
        if any(pd.isna([ma5, ma10, prev_ma5, prev_ma10])):
            return None
        if not (prev_ma5 <= prev_ma10 and ma5 > ma10):
            return None
        vol_ma5 = latest.get("VOL_MA5", 0)
        if vol_ma5 <= 0:
            return None
        vol_ratio = latest["volume"] / vol_ma5
        if vol_ratio < 1.2:
            return None
        return ("趋势确认",
                f"MA5({ma5:.2f}) 上穿 MA10({ma10:.2f})，量比 {vol_ratio:.2f}")

    def _factor_breakout(
        self, df: pd.DataFrame, latest: pd.Series
    ) -> Optional[tuple[str, str]]:
        high_60 = df["high"].iloc[-60:].max()
        if latest["close"] < high_60 * 0.995:
            return None
        vol_ma5 = latest.get("VOL_MA5", 0)
        if vol_ma5 <= 0:
            return None
        vol_ratio = latest["volume"] / vol_ma5
        if vol_ratio < 1.5:
            return None
        return ("放量突破", f"突破 60 日新高 {high_60:.2f}，量比 {vol_ratio:.2f}")

    def _factor_oversold(
        self, df: pd.DataFrame, latest: pd.Series
    ) -> Optional[tuple[str, str]]:
        rsi_val = latest.get("RSI6", 50)
        close = latest["close"]
        boll_lower = latest.get("BOLL_LOWER", close)
        if rsi_val >= 25 or close > boll_lower * 1.02:
            return None
        vol_ma5 = latest.get("VOL_MA5", 0)
        if vol_ma5 <= 0:
            return None
        vol_ratio = latest["volume"] / vol_ma5
        if vol_ratio < 1.3:
            return None
        return ("超卖反弹",
                f"RSI6={rsi_val:.1f}, 触及 BOLL 下轨 {boll_lower:.2f}, 量比 {vol_ratio:.2f}")

    def _signals_sell(
        self, df: pd.DataFrame, latest: pd.Series,
        prev: pd.Series, price: float, symbol: str
    ) -> list[Signal]:
        out: list[Signal] = []
        vol_ma5 = latest.get("VOL_MA5", 0)
        if vol_ma5 > 0:
            vol_ratio = latest["volume"] / vol_ma5
            pct_chg = (latest["close"] - prev["close"]) / prev["close"] * 100
            if vol_ratio > 3 and pct_chg > 5:
                out.append(Signal(
                    symbol=symbol, signal_type=SignalType.SELL,
                    strength=SignalStrength.STRONG,
                    name="放量拉升",
                    reason=f"量比 {vol_ratio:.2f}, 涨幅 {pct_chg:.1f}%",
                    trigger_price=price,
                    target_price=price * 0.97, stop_loss=price * 1.03,
                    position_pct=0.33,
                ))
        if len(df) >= 30:
            recent = df.tail(20)
            price_recent_high = recent["high"].max()
            macd_recent_high = recent["MACD_HIST"].max()
            if latest["close"] >= price_recent_high * 0.98:
                latest_hist = latest["MACD_HIST"]
                if (latest_hist < macd_recent_high * 0.5
                        and latest_hist < 0):
                    out.append(Signal(
                        symbol=symbol, signal_type=SignalType.SELL,
                        strength=SignalStrength.STRONG,
                        name="MACD 顶背离",
                        reason=f"价格近 20 日新高 {price_recent_high:.2f}，MACD HIST 背离 ({latest_hist:.2f} vs {macd_recent_high:.2f})",
                        trigger_price=price,
                        target_price=price * 0.95, stop_loss=price * 1.03,
                        position_pct=0.5,
                    ))
        return out

    def _check_stop_loss_strict(self, df: pd.DataFrame,
                                latest: pd.Series) -> bool:
        if len(df) < 3:
            return False
        ma20 = latest.get("MA20")
        if pd.isna(ma20):
            return False
        if latest["close"] >= ma20 * 0.98:
            return False
        for i in (-3, -2):
            r = df.iloc[i]
            r_ma20 = r.get("MA20")
            if pd.isna(r_ma20) or r["close"] >= r_ma20 * 0.98:
                return False
        return True


# ============================================================
# 当日 T 仓状态
# ============================================================
T_STATE_FILE = paths.data_path("t_state.json")
_T_FILE_LOCK = threading.RLock()


@dataclass
class TState:
    symbol: str
    trade_date: str = ""
    status: str = "empty"   # empty / holding / locked
    entry_price: float = 0.0
    entry_time: str = ""
    peak_price: float = 0.0
    lots: float = 0.0
    entry_signal: str = ""


class TStateStore:
    """按股票保存当日 T 仓状态（跨天自动清空）。"""

    def __init__(
        self,
        path: Optional[str] = None,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.path = path or T_STATE_FILE
        self.now = now

    def get(self, symbol: str) -> TState:
        normalized = str(symbol).zfill(6)
        today = self.now().strftime("%Y-%m-%d")
        with _T_FILE_LOCK:
            payload = self._read()
        if payload.get("trade_date") != today:
            return TState(symbol=normalized, trade_date=today)
        raw = payload.get("states", {}).get(normalized)
        return self._from_dict(normalized, today, raw)

    def set(self, symbol: str, state: TState) -> None:
        normalized = str(symbol).zfill(6)
        if state.status not in {"empty", "holding", "locked"}:
            raise ValueError(f"无效 status: {state.status}")
        normalized_state = replace(state, symbol=normalized)
        with _T_FILE_LOCK:
            payload = self._read()
            if payload.get("trade_date") != normalized_state.trade_date:
                payload = {"trade_date": normalized_state.trade_date, "states": {}}
            raw = asdict(normalized_state)
            raw.pop("symbol")
            payload["states"][normalized] = raw
            self._write(payload)

    def mark_buy(
        self,
        symbol: str,
        entry_price: float,
        lots: float,
        entry_signal: str,
        timestamp: Optional[str] = None,
    ) -> TState:
        if entry_price <= 0 or lots <= 0:
            raise ValueError("entry_price/lots 必须 > 0")
        entry_time = timestamp or self.now().isoformat(timespec="seconds")
        try:
            trade_date = datetime.fromisoformat(entry_time).strftime("%Y-%m-%d")
        except ValueError as error:
            raise ValueError(f"timestamp 格式无效: {entry_time}") from error
        state = TState(
            symbol=str(symbol).zfill(6),
            trade_date=trade_date,
            status="holding",
            entry_price=float(entry_price),
            entry_time=entry_time,
            peak_price=float(entry_price),
            lots=float(lots),
            entry_signal=str(entry_signal),
        )
        self.set(state.symbol, state)
        return state

    def update_peak(self, symbol: str, current_price: float) -> TState:
        state = self.get(symbol)
        if state.status != "holding" or current_price <= state.peak_price:
            return state
        state.peak_price = float(current_price)
        self.set(symbol, state)
        return state

    def mark_exit(self, symbol: str, status: str = "empty") -> TState:
        if status not in {"empty", "locked"}:
            raise ValueError(f"退出 status 只能为 empty 或 locked，实际: {status}")
        state = self.get(symbol)
        state.status = status
        state.lots = 0.0
        self.set(symbol, state)
        return state

    def reset_day(self, date: Optional[str] = None) -> None:
        trade_date = date or self.now().strftime("%Y-%m-%d")
        with _T_FILE_LOCK:
            self._write({"trade_date": trade_date, "states": {}})

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and isinstance(payload.get("states"), dict):
                return payload
        except Exception:
            pass
        return {"trade_date": "", "states": {}}

    def _write(self, payload: dict) -> None:
        paths.ensure_data_dir()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os_replace(tmp, self.path)

    @staticmethod
    def _from_dict(symbol: str, trade_date: str, raw: object) -> TState:
        if not isinstance(raw, dict):
            return TState(symbol=symbol, trade_date=trade_date)
        try:
            status = str(raw.get("status", "empty"))
            if status not in {"empty", "holding", "locked"}:
                raise ValueError("bad status")
            return TState(
                symbol=symbol,
                trade_date=trade_date,
                status=status,
                entry_price=float(raw.get("entry_price", 0.0)),
                entry_time=str(raw.get("entry_time", "")),
                peak_price=float(raw.get("peak_price", 0.0)),
                lots=float(raw.get("lots", 0.0)),
                entry_signal=str(raw.get("entry_signal", "")),
            )
        except (TypeError, ValueError):
            return TState(symbol=symbol, trade_date=trade_date)


def os_replace(src: str, dst: str) -> None:
    import os
    os.replace(src, dst)


# ============================================================
# T 仓锁利 / 止损（参考 a-trade t_trailing）
# ============================================================
DEFAULT_TAKE_PROFIT_PCT = 0.03
DEFAULT_STOP_LOSS_PCT = 0.02
DEFAULT_EXIT_LOTS = 1.0


@dataclass(frozen=True)
class TrailingConfig:
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    exit_lots: float = DEFAULT_EXIT_LOTS

    @classmethod
    def from_dict(
        cls,
        defaults: Optional[dict] = None,
        override: Optional[dict] = None,
        exit_lots: float = DEFAULT_EXIT_LOTS,
    ) -> "TrailingConfig":
        default_values = defaults or {}
        override_values = override or {}
        take = _resolve_percentage(
            "take_profit_pct", default_values, override_values,
            DEFAULT_TAKE_PROFIT_PCT,
        )
        stop = _resolve_percentage(
            "stop_loss_pct", default_values, override_values,
            DEFAULT_STOP_LOSS_PCT,
        )
        lots = _positive_number(exit_lots, "exit_lots")
        return cls(take_profit_pct=take, stop_loss_pct=stop, exit_lots=lots)


@dataclass(frozen=True)
class TrailingAction:
    action: str
    signal_type: str
    price: float
    lots: float
    reason: str
    gain_pct: float


def check_trailing(
    state: TState,
    current_price: float,
    config: TrailingConfig,
) -> Optional[TrailingAction]:
    if state.status != "holding" or state.entry_price <= 0 or state.lots <= 0:
        return None
    if current_price <= 0 or not math.isfinite(current_price):
        return None
    gain_pct = current_price / state.entry_price - 1.0
    lots = min(float(state.lots), float(config.exit_lots))
    if gain_pct >= config.take_profit_pct:
        return TrailingAction(
            action="take_profit",
            signal_type="sell",
            price=float(current_price),
            lots=lots,
            reason=f"T 仓收益 {gain_pct:+.2%}，达到 +{config.take_profit_pct:.2%} 锁利线",
            gain_pct=gain_pct,
        )
    if gain_pct <= -config.stop_loss_pct:
        return TrailingAction(
            action="stop_loss",
            signal_type="stop_loss",
            price=float(current_price),
            lots=lots,
            reason=f"T 仓收益 {gain_pct:+.2%}，达到 -{config.stop_loss_pct:.2%} 止损线",
            gain_pct=gain_pct,
        )
    return None


def _resolve_percentage(
    field: str,
    defaults: dict,
    override: dict,
    fallback: float,
) -> float:
    value = override.get(field)
    if value in {None, ""}:
        value = defaults.get(field)
    if value in {None, ""}:
        value = fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须为 0 和 1 之间的数字")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 < normalized < 1:
        raise ValueError(f"{field} 必须在 0 和 1 之间")
    return normalized


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须为正数")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field} 必须为正数")
    return normalized


# ============================================================
# HKS 数据适配：内部 K 线 dict → 信号引擎 DataFrame
# ============================================================
def rows_to_frame(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"vol": "volume"})
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def scan_rows(symbol: str, rows: list) -> tuple[list[Signal], pd.DataFrame]:
    df = rows_to_frame(rows)
    if df.empty or len(df) < 30:
        return [], df
    df_ind = add_all_indicators(df).reset_index(drop=True)
    engine = SignalEngine()
    return engine.scan(symbol, df_ind), df_ind
