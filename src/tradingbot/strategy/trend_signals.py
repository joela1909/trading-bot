from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
from ..indicators import adx, atr, ema, rsi, sma
from .base import WARMUP_FACTOR, Strategy


def _atr_trail(high: np.ndarray, low: np.ndarray, close: np.ndarray, atr_v: np.ndarray, mult: float) -> tuple[np.ndarray, np.ndarray]:
    """ATR-Trendlinie mit Nachziehen (Supertrend-Verfahren). Gibt (Trend +1/-1/0, Linie) zurueck.
    In einem Aufwaertstrend liegt die Linie unter dem Kurs und steigt nur. Schliesst der Kurs darunter, dreht der Trend."""
    n = len(close)
    mid = (high + low) / 2.0
    upper, lower = mid + mult * atr_v, mid - mult * atr_v
    f_up, f_lo = np.full(n, np.nan), np.full(n, np.nan)
    trend = np.zeros(n, dtype=int)
    line = np.full(n, np.nan)
    started = False
    for i in range(n):
        if atr_v[i] != atr_v[i]:
            continue
        if not started:
            f_up[i], f_lo[i] = upper[i], lower[i]
            trend[i] = 1 if close[i] >= mid[i] else -1
            started = True
        else:
            f_up[i] = upper[i] if (upper[i] < f_up[i - 1] or close[i - 1] > f_up[i - 1]) else f_up[i - 1]
            f_lo[i] = lower[i] if (lower[i] > f_lo[i - 1] or close[i - 1] < f_lo[i - 1]) else f_lo[i - 1]
            trend[i] = trend[i - 1]
            if trend[i - 1] == -1 and close[i] > f_up[i]:
                trend[i] = 1
            elif trend[i - 1] == 1 and close[i] < f_lo[i]:
                trend[i] = -1
        line[i] = f_lo[i] if trend[i] == 1 else f_up[i]
    return trend, line


