from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
from .base import Strategy


class PdhPdlSweepFadeStrategy(Strategy):
    """Gescheiterter Ausbruch ueber das Hoch oder Tief des Vortags.

    Short: Die Kerze bricht ueber das Vortageshoch (PDH), schliesst aber wieder darunter und ist bearish.
    Long: Die Kerze bricht unter das Vortagestief (PDL), schliesst aber wieder darueber und ist bullish.
    Optional muss die Kerze die vorherige Kerze umschliessen (Engulfing).

    Das Signal faellt beim Kerzenschluss, der Einstieg erfolgt zur Eroeffnung der naechsten Kerze. Stop und Ziel sind feste
    Kurse je Trade: Stop = x tageweise ATR oder Extrem der Signalkerze, Ziel = x tageweise ATR oder die gegenueberliegende Vortageslinie.
    Der Vortag ist der Kalendertag in `tz` (Standard New York). Die tageweise ATR stammt vom letzten abgeschlossenen Tag.
    """

    name = "pdh_pdl_sweep"
    buy_reason = "Ausbruch unter das Vortagestief gescheitert"
    short_reason = "Ausbruch ueber das Vortageshoch gescheitert"
    sell_reason = cover_reason = "Ausstieg nur ueber Stop und Ziel"
    entry_on_next_open = True
    fixed_levels = True
    use_exit_grid = False
    exit_param_keys = ("sl_mode", "sl_atr_mult", "tp_mode", "tp_atr_mult")
    param_grid = {
        "sl_mode": [0, 1],
        "sl_atr_mult": [0.3, 0.6, 1.0],
        "tp_mode": [0, 1],
        "tp_atr_mult": [0.1, 0.3, 0.6, 1.0],
        "require_engulf": [False, True],
    }

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None, timeframe: str | None = None):
        super().__init__(cfg, session, timeframe)
        self.levels_from_fill = bool(self.p("levels_from_fill", False))

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        # Bei Extrem-Stop bzw. Gegenlinie-Ziel spielt das ATR-Vielfache keine Rolle: nur einen Wert behalten.
        if params.get("sl_mode") == 0 and params.get("sl_atr_mult") != cls.param_grid["sl_atr_mult"][0]:
            return False
        if params.get("tp_mode") == 0 and params.get("tp_atr_mult") != cls.param_grid["tp_atr_mult"][0]:
            return False
        return True

    @property
    def warmup_bars(self) -> int:
        per_day = self.bars_per_day or 24
        return per_day * (3 * int(self.p("daily_atr_period", 14)))

    # ------------------------------------------------------------------ Indikatoren
    def _add_indicators(self, df: pd.DataFrame) -> None:
        tz = self.p("tz", "America/New_York")
        local = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True).dt.tz_convert(tz)
        day = pd.Series(local.dt.strftime("%Y-%m-%d").to_numpy(), index=df.index)
        hhmm = (local.dt.hour * 100 + local.dt.minute).to_numpy()
        valid = (hhmm >= 930) & (hhmm < 1600) if self.p("prior_day_rth_only", False) else np.ones(len(df), dtype=bool)

        g = pd.DataFrame({"day": day, "hi": df["high"].where(valid), "lo": df["low"].where(valid)})
        day_hi = g.groupby("day", sort=False)["hi"].max()
        day_lo = g.groupby("day", sort=False)["lo"].min()
        # Vortag = der letzte Tag davor mit gueltigen Kerzen. Ohne gueltigen Vortag bleiben ueltere Level stehen.
        df["pdh"] = day.map(day_hi.shift(1).ffill()).to_numpy()
        df["pdl"] = day.map(day_lo.shift(1).ffill()).to_numpy()

        daily = pd.DataFrame({"day": day, "high": df["high"], "low": df["low"], "close": df["close"]}).groupby("day", sort=False).agg(
            high=("high", "max"), low=("low", "min"), close=("close", "last")
        )
        prev_close = daily["close"].shift(1)
        tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - prev_close).abs(), (daily["low"] - prev_close).abs()], axis=1).max(axis=1)
        period = max(2, int(self.p("daily_atr_period", 14)))
        atr_daily = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().shift(1)  # nur abgeschlossene Tage
        df["atr_d"] = day.map(atr_daily).to_numpy()
        df["atr_d"] = df["atr_d"].where(df["atr_d"] > 0, df["atr"])  # Rueckfall wie im Original: ATR der Kerzen

    # ---------------------------------------------------------------------- Signale
    def _engulf(self, df: pd.DataFrame, bearish: bool) -> np.ndarray:
        po, pc = df["open"].shift(1), df["close"].shift(1)
        o, c = df["open"], df["close"]
        if bearish:
            ok = (pc > po) & (c < o) & (o >= pc) & (c <= po)
        else:
            ok = (pc < po) & (c > o) & (o <= pc) & (c >= po)
        if self.p("engulf_full_range", False):
            ok = ok & (df["high"] >= df["high"].shift(1)) & (df["low"] <= df["low"].shift(1))
        return ok.fillna(False).to_numpy()

    def signals(self, df):
        o, c, h, l = df["open"], df["close"], df["high"], df["low"]
        buy = ((l < df["pdl"]) & (c > df["pdl"]) & (c > o)).to_numpy()
        if self.p("require_engulf", False):
            buy = buy & self._engulf(df, bearish=False)
        if not self.p("allow_long", True):
            buy = np.zeros(len(df), dtype=bool)
        buy = buy & (df["atr"] > 0).to_numpy()
        return buy, np.zeros(len(df), dtype=bool)

    def short_signals(self, df):
        o, c, h = df["open"], df["close"], df["high"]
        short = ((h > df["pdh"]) & (c < df["pdh"]) & (c < o)).to_numpy()
        if self.p("require_engulf", False):
            short = short & self._engulf(df, bearish=True)
        short = short & (df["atr"] > 0).to_numpy()
        return short, np.zeros(len(df), dtype=bool)

    # ----------------------------------------------------------- Stop und Ziel je Trade
    def entry_levels(self, df):
        c, h, l = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
        atr_d, pdh, pdl = df["atr_d"].to_numpy(), df["pdh"].to_numpy(), df["pdl"].to_numpy()
        sl_mult, tp_mult = float(self.p("sl_atr_mult", 0.3)), float(self.p("tp_atr_mult", 0.1))
        atr_stop = int(self.p("sl_mode", 1)) == 1
        atr_target = int(self.p("tp_mode", 1)) == 1
        long_stop = c - sl_mult * atr_d if atr_stop else l
        short_stop = c + sl_mult * atr_d if atr_stop else h
        long_tp = c + tp_mult * atr_d if atr_target else pdh
        short_tp = c - tp_mult * atr_d if atr_target else pdl
        return long_stop, long_tp, short_stop, short_tp
