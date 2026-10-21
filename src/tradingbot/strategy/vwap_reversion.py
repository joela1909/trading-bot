from __future__ import annotations

import pandas as pd

from ..indicators import ema, session_vwap
from ..session import session_day_key
from .base import WARMUP_FACTOR, Strategy

MIN_BARS_INTO_DAY = 4


class VwapReversionStrategy(Strategy):
    """Tageshandel, Rueckkehr zum Mittelwert: Kauf, wenn der Kurs mehrere ATR unter den Tages-VWAP faellt,
    Verkauf, sobald er den VWAP wieder erreicht. Der VWAP beginnt jeden Tag (in der Handelszeitzone) neu."""

    name = "vwap_reversion"
    buy_reason = "Kurs deutlich unter dem Tages-VWAP"
    sell_reason = "Kurs zurueck am Tages-VWAP"
    short_reason = "Kurs deutlich ueber dem Tages-VWAP"
    cover_reason = "Kurs zurueck am Tages-VWAP"
    param_grid = {"vwap_dev_atr": [1.0, 1.5, 2.0, 3.0], "trend_ema": [0, 200]}

    @property
    def warmup_bars(self) -> int:
        # mindestens ein ganzer Tag, damit der Live-Bot den VWAP wie der Backtest ab Mitternacht berechnet
        day = (self.bars_per_day or 0) + 20
        return max(300, day, WARMUP_FACTOR * max(self.p("trend_ema", 0), self.cfg.atr_period))

    def _add_indicators(self, df: pd.DataFrame) -> None:
        day = session_day_key(df["timestamp"], self.session.timezone)
        df["vwap"] = session_vwap(df, day)
        df["bars_into_day"] = df.groupby(day.to_numpy()).cumcount()
        trend = self.p("trend_ema", 0)
        if trend > 0:
            df["ema_trend"] = ema(df["close"], trend)

    def signals(self, df):
        dev = self.p("vwap_dev_atr", 2.0)
        buy = (
            (df["close"] < df["vwap"] - dev * df["atr"]).to_numpy()
            & (df["atr"] > 0).to_numpy()
            & (df["bars_into_day"] >= MIN_BARS_INTO_DAY).to_numpy()
        )
        if self.p("trend_ema", 0) > 0:
            buy = buy & (df["close"] > df["ema_trend"]).to_numpy()
        sell = (df["close"] >= df["vwap"]).to_numpy() & df["atr"].notna().to_numpy()
        return buy, sell

    def short_signals(self, df):
        dev = self.p("vwap_dev_atr", 2.0)
        short = (
            (df["close"] > df["vwap"] + dev * df["atr"]).to_numpy()
            & (df["atr"] > 0).to_numpy()
            & (df["bars_into_day"] >= MIN_BARS_INTO_DAY).to_numpy()
        )
        if self.p("trend_ema", 0) > 0:
            short = short & (df["close"] < df["ema_trend"]).to_numpy()
        cover = (df["close"] <= df["vwap"]).to_numpy() & df["atr"].notna().to_numpy()
        return short, cover