class TrendSignalsStrategy(Strategy):
    """Trendsignale mit Bestaetigung und Ausstieg, aus allgemein bekannten Bausteinen zusammengesetzt.

    Trendlinie: ATR-Trendlinie, die nachzieht. Dreht sie, entsteht ein Signal.
    Modus 0 (Trend, Standard): Einstieg in Richtung der neuen Trendrichtung, wenn die Filter es bestaetigen. Ausstieg bei der Gegendrehung.
    Modus 1 (Gegenbewegung): Einstieg gegen den Trend bei einem Ausreisser ueber das Bollinger-Band mit extremem RSI, Ausstieg an der Mittellinie.
    Filter: Trendstaerke (ADX), Lage zum langsamen EMA, relative Volatilitaet, optional kein Einstieg im Squeeze, Mindest-Qualitaet 0 bis 4.
    Die Qualitaet zaehlt offen nachvollziehbar: ADX stark, Kurs richtig zum EMA, Volumen ueber dem Schnitt, kein Squeeze.
    Das ist ein eigener Nachbau der Funktionen, keine Kopie eines fremden Indikators, und liefert andere Signale als dieser.
    """

    name = "trend_signals"
    buy_reason = "Trendlinie dreht nach oben, Filter bestaetigen"
    sell_reason = "Trendlinie dreht nach unten"
    short_reason = "Trendlinie dreht nach unten, Filter bestaetigen"
    cover_reason = "Trendlinie dreht nach oben"
    param_grid = {"trail_atr_mult": [1.5, 2.0, 3.0], "adx_min": [0, 20], "confirm_ema": [0, 100]}

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None, timeframe: str | None = None):
        super().__init__(cfg, session, timeframe)

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(int(self.p("trail_atr_length", 10)), int(self.p("confirm_ema", 0)), 50, self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        length = max(2, int(self.p("trail_atr_length", 10)))
        df["atr_trail"] = atr(df, length)
        trend, line = _atr_trail(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), df["atr_trail"].to_numpy(), float(self.p("trail_atr_mult", 2.0)))
        df["trail_trend"], df["trail_line"] = trend, line
        df["adx"] = adx(df, 14)
        confirm = int(self.p("confirm_ema", 0))
        df["ema_confirm"] = ema(df["close"], confirm) if confirm > 0 else np.nan
        df["rel_atr"] = df["atr_trail"] / atr(df, 50)
        mid = sma(df["close"], 20)
        sd = df["close"].rolling(20, min_periods=20).std(ddof=0)
        df["bb_mid"], df["bb_up"], df["bb_lo"] = mid, mid + 2.0 * sd, mid - 2.0 * sd
        kc = 1.5 * atr(df, 20)
        df["squeeze"] = ((df["bb_up"] < mid + kc) & (df["bb_lo"] > mid - kc)).fillna(False)
        df["vol_ok"] = (df["volume"] >= df["volume"].rolling(20, min_periods=1).mean()).fillna(False)
        df["rsi_sig"] = rsi(df["close"], 14)

    # -------------------------------------------------------------------- Filter
    def _filters(self, df: pd.DataFrame, long: bool) -> np.ndarray:
        ok = np.ones(len(df), dtype=bool)
        adx_min = float(self.p("adx_min", 0))
        if adx_min > 0:
            ok &= (df["adx"] >= adx_min).fillna(False).to_numpy()
        if int(self.p("confirm_ema", 0)) > 0:
            side = (df["close"] > df["ema_confirm"]) if long else (df["close"] < df["ema_confirm"])
            ok &= side.fillna(False).to_numpy()
        max_rel = float(self.p("max_relative_atr", 0))
        if max_rel > 0:
            ok &= (df["rel_atr"] <= max_rel).fillna(False).to_numpy()
        if self.p("block_squeeze", False):
            ok &= ~df["squeeze"].to_numpy()
        min_q = int(self.p("min_quality", 0))
        if min_q > 0:
            ok &= self._quality(df, long) >= min_q
        return ok & (df["atr"] > 0).to_numpy()

    def _quality(self, df: pd.DataFrame, long: bool) -> np.ndarray:
        """Qualitaet 0 bis 4: ADX stark, Kurs richtig zum langsamen EMA, Volumen ueber dem Schnitt, kein Squeeze."""
        slow = ema(df["close"], 100)
        side = (df["close"] > slow) if long else (df["close"] < slow)
        points = (df["adx"] >= 20).fillna(False).astype(int) + side.fillna(False).astype(int) + df["vol_ok"].astype(int) + (~df["squeeze"]).astype(int)
        return points.to_numpy()

    # -------------------------------------------------------------------- Signale
    def _flips(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        t = df["trail_trend"].to_numpy()
        prev = np.concatenate([[0], t[:-1]])
        return (t == 1) & (prev == -1), (t == -1) & (prev == 1)

    def signals(self, df):
        up, down = self._flips(df)
        if int(self.p("mode", 0)) == 1:
            buy = (df["close"] < df["bb_lo"]).fillna(False).to_numpy() & (df["rsi_sig"] < float(self.p("rsi_low", 30))).fillna(False).to_numpy()
            return buy & self._filters_neutral(df), (df["close"] >= df["bb_mid"]).fillna(False).to_numpy() | up
        return up & self._filters(df, True), down

    def short_signals(self, df):
        up, down = self._flips(df)
        if int(self.p("mode", 0)) == 1:
            short = (df["close"] > df["bb_up"]).fillna(False).to_numpy() & (df["rsi_sig"] > float(self.p("rsi_high", 70))).fillna(False).to_numpy()
            return short & self._filters_neutral(df), (df["close"] <= df["bb_mid"]).fillna(False).to_numpy() | down
        return down & self._filters(df, False), up

    def _filters_neutral(self, df: pd.DataFrame) -> np.ndarray:
        return (df["atr"] > 0).to_numpy()
