from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
from ..indicators import atr, ema
from .base import WARMUP_FACTOR, Strategy


class AnchoredVwapSignalStrategy(Strategy):
    """VWAP seit Mitternacht (New Yorker Zeit) gegen einen EMA, gesteuert von einer Zustandsmaschine pro Tag.

    Marker, wie im Original:
    - Gruener Punkt: Long-Einstieg. Roter Punkt: Short-Einstieg.
    - Gruenes Dreieck: Long schliessen. Rotes Dreieck: Short schliessen.
    Ablauf: Liegt der Kurs unter dem EMA und der EMA unter dem VWAP, kommt ein roter Punkt und es wird auf ein rotes Dreieck gewartet, sobald
    der Kurs wieder ueber den EMA schliesst. Danach kann der EMA ueber den VWAP steigen, und faellt der Kurs dann unter den VWAP, kommt ein gruener Punkt.
    Spiegelbildlich fuer die andere Seite. Signale gibt es nur im Handelsfenster, der Zustand beginnt jeden Tag neu.

    Der Einstieg erfolgt zur Eroeffnung der Kerze nach dem Punkt, der Stop liegt bei x ATR vom Schlusskurs der Signalkerze. Ein Ausstieg durch ein
    Dreieck gilt ebenfalls zur naechsten Eroeffnung. Tagesversuche: Nach dem ersten abgeschlossenen Trade des Tages sind weitere nur nach einem
    Verlust erlaubt, hoechstens `additional_attempts`. Ein relativer ATR-Filter blockiert Einstiege bei zu hoher Schwankung.
    Bisher nur im Backtest, weil die Tagesversuche im Live-Bot noch fehlen.
    """

    name = "anchored_vwap"
    buy_reason = "Gruener Punkt"
    sell_reason = "Gruenes Dreieck"
    short_reason = "Roter Punkt"
    cover_reason = "Rotes Dreieck"
    entry_on_next_open = True
    exit_on_next_open = True
    fixed_levels = True
    use_exit_grid = False
    backtest_only = True
    exit_param_keys = ("atr_stop_multiplier", "additional_attempts")
    param_grid = {
        "ema_length": [10, 20, 50],
        "atr_stop_multiplier": [1.0, 2.0, 3.0],
        "max_relative_atr": [1.2, 99.0],
        "additional_attempts": [0, 2],
    }

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None, timeframe: str | None = None):
        super().__init__(cfg, session, timeframe)
        if not self.p("use_atr_stop", True):
            raise ValueError("anchored_vwap: use_atr_stop=false wird nicht unterstuetzt, die Positionsgroesse braucht den Stop")

    @property
    def warmup_bars(self) -> int:
        recursive = WARMUP_FACTOR * max(int(self.p("ema_length", 20)), int(self.p("baseline_atr_period", 50)), self.cfg.atr_period)
        return max(recursive, 2 * (self.bars_per_day or 288))

    # ------------------------------------------------------------------ Indikatoren
    def _in_session(self, hhmm: np.ndarray) -> np.ndarray:
        a = min(max(int(self.p("session_start_hhmm", 930)), 0), 2359)
        b = min(max(int(self.p("session_end_hhmm", 1600)), 0), 2359)
        return (hhmm >= a) & (hhmm <= b) if a <= b else (hhmm >= a) | (hhmm <= b)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        tz = self.p("tz", "America/New_York")
        local = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True).dt.tz_convert(tz)
        day = (local.dt.year * 10000 + local.dt.month * 100 + local.dt.day).to_numpy()
        hhmm = (local.dt.hour * 100 + local.dt.minute).to_numpy()
        df["day_key"] = day

        price = (df["high"] + df["low"] + df["close"]) / 3.0 if self.p("use_typical_price", True) else df["close"]
        vol = df["volume"].clip(lower=0.0)
        cum_vol = vol.groupby(day).cumsum()
        cum_pv = (price * vol).groupby(day).cumsum()
        df["vwap"] = (cum_pv / cum_vol.where(cum_vol > 0)).to_numpy()
        df["ema_sig"] = ema(df["close"], max(1, int(self.p("ema_length", 20))))
        df["atr_base"] = atr(df, max(2, int(self.p("baseline_atr_period", 50))))
        df["rel_atr"] = df["atr"] / df["atr_base"]

        n = len(df)
        gd, rd, gt, rt = (np.zeros(n, dtype=bool) for _ in range(4))
        close, e, vw, a, b = (df[c].to_numpy() for c in ("close", "ema_sig", "vwap", "atr", "atr_base"))
        cv = cum_vol.to_numpy()
        sess = self._in_session(hhmm)
        regime, wait_red, wait_green = 0, False, False
        prev_close = prev_vwap = 0.0
        has_prev = False
        cur_day = None
        for i in range(n):
            if day[i] != cur_day:
                cur_day, regime, wait_red, wait_green, has_prev = day[i], 0, False, False, False
            ei, vi, ai, bi = e[i], vw[i], a[i], b[i]
            if ei != ei or cv[i] <= 0 or vi != vi or ai != ai or bi != bi or ai <= 0 or bi <= 0:
                continue
            ci = close[i]
            if sess[i]:
                if wait_red and ci > ei:
                    rt[i], wait_red, regime = True, False, -2
                elif wait_green and ci < ei:
                    gt[i], wait_green, regime = True, False, 2
                elif regime == -2 and ei > vi:
                    regime = -3
                elif regime == 2 and ei < vi:
                    regime = 3
                elif regime == -3 and has_prev and prev_close >= prev_vwap and ci < vi:
                    gd[i], regime = True, 0
                elif regime == 3 and has_prev and prev_close <= prev_vwap and ci > vi:
                    rd[i], regime = True, 0
                elif regime == 0:
                    if ei < vi and ci < ei:
                        rd[i], wait_red, regime = True, True, -1
                    elif ei > vi and ci > ei:
                        gd[i], wait_green, regime = True, True, 1
            prev_close, prev_vwap, has_prev = ci, vi, True
        df["green_dot"], df["red_dot"], df["green_tri"], df["red_tri"] = gd, rd, gt, rt

    # ---------------------------------------------------------------------- Signale
    def _vol_ok(self, df: pd.DataFrame) -> np.ndarray:
        if not self.p("use_relative_volatility_filter", True):
            return np.ones(len(df), dtype=bool)
        return (df["rel_atr"] <= max(float(self.p("max_relative_atr", 1.2)), 0.1)).fillna(False).to_numpy()

    def signals(self, df):
        return df["green_dot"].to_numpy() & self._vol_ok(df), df["green_tri"].to_numpy()

    def short_signals(self, df):
        return df["red_dot"].to_numpy() & self._vol_ok(df), df["red_tri"].to_numpy()

    # ------------------------------------------------- Stop je Trade und Tagesversuche
    def entry_levels(self, df):
        c, a = df["close"].to_numpy(), df["atr"].to_numpy()
        m = max(float(self.p("atr_stop_multiplier", 2.0)), 0.1)
        no_target = np.full(len(df), np.nan)
        return c - m * a, no_target, c + m * a, no_target.copy()

    def attempt_rule(self):
        return {"stop_after_first_win": True, "additional_attempts": int(self.p("additional_attempts", 2))}

    def day_keys(self, df):
        return df["day_key"].to_numpy()
