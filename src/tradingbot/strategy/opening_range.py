from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import ema, session_vwap
from ..session import session_day_key
from .base import WARMUP_FACTOR, Strategy


class OpeningRangeBreakoutStrategy(Strategy):
    """Tageshandel, Ausbruch: Hoch und Tief der ersten Minuten nach Handelsbeginn (entry_start) bilden die
    Eroeffnungsspanne. Kauf beim ersten Schlusskurs ueber dem Hoch, Verkauf bei Schlusskurs unter dem Tages-VWAP."""

    name = "opening_range"
    buy_reason = "Ausbruch ueber die Eroeffnungsspanne"
    sell_reason = "Schlusskurs unter dem Tages-VWAP"
    param_grid = {"orb_minutes": [30, 60, 120], "trend_ema": [0, 200]}

    @property
    def warmup_bars(self) -> int:
        return max(300, WARMUP_FACTOR * max(self.p("trend_ema", 0), self.cfg.atr_period))

    def _add_indicators(self, df: pd.DataFrame) -> None:
        ts = df["timestamp"].astype("int64")
        tf_min = int(np.median(np.diff(ts.to_numpy()))) // 60_000 if len(df) > 1 else 1
        local = pd.to_datetime(ts, unit="ms", utc=True).dt.tz_convert(self.session.timezone)
        open_min = (local.dt.hour * 60 + local.dt.minute).to_numpy()
        day = session_day_key(ts, self.session.timezone).to_numpy()
        start = self.session.entry_start_min
        range_end = start + self.p("orb_minutes", 60)

        in_range = (open_min >= start) & (open_min + tf_min <= range_end)
        ready = open_min + tf_min > range_end
        or_high = df["high"].where(in_range).groupby(day).cummax().groupby(day).ffill()
        or_low = df["low"].where(in_range).groupby(day).cummin().groupby(day).ffill()
        df["or_high"] = or_high.where(ready)
        df["or_low"] = or_low.where(ready)
        df["vwap"] = session_vwap(df, pd.Series(day, index=df.index))
        trend = self.p("trend_ema", 0)
        if trend > 0:
            df["ema_trend"] = ema(df["close"], trend)

    def signals(self, df):
        above = (df["close"] > df["or_high"]).to_numpy()
        prev_above = np.concatenate([[False], above[:-1]])
        buy = above & ~prev_above & (df["atr"] > 0).to_numpy()
        if self.p("trend_ema", 0) > 0:
            buy = buy & (df["close"] > df["ema_trend"]).to_numpy()
        sell = (df["close"] < df["vwap"]).to_numpy() & df["atr"].notna().to_numpy()
        return buy, sell
