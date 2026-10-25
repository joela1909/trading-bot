from __future__ import annotations

import pandas as pd

from .base import Strategy, WARMUP_FACTOR


class DonchianStrategy(Strategy):
    """Ausbruchs-Strategie: Kauf bei Schlusskurs ueber dem Hoch der letzten N Kerzen,
    Verkauf bei Schlusskurs unter dem Tief der letzten M Kerzen."""

    name = "donchian"
    buy_reason = "Ausbruch ueber das Hoch der letzten Kerzen"
    sell_reason = "Schlusskurs unter dem Tief der letzten Kerzen"
    short_reason = "Ausbruch unter das Tief der letzten Kerzen"
    cover_reason = "Schlusskurs ueber dem Hoch der letzten Kerzen"
    param_grid = {"donchian_entry": [20, 40, 80], "donchian_exit": [10, 20]}
    chart_lines = [("don_high", "Ausbruch Hoch (Long)", "long"), ("don_low_entry", "Ausbruch Tief (Short)", "short")]

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(self.p("donchian_entry", 40), self.p("donchian_exit", 20), self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        df["don_high"] = df["high"].rolling(self.p("donchian_entry", 40)).max().shift(1)
        df["don_low"] = df["low"].rolling(self.p("donchian_exit", 20)).min().shift(1)
        df["don_low_entry"] = df["low"].rolling(self.p("donchian_entry", 40)).min().shift(1)
        df["don_high_exit"] = df["high"].rolling(self.p("donchian_exit", 20)).max().shift(1)

    def signals(self, df):
        buy = (df["close"] > df["don_high"]).to_numpy() & (df["atr"] > 0).to_numpy()
        sell = (df["close"] < df["don_low"]).to_numpy()
        return buy, sell

    def short_signals(self, df):
        short = (df["close"] < df["don_low_entry"]).to_numpy() & (df["atr"] > 0).to_numpy()
        cover = (df["close"] > df["don_high_exit"]).to_numpy()
        return short, cover
