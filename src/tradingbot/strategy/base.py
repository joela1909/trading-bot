from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..config import StrategyConfig
from ..models import Decision


class Strategy(ABC):
    name: str = "base"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Anzahl Kerzen, die fuer stabile Indikatorwerte noetig sind."""

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fuegt Indikator-Spalten hinzu. Gibt eine Kopie zurueck."""

    @abstractmethod
    def decide(self, df: pd.DataFrame, i: int = -1) -> Decision:
        """Entscheidung fuer Kerze i (Standard: letzte Kerze) auf einem vorbereiteten DataFrame."""

    def trailing_stop(self, highest_price: float, atr_value: float) -> float:
        return highest_price - self.cfg.stop_atr_mult * atr_value
