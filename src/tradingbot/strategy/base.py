from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
from ..data import timeframe_seconds
from ..indicators import atr
from ..models import Decision, Signal

# Geglaettete Indikatoren (EMA, RSI, ATR) schwingen erst nach vielen Kerzen auf ihren Endwert ein. Mit diesem
# Faktor kommen Live-Bot (wenige Kerzen) und Backtest (ganze Historie) auf praktisch dieselben Signale.
WARMUP_FACTOR = 8

EXIT_GRID: dict[str, list] = {
    "stop_atr_mult": [2.0, 3.0],
    "take_profit_atr_mult": [0.0, 4.0],
}


class Strategy(ABC):
    """Eine Strategie liefert vektorisierte Kauf- und Verkaufssignale.

    Backtester und Live-Bot nutzen dieselben Signale, damit Test und Realitaet nicht auseinanderlaufen.
    Stop-Loss, Take-Profit und Trailing-Stop sind fuer alle Strategien gleich und ATR-basiert.
    """

    name: str = "base"
    buy_reason: str = "Kaufsignal"
    sell_reason: str = "Verkaufssignal"
    short_reason: str = "Short-Signal"
    cover_reason: str = "Short-Ausstiegssignal"
    param_grid: dict[str, list] = {}
    # Linien fuer die Marktansicht im Dashboard: (Spalte, Beschriftung, Stil main|long|short|trend)
    chart_lines: list[tuple[str, str, str]] = []

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None, timeframe: str | None = None):
        # eigene Stop- und Zielwerte dieser Strategie (strategy.exits.<name>) haben Vorrang
        own = (getattr(cfg, "exits", None) or {}).get(self.name)
        if own is not None:
            update = {k: v for k, v in own.model_dump().items() if v is not None}
            if update:
                cfg = cfg.model_copy(update=update)
        self.cfg = cfg
        # Intraday-Strategien brauchen Zeitzone und Handelsbeginn. Ohne Tageshandel: UTC, Tag ab 00:00.
        self.session = session or DaytradingConfig(timezone="UTC", entry_start="00:00", entry_end="23:59", flat_time="24:00")
        self.timeframe_ms: int | None = timeframe_seconds(timeframe) * 1000 if timeframe else None

    @property
    def bars_per_day(self) -> int | None:
        return 86_400_000 // self.timeframe_ms if self.timeframe_ms else None

    def p(self, key: str, default: Any) -> Any:
        value = getattr(self.cfg, key, None)
        return default if value is None else value

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return True

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Anzahl Kerzen, die fuer stabile Indikatorwerte noetig sind."""

    @abstractmethod
    def _add_indicators(self, df: pd.DataFrame) -> None:
        """Fuegt strategiespezifische Indikator-Spalten in-place hinzu."""

    @abstractmethod
    def signals(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Gibt (kauf, verkauf) als Bool-Arrays zurueck. Auf einem vorbereiteten DataFrame."""

    def short_signals(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Gibt (short_einstieg, short_ausstieg) als Bool-Arrays zurueck. Standard: keine Shorts."""
        z = np.zeros(len(df), dtype=bool)
        return z, z.copy()

    def all_signals(self, df: pd.DataFrame) -> tuple:
        """(kauf, verkauf, short, cover). short und cover sind None, wenn Shorts aus sind."""
        buy, sell = self.signals(df)
        if not self.cfg.allow_short:
            return buy, sell, None, None
        short, cover = self.short_signals(df)
        return buy, sell, short, cover

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["atr"] = atr(out, self.cfg.atr_period)
        self._add_indicators(out)
        return out

    def decide(self, df: pd.DataFrame, i: int = -1, context: dict | None = None) -> Decision:
        n = len(df)
        if n < 2:
            return Decision(Signal.HOLD, "zu wenig Daten")
        if i < 0:
            i = n + i
        if i < 1:
            return Decision(Signal.HOLD, "zu wenig Daten")
        atr_value = float(df["atr"].iloc[i])
        if np.isnan(atr_value):
            return Decision(Signal.HOLD, "Indikatoren noch im Warmup")
        close = float(df["close"].iloc[i])
        position = (context or {}).get("position")
        side = position.get("side", "long") if position else None

        if side == "short":
            _, cover = self.short_signals(df)
            if cover[i]:
                return Decision(Signal.COVER, self.cover_reason, atr=atr_value)
            return Decision(Signal.HOLD, "kein Signal", atr=atr_value)

        buy, sell = self.signals(df)
        flat_with_shorts = context is not None and side is None and self.cfg.allow_short
        if sell[i] and not flat_with_shorts:
            return Decision(Signal.SELL, self.sell_reason, atr=atr_value)
        if buy[i]:
            return self._entry(Signal.BUY, self.buy_reason, close, atr_value)
        if flat_with_shorts:
            short, _ = self.short_signals(df)
            if short[i]:
                return self._entry(Signal.SHORT, self.short_reason, close, atr_value)
        return Decision(Signal.HOLD, "kein Signal", atr=atr_value)

    def _entry(self, signal: Signal, reason: str, close: float, atr_value: float) -> Decision:
        if atr_value <= 0:
            return Decision(Signal.HOLD, "ATR ist null, kein sinnvoller Stop", atr=atr_value)
        sign = -1 if signal == Signal.SHORT else 1
        stop = close - sign * self.stop_distance(close, atr_value)
        target = self.target_distance(close, atr_value)
        tp = close + sign * target if target is not None else None
        return Decision(signal, reason, stop_loss=stop, take_profit=tp, atr=atr_value)

    def stop_distance(self, price: float, atr_value: float) -> float:
        """Abstand des Stop-Loss vom Kurs: stop_atr_mult x ATR, aber mindestens min_stop_pct % vom Kurs."""
        by_atr = self.cfg.stop_atr_mult * atr_value
        floor = self.cfg.min_stop_pct / 100.0 * price
        return max(by_atr, floor)

    # Strategien mit eigenem Stop (z. B. knapp hinter einer Zone) liefern ihn je Kerze als Spalten (Long, Short).
    # Das Ziel liegt dann bei `target_rr` mal dem Risiko (0 = wie sonst ueber take_profit_atr_mult).
    stop_columns: tuple[str, str] | None = None
    # Spalte einer Linie, an der der Stop nachgezogen wird (z. B. die SuperTrend-Linie) statt ATR-Abstand
    trail_line: str | None = None
    # Fuer den Chart: Linien, die anfangs sichtbar sind (None = Standardregel), Linien mit Farbe je Kerze
    # {Linie: Spalte mit 1 = long, 0 = short} und Signal-Marken [(Spalte, "long"|"short", Spalte mit Text)].
    chart_visible: tuple[str, ...] | None = None
    line_direction: dict[str, str] = {}
    chart_markers: list[tuple[str, str, str | None]] = []
    # Spalten mit zwingenden Ausstiegen (Long schliessen, Short schliessen), z. B. die X-Marken von SuperTrend AI.
    # Im Ensemble schliessen sie jeden laufenden Trade, unabhaengig von den Stimmen.
    exit_columns: tuple[str, str] | None = None
    default_target_rr: float = 0.0
    # Strategie setzt Stop und Ziel selbst (SuperTrend AI: Stop an der Linie, kein festes Ziel). Dann gelten weder
    # Mindest-Stop noch Ziel aus den Einstellungen, und der Kostenfilter prueft ihre Einstiege nicht.
    own_exits: bool = False

    def cost_check(self, trigger: str) -> bool:
        """Soll der Kostenfilter einen Einstieg mit diesem Ausloeser pruefen?"""
        return not self.own_exits

    def trigger_at(self, df: pd.DataFrame, i: int, side: str) -> str:
        """Welche Strategie loest den Einstieg an Kerze i aus? Bei Einzelstrategien sie selbst."""
        return self.name

    def trail_line_for(self, trigger: str) -> str | None:
        """Linie fuer den Trailing-Stop eines Trades mit diesem Ausloeser (None = Trailing aus den Einstellungen)."""
        return self.trail_line

    def entry_plan(self, df: pd.DataFrame, i: int, side: str, entry: float, atr_value: float) -> tuple[float, float | None]:
        """Stop und Ziel fuer einen Einstieg an Kerze i zum Kurs `entry`."""
        sign = -1 if side == "short" else 1
        if self.stop_columns is not None:
            level = df[self.stop_columns[0 if side == "long" else 1]].iloc[i]
            if level == level and sign * (entry - float(level)) > 0 and self.own_exits:
                dist = max(abs(entry - float(level)), 0.1 * atr_value)
                rr = float(self.p("target_rr", self.default_target_rr) or 0)
                return entry - sign * dist, (entry + sign * rr * dist if rr > 0 else None)
            if level == level and sign * (entry - float(level)) > 0:
                # Mindestabstand, damit ein Docht direkt am Kurs keine riesige Position erzeugt
                dist = max(abs(entry - float(level)), 0.1 * atr_value, entry * self.cfg.min_stop_pct / 100.0)
                rr = float(self.p("target_rr", self.default_target_rr) or 0)
                if rr > 0:
                    return entry - sign * dist, entry + sign * rr * dist
                td = self.target_distance(entry, atr_value)
                return entry - sign * dist, (entry + sign * td if td is not None else None)
        stop = entry - sign * self.stop_distance(entry, atr_value)
        td = self.target_distance(entry, atr_value)
        return stop, (entry + sign * td if td is not None else None)

    def target_distance(self, price: float, atr_value: float) -> float | None:
        """Abstand des Take-Profit. Behaelt das eingestellte Verhaeltnis zum Stop, auch wenn der Mindestabstand greift."""
        tp_mult = self.cfg.take_profit_atr_mult
        if tp_mult <= 0:
            return None
        if self.cfg.stop_atr_mult <= 0:
            return tp_mult * atr_value
        return self.stop_distance(price, atr_value) * tp_mult / self.cfg.stop_atr_mult

    def explain(self, df: pd.DataFrame, i: int = -1) -> list[str]:
        """Kurze Saetze fuer das Dashboard: was die Strategie an der letzten Kerze sieht."""
        try:
            buy, _, short, _ = self.all_signals(df)
        except NotImplementedError:
            return []
        out = [f"Long-Einstiegssignal: {'ja' if buy[i] else 'nein'}"]
        if short is not None:
            out.append(f"Short-Einstiegssignal: {'ja' if short[i] else 'nein'}")
        return out

    def _trend_text(self, row) -> tuple[list[str], bool, bool]:
        """Erklaert den Trendfilter. Gibt (Saetze, Long erlaubt, Short erlaubt) zurueck."""
        trend = self.p("trend_ema", 0)
        if trend <= 0 or "ema_trend" not in row.index or row["ema_trend"] != row["ema_trend"]:
            return [], True, True
        above = row["close"] > row["ema_trend"]
        side = "nur Long" if above else ("nur Short" if self.cfg.allow_short else "kein Long")
        return [f"Trend: Kurs {'ueber' if above else 'unter'} EMA {trend} ({fmt_price(row['ema_trend'])}) -> {side} moeglich"], above, not above

    def export_state(self) -> dict:
        """Strategie-Zustand, der mit dem Bot gespeichert wird (z. B. Zaehler der KI-Aufrufe)."""
        return {}

    def import_state(self, state: dict) -> None:
        return None

    def trailing_distance(self, price: float, atr_value: float) -> float:
        """Abstand des Trailing-Stops: fest trailing_pct % vom Kurs, sonst wie der Stop."""
        if self.cfg.trailing_pct > 0:
            return self.cfg.trailing_pct / 100.0 * price
        return self.stop_distance(price, atr_value)

    # Bot und Backtest uebernehmen einen neuen Trailing-Stop nur, wenn er naeher am Kurs liegt als der alte:
    # bei Long nur hoeher, bei Short nur tiefer. Er wandert also nie zurueck.
    def trailing_stop(self, highest_price: float, atr_value: float) -> float:
        return highest_price - self.trailing_distance(highest_price, atr_value)

    def trailing_stop_short(self, lowest_price: float, atr_value: float) -> float:
        return lowest_price + self.trailing_distance(lowest_price, atr_value)


def fmt_price(x: float) -> str:
    a = abs(x)
    return f"{x:.2f}" if a >= 100 else f"{x:.4f}" if a >= 1 else f"{x:.6f}"


def cross_up(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a.shift(1) <= b.shift(1)) & (a > b)).to_numpy()


def cross_down(a: pd.Series, b: pd.Series) -> np.ndarray:
    return ((a.shift(1) >= b.shift(1)) & (a < b)).to_numpy()
