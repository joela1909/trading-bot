from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

import pandas as pd

from .models import MarketLimits

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

_TF_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(tf: str) -> int:
    unit = tf[-1]
    if unit not in _TF_SECONDS:
        raise ValueError(f"Unbekannter Zeitrahmen: {tf}")
    return int(tf[:-1]) * _TF_SECONDS[unit]


def ohlcv_to_df(rows: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    df["timestamp"] = df["timestamp"].astype("int64")
    for c in OHLCV_COLUMNS[1:]:
        df[c] = df[c].astype(float)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def drop_incomplete_candle(df: pd.DataFrame, timeframe: str, now_ms: int | None = None) -> pd.DataFrame:
    """Entfernt die letzte Kerze, wenn sie noch nicht abgeschlossen ist."""
    if df.empty:
        return df
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    last_open = int(df["timestamp"].iloc[-1])
    if last_open + timeframe_seconds(timeframe) * 1000 > now_ms:
        return df.iloc[:-1].reset_index(drop=True)
    return df


class DataFeed(Protocol):
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame: ...
    def fetch_price(self, symbol: str) -> float: ...
    def market_limits(self, symbol: str) -> MarketLimits: ...


class CcxtDataFeed:
    """Oeffentliche Marktdaten ueber ccxt (ohne API-Key nutzbar)."""

    def __init__(self, exchange_id: str, ccxt_exchange=None, market_type: str = "spot"):
        if ccxt_exchange is None:
            import ccxt

            cls = getattr(ccxt, exchange_id)
            params: dict = {"enableRateLimit": True}
            if market_type == "futures":
                params["options"] = {"defaultType": "swap"}
            ccxt_exchange = cls(params)
        self.exchange = ccxt_exchange
        self._markets_loaded = False

    def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            self.exchange.load_markets()
            self._markets_loaded = True

    MAX_PER_REQUEST = 1000

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        if limit > self.MAX_PER_REQUEST:
            since_ms = int(time.time() * 1000) - (limit + 2) * timeframe_seconds(timeframe) * 1000
            return self.fetch_ohlcv_range(symbol, timeframe, since_ms).tail(limit).reset_index(drop=True)
        rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = ohlcv_to_df(rows)
        return drop_incomplete_candle(df, timeframe)

    def fetch_chart(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Fuer den Chart: die letzten Kerzen inklusive der noch laufenden."""
        rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=min(limit, self.MAX_PER_REQUEST))
        return ohlcv_to_df(rows)

    def fetch_ohlcv_range(self, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None) -> pd.DataFrame:
        until_ms = until_ms or int(time.time() * 1000)
        step_ms = timeframe_seconds(timeframe) * 1000
        all_rows: list[list[float]] = []
        cursor = since_ms
        while cursor < until_ms:
            rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
            if not rows:
                break
            all_rows.extend(rows)
            next_cursor = rows[-1][0] + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            log.debug("%s: %d Kerzen geladen", symbol, len(all_rows))
        df = ohlcv_to_df(all_rows)
        df = df[df["timestamp"] < until_ms].reset_index(drop=True)
        return drop_incomplete_candle(df, timeframe)

    def fetch_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            raise RuntimeError(f"Kein Kurs fuer {symbol} erhalten")
        return float(price)

    def market_limits(self, symbol: str) -> MarketLimits:
        self._ensure_markets()
        market = self.exchange.market(symbol)
        limits = market.get("limits", {}) or {}
        precision = market.get("precision", {}) or {}
        amount_prec = precision.get("amount")
        if amount_prec is None:
            amount_prec = 8
        elif isinstance(amount_prec, float) and amount_prec < 1:
            amount_prec = max(0, round(-1 * __import__("math").log10(amount_prec)))
        return MarketLimits(
            min_amount=float((limits.get("amount") or {}).get("min") or 0.0),
            min_cost=float((limits.get("cost") or {}).get("min") or 0.0),
            amount_precision=int(amount_prec),
        )


def load_csv(path: str | Path) -> pd.DataFrame:
    """CSV mit Spalten timestamp,open,high,low,close,volume. timestamp als ms oder ISO-Datum."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV fehlen Spalten: {missing}")
    if not pd.api.types.is_numeric_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).map(lambda t: int(t.timestamp() * 1000))
    return ohlcv_to_df(df[OHLCV_COLUMNS].values.tolist())


def load_history(feed: "CcxtDataFeed", symbol: str, timeframe: str, since: str, until: str | None, cache_dir: str | Path) -> pd.DataFrame:
    """Laedt historische Kerzen von der Boerse und legt sie als CSV im Zwischenspeicher ab."""
    from datetime import datetime, timezone

    until_label = until or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe = symbol.replace("/", "_").replace(":", "-")
    path = Path(cache_dir) / f"{safe}_{timeframe}_{since}_{until_label}.csv"
    if path.exists():
        log.info("%s: aus Zwischenspeicher %s", symbol, path)
        return load_csv(path)

    def to_ms(day: str) -> int:
        return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)

    log.info("%s: lade %s ab %s von der Boerse ...", symbol, timeframe, since)
    df = feed.fetch_ohlcv_range(symbol, timeframe, to_ms(since), to_ms(until) if until else None)
    if df.empty:
        raise RuntimeError(f"{symbol}: keine Kerzen erhalten")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df
