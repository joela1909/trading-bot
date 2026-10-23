from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
from ..data import timeframe_seconds
from ..indicators import atr
from ..models import Decision, Signal

# Geglaettete Indikatoren (EMA, RSI, ATR) schwingen erst nach vielen Kerzen auf ihren Endwert ein. Mit diesem
# Faktor kommen Live-Bot (wenige Kerzen) und Backtest (ganze Historie) auf praktisch dieselben Signale.
WARMUP_FACTOR = 8

EXIT_GRID: dict[str, list] = {
    "stop_atr_mult": [2.0, 3.0],
    "take_profit_atr_mult": [0.0, 4.0],
}


class Strategy(ABC):
    """Eine Strategie liefert vektorisierte Kauf- und Verkaufssignale.

    Backtester und Live-Bot nutzen dieselben Signale, damit Test und Realitaet nicht auseinanderlaufen.
    Stop-Loss, Take-Profit und Trailing-Stop sind fuer alle Strategien gleich und ATR-basiert.
    """

    name: str = "base"
    buy_reason: str = "Kaufsignal"
    sell_reason: str = "Verkaufssignal"
    short_reason: str = "Short-Signal"
    cover_reason: str = "Short-Ausstiegssignal"
    param_grid: dict[str, list] = {}
    # Strategien mit eigenen Stop- und Zielkursen je Trade (statt ATR-Vielfachen aus der Konfiguration):
    entry_on_next_open = False  # Signal bei Kerzenschluss, Einstieg zur Eroeffnung der naechsten Kerze
    fixed_levels = False  # entry_levels() liefert absolute Kurse, der Trailing-Stop ist aus
    levels_from_fill = False  # Abstaende vom Fill-Kurs statt absolute Kurse der Signalkerze verwenden
    use_exit_grid = True  # False: die Stop-/Ziel-Parameter der Strategie ersetzen EXIT_GRID im Vergleich
    exit_param_keys: tuple = tuple(EXIT_GRID)  # Parameter, die nur die Stop-/Zielkurse beeinflussen, nicht die Signale

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None, timeframe: str | None = None):
        self.cfg = cfg
        # Intraday-Strategien brauchen Zeitzone und Handelsbeginn. Ohne Tageshandel: UTC, Tag ab 00:00.
        self.session = session or DaytradingConfig(timezone="UTC", entry_start="00:00", entry_end="23:59", flat_time="24:00")
        self.timeframe_ms: int | None = timeframe_seconds(timeframe) * 1000 if timeframe else None

    @property
    def bars_per_day(self) -> int | None:
        return 86_400_000 // self.timeframe_ms if self.timeframe_ms else None

    def p(self, key: str, default: Any) -> Any:
        value = getattr(self.cfg, key, None)
        return default if value is None else value

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return True

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Anzahl Kerzen, die fuer stabile Indikatorwerte noetig sind."""

    @abstractmethod
    def _add_indicators(self, df: pd.DataFrame) -> None:
        """Fuegt strategiespezifische Indikator-Spalten in-place hinzu."""

    @abstractmethod
    def signals(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Gibt (kauf, verkauf) als Bool-Arrays zurueck. Auf einem vorbereiteten DataFrame."""

    def short_signals(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Gibt (short_einstieg, short_ausstieg) als Bool-Arrays zurueck. Standard: keine Shorts."""
        z = np.zeros(len(df), dtype=bool)
        return z, z.copy()

    def all_signals(self, df: pd.DataFrame) -> tuple:
        """(kauf, verkauf, short, cover). short und cover sind None, wenn Shorts aus sind."""
        buy, sell = self.signals(df)
        if not self.cfg.allow_short:
            return buy, sell, None, None
        short, cover = self.short_signals(df)
        return buy, sell, short, cover

    def entry_levels(self, df: pd.DataFrame) -> tuple | None:
        """Optional: absolute (long_stop, long_ziel, short_stop, short_ziel) je Signalkerze, NaN = kein Ziel.
        Bezug ist der Schlusskurs der Signalkerze. Nur fuer Strategien mit fixed_levels."""
        return None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["atr"] = atr(out, self.cfg.atr_period)
        self._add_indicators(out)
        return out

    def decide(self, df: pd.DataFrame, i: int = -1, context: dict | None = None) -> Decision:
        n = len(df)
        if n < 2:
            return Decision(Signal.HOLD, "zu wenig Daten")
        if i < 0:
            i = n + i
        if i < 1:
            return Decision(Signal.HOLD, "zu wenig Daten")
        atr_value = float(df["atr"].iloc[i])
        if np.isnan(atr_value):
            return Decision(Signal.HOLD, "Indikatoren noch im Warmup")
        close = float(df["close"].iloc[i])
        position = (context or {}).get("position")
        side = position.get("side", "long") if position else None

        if side == "short":
            _, cover = self.short_signals(df)
            if cover[i]:
                return Decision(Signal.COVER, self.cover_reason, atr=atr_value)
            return Decision(Signal.HOLD, "kein Signal", atr=atr_value)

        buy, sell = self.signals(df)
        flat_with_shorts = context is not None and side is None and self.cfg.allow_short
        if sell[i] and not flat_with_shorts:
            return Decision(Signal.SELL, self.sell_reason, atr=atr_value)
        if buy[i]:
            return self._entry(Signal.BUY, self.buy_reason, close, atr_value)
        if flat_with_shorts:
            short, _ = self.short_signals(df)
            if short[i]:
                return self._entry(Signal.SHORT, self.short_reason, close, atr_value)
        return Decision(Signal.HOLD, "kein Signal", atr=atr_value)

    def _entry(self, signal: Signal, reason: str, close: float, atr_value: float) -> Decision:
        if atr_value <= 0:
            return Decision(Signal.HOLD, "ATR ist null, kein sinnvoller Stop", atr=atr_value)
        sign = -1 if signal == Signal.SHORT else 1
        stop = close - sign * self.cfg.stop_atr_mult * atr_value
        tp_mult = self.cfg.take_profit_atr_mult
        tp = close + sign * tp_mult * atr_value if tp_mult > 0 else None
        return Decision(signal, reason, stop_loss=stop, take_profit=tp, atr=atr_value)

    def export_state(self) -> dict:
        """Strategie-Zustand, der mit dem Bot gespeichert wird (z. B. Zaehler der KI-Aufrufe)."""
        return {}

    def import_state(self, state: dict) -> None:
        return None

    def trailing_stop(self, highest_price: float, atr_value: float) -> float:
        return highest_price - self.cfg.stop_atr_mult * atr_value

    def trailing_stop_short(self, lowest_price: float, atr_value: float) -> float:
        return lowest_price + self.cfg.stop_atr_mult * atr_value


def cross_up(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a.shift(1) <= b.shift(1)) & (a > b)).to_numpy()


def cross_down(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a.shift(1) >= b.shift(1)) & (a < b)).to_numpy()
