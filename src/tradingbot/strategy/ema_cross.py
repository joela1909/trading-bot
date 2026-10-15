from __future__ import annotations

import math

import pandas as pd

from ..indicators import atr, ema, rsi
from ..models import Decision, Signal
from .base import Strategy


class EmaCrossStrategy(Strategy):
    """Trendfolge: Kauf beim Kreuzen der schnellen ueber die langsame EMA (RSI-Filter),
    Verkauf beim Gegenkreuz. Stop-Loss und Take-Profit werden ueber die ATR bemessen."""

    name = "ema_cross"

    @property
    def warmup_bars(self) -> int:
        return 3 * max(self.cfg.slow_ema, self.cfg.rsi_period, self.cfg.atr_period)

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = ema(out["close"], self.cfg.fast_ema)
        out["ema_slow"] = ema(out["close"], self.cfg.slow_ema)
        out["rsi"] = rsi(out["close"], self.cfg.rsi_period)
        out["atr"] = atr(out, self.cfg.atr_period)
        return out

    def decide(self, df: pd.DataFrame, i: int = -1) -> Decision:
        if len(df) < 2:
            return Decision(Signal.HOLD, "zu wenig Daten")
        if i < 0:
            i = len(df) + i
        if i < 1:
            return Decision(Signal.HOLD, "zu wenig Daten")
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        needed = (row["ema_fast"], row["ema_slow"], row["rsi"], row["atr"], prev["ema_fast"], prev["ema_slow"])
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in needed):
            return Decision(Signal.HOLD, "Indikatoren noch im Warmup")

        atr_value = float(row["atr"])
        close = float(row["close"])
        crossed_up = prev["ema_fast"] <= prev["ema_slow"] and row["ema_fast"] > row["ema_slow"]
        crossed_down = prev["ema_fast"] >= prev["ema_slow"] and row["ema_fast"] < row["ema_slow"]

        if crossed_down:
            return Decision(Signal.SELL, "EMA-Kreuz nach unten", atr=atr_value)

        if crossed_up:
            if row["rsi"] >= self.cfg.rsi_max_entry:
                return Decision(Signal.HOLD, f"EMA-Kreuz nach oben, aber RSI {row['rsi']:.1f} ueberkauft", atr=atr_value)
            if atr_value <= 0:
                return Decision(Signal.HOLD, "ATR ist null, kein sinnvoller Stop", atr=atr_value)
            stop = close - self.cfg.stop_atr_mult * atr_value
            tp = close + self.cfg.take_profit_atr_mult * atr_value if self.cfg.take_profit_atr_mult > 0 else None
            return Decision(Signal.BUY, "EMA-Kreuz nach oben", stop_loss=stop, take_profit=tp, atr=atr_value)

        return Decision(Signal.HOLD, "kein Signal", atr=atr_value)
