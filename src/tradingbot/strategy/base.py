from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
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
    param_grid: dict[str, list] = {}

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None):
        self.cfg = cfg
        # Intraday-Strategien brauchen Zeitzone und Handelsbeginn. Ohne Tageshandel: UTC, Tag ab 00:00.
        self.session = session or DaytradingConfig(timezone="UTC", entry_start="00:00", entry_end="23:59", flat_time="24:00")

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
        buy, sell = self.signals(df)
        close = float(df["close"].iloc[i])
        if sell[i]:
            return Decision(Signal.SELL, self.sell_reason, atr=atr_value)
        if buy[i]:
            if atr_value <= 0:
                return Decision(Signal.HOLD, "ATR ist null, kein sinnvoller Stop", atr=atr_value)
            stop = close - self.cfg.stop_atr_mult * atr_value
            tp = close + self.cfg.take_profit_atr_mult * atr_value if self.cfg.take_profit_atr_mult > 0 else None
            return Decision(Signal.BUY, self.buy_reason, stop_loss=stop, take_profit=tp, atr=atr_value)
        return Decision(Signal.HOLD, "kein Signal", atr=atr_value)

    def export_state(self) -> dict:
        """Strategie-Zustand, der mit dem Bot gespeichert wird (z. B. Zaehler der KI-Aufrufe)."""
        return {}

    def import_state(self, state: dict) -> None:
        return None

    def trailing_stop(self, highest_price: float, atr_value: float) -> float:
        return highest_price - self.cfg.stop_atr_mult * atr_value


def cross_up(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a.shift(1) <= b.shift(1)) & (a > b)).to_numpy()


def cross_down(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a.shift(1) >= b.shift(1)) & (a < b)).to_numpy()
