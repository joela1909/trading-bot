from __future__ import annotations

import pandas as pd

from ..indicators import ema, rsi
from .base import Strategy, WARMUP_FACTOR


class RsiReversionStrategy(Strategy):
    """Rueckkehr zum Mittelwert: Kauf bei stark ueberverkauftem RSI (optional nur im Aufwaertstrend),
    Verkauf, sobald der RSI wieder erholt ist."""

    name = "rsi_reversion"
    buy_reason = "RSI ueberverkauft"
    sell_reason = "RSI erholt"
    short_reason = "RSI ueberkauft"
    cover_reason = "RSI abgekuehlt"
    param_grid = {"rsi_buy": [25, 30, 35], "rsi_sell": [50, 60, 70], "trend_ema": [0, 200]}
    chart_lines = [("ema_trend", "Trend-EMA", "trend")]

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(self.cfg.rsi_period, self.p("trend_ema", 0), self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        df["rsi"] = rsi(df["close"], self.cfg.rsi_period)
        trend = self.p("trend_ema", 0)
        if trend > 0:
            df["ema_trend"] = ema(df["close"], trend)

    def signals(self, df):
        buy = (df["rsi"] < self.p("rsi_buy", 30)).to_numpy() & (df["atr"] > 0).to_numpy()
        if self.p("trend_ema", 0) > 0:
            buy = buy & (df["close"] > df["ema_trend"]).to_numpy()
        sell = (df["rsi"] > self.p("rsi_sell", 55)).to_numpy()
        return buy, sell

    def short_signals(self, df):
        # gespiegelte Schwellen: rsi_buy=30 -> Short ueber 70, rsi_sell=55 -> Ausstieg unter 45
        short = (df["rsi"] > 100.0 - self.p("rsi_buy", 30)).to_numpy() & (df["atr"] > 0).to_numpy()
        if self.p("trend_ema", 0) > 0:
            short = short & (df["close"] < df["ema_trend"]).to_numpy()
        cover = (df["rsi"] < 100.0 - self.p("rsi_sell", 55)).to_numpy()
        return short, cover
