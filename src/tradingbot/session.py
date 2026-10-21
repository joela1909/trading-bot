"""Handelszeiten fuer den Tageshandel: wann Einstiege erlaubt sind und wann alles geschlossen wird.

Bot und Backtester nutzen dieselben Regeln, damit Test und Realitaet nicht auseinanderlaufen."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import DaytradingConfig


class Session:
    def __init__(self, cfg: DaytradingConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.entry_start = cfg.entry_start_min
        self.entry_end = cfg.entry_end_min
        self.flat = cfg.flat_min

    # ------------------------------------------------------------ einzelne Zeitpunkte (Bot)
    def local(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self.tz)

    def day(self, dt: datetime) -> str:
        return self.local(dt).strftime("%Y-%m-%d")

    def minute_of_day(self, dt: datetime) -> int:
        loc = self.local(dt)
        return loc.hour * 60 + loc.minute

    def entry_allowed(self, dt: datetime) -> bool:
        m = self.minute_of_day(dt)
        return self.entry_start <= m < self.entry_end

    def must_close(self, now: datetime, opened_at: datetime) -> str | None:
        """Grund, warum eine Position jetzt geschlossen werden muss, sonst None."""
        if self.day(now) != self.day(opened_at):
            return "session_end"
        if self.minute_of_day(now) >= self.flat:
            return "session_end"
        if self.cfg.max_hold_minutes > 0 and (now - opened_at).total_seconds() >= self.cfg.max_hold_minutes * 60:
            return "max_hold"
        return None

    # ------------------------------------------------------------ vektorisiert (Backtest)
    def bar_times(self, ts_ms: np.ndarray, tf_ms: int) -> tuple[np.ndarray, np.ndarray]:
        """Lokale Minute des Tages und lokaler Tag (als Zahl) zum Schlusszeitpunkt jeder Kerze.
        Entscheidungen fallen beim Kerzenschluss, deshalb zaehlt dieser Zeitpunkt."""
        close_ts = pd.to_datetime(np.asarray(ts_ms, dtype="int64") + tf_ms, unit="ms", utc=True).tz_convert(self.tz)
        minutes = (close_ts.hour * 60 + close_ts.minute).to_numpy()
        days = (close_ts.year * 10000 + close_ts.month * 100 + close_ts.day).to_numpy()
        return minutes, days


def session_day_key(ts_ms: pd.Series, tz: str) -> pd.Series:
    """Lokaler Kalendertag je Kerze (nach Eroeffnungszeit), z. B. fuer VWAP-Resets."""
    local = pd.to_datetime(ts_ms.astype("int64"), unit="ms", utc=True).dt.tz_convert(tz)
    return local.dt.strftime("%Y-%m-%d")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
