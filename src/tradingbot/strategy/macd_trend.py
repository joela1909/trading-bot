from __future__ import annotations

import pandas as pd

from ..indicators import ema, macd
from .base import Strategy, WARMUP_FACTOR, cross_down, cross_up


class MacdTrendStrategy(Strategy):
    """Trendfolge: Kauf, wenn die MACD-Linie die Signallinie von unten kreuzt (optional nur oberhalb
    einer langen EMA), Verkauf beim Gegenkreuz."""

    name = "macd_trend"
    buy_reason = "MACD kreuzt nach oben"
    sell_reason = "MACD kreuzt nach unten"
    param_grid = {"macd_fast": [8, 12], "macd_slow": [21, 26], "macd_signal": [9], "trend_ema": [0, 200]}

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return params.get("macd_slow", 26) > params.get("macd_fast", 12)

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(self.p("macd_slow", 26) + self.p("macd_signal", 9), self.p("trend_ema", 0), self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        line, signal = macd(df["close"], self.p("macd_fast", 12), self.p("macd_slow", 26), self.p("macd_signal", 9))
        df["macd"] = line
        df["macd_signal"] = signal
        trend = self.p("trend_ema", 0)
        if trend > 0:
            df["ema_trend"] = ema(df["close"], trend)

    def signals(self, df):
        buy = cross_up(df["macd"], df["macd_signal"]) & (df["atr"] > 0).to_numpy()
        if self.p("trend_ema", 0) > 0:
            buy = buy & (df["close"] > df["ema_trend"]).to_numpy()
        return buy, cross_down(df["macd"], df["macd_signal"])
