from __future__ import annotations

import pandas as pd

from ..indicators import ema, sma
from .base import Strategy, WARMUP_FACTOR


class BollingerReversionStrategy(Strategy):
    """Rueckkehr zum Mittelwert: Kauf, wenn der Kurs unter das untere Bollinger-Band faellt,
    Verkauf, sobald er die Mittellinie erreicht."""

    name = "bollinger_reversion"
    buy_reason = "Kurs unter dem unteren Bollinger-Band"
    sell_reason = "Kurs an der Mittellinie"
    param_grid = {"bb_period": [20, 40], "bb_std": [2.0, 2.5], "trend_ema": [0, 200]}

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(self.p("bb_period", 20), self.p("trend_ema", 0), self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        period = self.p("bb_period", 20)
        mid = sma(df["close"], period)
        sd = df["close"].rolling(period, min_periods=period).std(ddof=0)
        df["bb_mid"] = mid
        df["bb_lower"] = mid - self.p("bb_std", 2.0) * sd
        trend = self.p("trend_ema", 0)
        if trend > 0:
            df["ema_trend"] = ema(df["close"], trend)

    def signals(self, df):
        buy = (df["close"] < df["bb_lower"]).to_numpy() & (df["atr"] > 0).to_numpy()
        if self.p("trend_ema", 0) > 0:
            buy = buy & (df["close"] > df["ema_trend"]).to_numpy()
        sell = (df["close"] > df["bb_mid"]).to_numpy()
        return buy, sell
