from __future__ import annotations

import pandas as pd

from ..indicators import ema, rsi
from .base import Strategy, WARMUP_FACTOR, cross_down, cross_up


class EmaCrossStrategy(Strategy):
    """Trendfolge: Kauf beim Kreuzen der schnellen ueber die langsame EMA (mit RSI-Filter),
    Verkauf beim Gegenkreuz."""

    name = "ema_cross"
    buy_reason = "EMA-Kreuz nach oben"
    sell_reason = "EMA-Kreuz nach unten"
    param_grid = {"fast_ema": [10, 20, 30], "slow_ema": [50, 100, 200]}

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return params.get("slow_ema", 50) > params.get("fast_ema", 20)

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(self.cfg.slow_ema, self.cfg.rsi_period, self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        df["ema_fast"] = ema(df["close"], self.cfg.fast_ema)
        df["ema_slow"] = ema(df["close"], self.cfg.slow_ema)
        df["rsi"] = rsi(df["close"], self.cfg.rsi_period)

    def signals(self, df):
        up = cross_up(df["ema_fast"], df["ema_slow"])
        down = cross_down(df["ema_fast"], df["ema_slow"])
        buy = up & (df["rsi"] < self.cfg.rsi_max_entry).to_numpy() & (df["atr"] > 0).to_numpy()
        return buy, down
