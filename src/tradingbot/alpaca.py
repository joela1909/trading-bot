"""US-Aktienkurse von Alpaca (https://alpaca.markets) fuer den Aktienhandel im Papiermodus.

Ein kostenloses Alpaca-Papierkonto (nur E-Mail, weltweit) liefert Echtzeit-Kurse der Boerse IEX. Der Bot handelt
damit wie bei Krypto auf einem eigenen Papierkonto; Orders gehen nicht an Alpaca. Schluessel in .env bzw. als
Cloudflare-Secret: ALPACA_API_KEY und ALPACA_API_SECRET."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from .data import drop_incomplete_candle, ohlcv_to_df, timeframe_seconds
from .models import MarketLimits

log = logging.getLogger(__name__)

DATA_URL = "https://data.alpaca.markets/v2"
TIMEFRAMES = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "4h": "4Hour", "1d": "1Day"}
TRADING_SECONDS_PER_DAY = 16 * 3600  # IEX-Kerzen gibt es etwa von 4:00 bis 20:00 New York


class AlpacaError(RuntimeError):
    pass


class AlpacaDataFeed:
    """Kerzen und letzter Kurs fuer US-Aktien. Gleiche Schnittstelle wie CcxtDataFeed."""

    def __init__(self, api_key: str, api_secret: str, feed: str = "iex", session: requests.Session | None = None, timeout: float = 15.0):
        if not api_key or not api_secret:
            raise AlpacaError("Alpaca-Schluessel fehlen (ALPACA_API_KEY und ALPACA_API_SECRET)")
        self.feed = feed
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret, "Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.http.get(f"{DATA_URL}{path}", params=params or {}, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise AlpacaError("Alpaca lehnt die Schluessel ab (401/403). Bitte ALPACA_API_KEY und ALPACA_API_SECRET pruefen.")
        if r.status_code == 429:
            raise AlpacaError("Alpaca: zu viele Anfragen, kurz warten")
        if r.status_code >= 400:
            raise AlpacaError(f"Alpaca antwortet mit {r.status_code}: {r.text[:200]}")
        return r.json()

    def _bars(self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None) -> pd.DataFrame:
        if timeframe not in TIMEFRAMES:
            raise AlpacaError(f"Zeitrahmen {timeframe} gibt es bei Alpaca hier nicht")
        params = {
            "timeframe": TIMEFRAMES[timeframe], "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
            "feed": self.feed, "adjustment": "split", "sort": "asc",
        }
        if end is not None:
            params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows: list[list[float]] = []
        while True:
            data = self._get(f"/stocks/{symbol}/bars", params)
            for b in data.get("bars") or []:
                ts = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp() * 1000)
                rows.append([ts, b["o"], b["h"], b["l"], b["c"], b.get("v", 0.0)])
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return ohlcv_to_df(rows)

    @staticmethod
    def _lookback(timeframe: str, limit: int) -> timedelta:
        """Wie weit zurueck, damit `limit` Kerzen zusammenkommen: Nacht, Wochenende und Feiertage eingerechnet."""
        tf_s = timeframe_seconds(timeframe)
        if tf_s >= 86400:
            trading_days = limit
        else:
            trading_days = math.ceil(limit * tf_s / TRADING_SECONDS_PER_DAY)
        return timedelta(days=math.ceil(trading_days * 7 / 5) + 4)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - self._lookback(timeframe, limit)
        df = self._bars(symbol, timeframe, start)
        return drop_incomplete_candle(df.tail(limit + 1).reset_index(drop=True), timeframe).tail(limit).reset_index(drop=True)

    def fetch_chart(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - self._lookback(timeframe, limit)
        return self._bars(symbol, timeframe, start).tail(limit).reset_index(drop=True)

    def fetch_ohlcv_range(self, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None) -> pd.DataFrame:
        start = datetime.fromtimestamp(since_ms / 1000, timezone.utc)
        end = datetime.fromtimestamp(until_ms / 1000, timezone.utc) if until_ms else None
        return self._bars(symbol, timeframe, start, end)

    def fetch_price(self, symbol: str) -> float:
        data = self._get(f"/stocks/{symbol}/trades/latest", {"feed": self.feed})
        trade = data.get("trade") or {}
        if "p" not in trade:
            raise AlpacaError(f"Kein Kurs fuer {symbol}")
        return float(trade["p"])

    def market_limits(self, symbol: str) -> MarketLimits:
        # ganze Stueck: Leerverkauf geht bei Alpaca nur mit ganzen Aktien
        return MarketLimits(min_amount=1.0, min_cost=1.0, amount_precision=0)

    def check(self) -> None:
        """Wirft AlpacaError, wenn die Schluessel nicht passen."""
        self._get("/stocks/SPY/trades/latest", {"feed": self.feed})


def us_market_open(now: datetime | None = None) -> bool:
    """Regulaere Handelszeit der US-Boersen (Mo-Fr 9:30-16:00 New York). Feiertage kennt diese Pruefung nicht."""
    from zoneinfo import ZoneInfo

    ny = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    minutes = ny.hour * 60 + ny.minute
    return ny.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60

