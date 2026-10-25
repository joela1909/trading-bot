from __future__ import annotations

import pandas as pd

from .base import WARMUP_FACTOR, Strategy


class TimeSeriesMomentumStrategy(Strategy):
    """Zeitreihen-Momentum: mit dem mittelfristigen Trend handeln.

    Long, sobald der Kurs ueber dem Kurs von vor `mom_lookback` Kerzen liegt und dazu ueber seinem gleitenden
    Durchschnitt derselben Laenge. Short spiegelbildlich. Ausstieg, wenn das Momentum die Richtung wechselt.
    Eingestiegen wird nur beim Wechsel, nicht auf jeder Kerze des Trends."""

    name = "tsmom"
    buy_reason = "Momentum dreht nach oben"
    sell_reason = "Momentum dreht nach unten"
    short_reason = "Momentum dreht nach unten"
    cover_reason = "Momentum dreht nach oben"
    param_grid = {"mom_lookback": [20, 50, 100, 200]}
    chart_lines = [("mom_ma", "Durchschnitt (Momentum)", "trend")]

    @property
    def lookback(self) -> int:
        return int(self.p("mom_lookback", 50))

    @property
    def warmup_bars(self) -> int:
        return max(WARMUP_FACTOR * self.cfg.atr_period, 2 * self.lookback + 5)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        n = self.lookback
        df["mom"] = df["close"] / df["close"].shift(n) - 1.0
        df["mom_ma"] = df["close"].rolling(n).mean()
        up = (df["mom"] > 0) & (df["close"] > df["mom_ma"])
        down = (df["mom"] < 0) & (df["close"] < df["mom_ma"])
        df["mom_up"] = up
        df["mom_down"] = down

    def signals(self, df):
        up, down = df["mom_up"], df["mom_down"]
        ok = (df["atr"] > 0).to_numpy()
        buy = (up & ~up.shift(1, fill_value=False)).to_numpy() & ok
        sell = down.to_numpy() | (df["mom"] < 0).to_numpy()
        return buy, sell

    def short_signals(self, df):
        up, down = df["mom_up"], df["mom_down"]
        ok = (df["atr"] > 0).to_numpy()
        short = (down & ~down.shift(1, fill_value=False)).to_numpy() & ok
        cover = up.to_numpy() | (df["mom"] > 0).to_numpy()
        return short, cover
