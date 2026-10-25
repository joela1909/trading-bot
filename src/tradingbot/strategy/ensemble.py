from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import DaytradingConfig, StrategyConfig
from ..indicators import atr
from ..models import Decision, Signal
from .base import Strategy

# Strategien, die ein Ensemble standardmaessig fragt: alles im REGISTRY ausser dem Ensemble selbst und tsmom
# (fuer hoehere Zeitrahmen gedacht; ueber `members` trotzdem waehlbar).
EXCLUDED = ("ensemble",)
NOT_BY_DEFAULT = ("tsmom", "smc", "supertrend_ai", "reversal_zones", "pdh_pdl_sweep", "anchored_vwap", "trend_signals")
# nur im Backtest ausfuehrbar, nie im Ensemble
EXCLUDED += ("anchored_vwap",)


class EnsembleStrategy(Strategy):
    """Alle Strategien gleichzeitig: Jede liefert ihr Signal, ein Schiedsrichter entscheidet.

    Schiedsrichter "rules" (Standard): Einstieg, wenn mindestens `min_votes` Strategien dieselbe Richtung sagen
    und mehr dafuer als dagegen sind. Ausstieg nur, wenn die Gegenrichtung so eine Mehrheit hat, sonst ueber
    Stop-Loss, Take-Profit und Trailing-Stop.

    `solo_member`: Eine ausgewaehlte Strategie, deren Signal allein reicht. Gibt sie Long oder Short, wird
    eingestiegen, egal was die anderen sagen. Sagt sie nichts, entscheidet die Mehrheit wie oben.

    Die eigenen Ausstiegssignale der Strategien zaehlen bewusst nicht: Bei Umkehr-Strategien sind das Zustaende
    ("Kurs ueber der Mittellinie"), die nach einem Trend-Einstieg fast immer gelten. Mitgezaehlt warfen sie im
    Papierhandel 14 von 17 Trades schon nach einer Kerze wieder raus.

    Konfiguration (config.yaml, Abschnitt strategy):
        name: ensemble
        min_votes: 2
        solo_member: opening_range   # optional: deren Signal reicht allein, die anderen koennen es nicht aufhalten
        members: [vwap_reversion, opening_range, ...]   # optional, Standard: alle
        member_params: {vwap_reversion: {vwap_dev_atr: 3.0}}   # optional je Strategie
    """

    name = "ensemble"
    buy_reason = "Mehrheit fuer Long"
    sell_reason = "Mehrheit fuer Short"
    short_reason = "Mehrheit fuer Short"
    cover_reason = "Mehrheit fuer Long"
    param_grid = {"min_votes": [1, 2, 3]}

    def __init__(self, cfg: StrategyConfig, session: DaytradingConfig | None = None, timeframe: str | None = None):
        super().__init__(cfg, session, timeframe)
        from . import REGISTRY  # spaeter Import: das REGISTRY enthaelt diese Klasse selbst

        names = list(self.p("members", None) or [n for n in REGISTRY if n not in EXCLUDED + NOT_BY_DEFAULT])
        unknown = [n for n in names if n not in REGISTRY or n in EXCLUDED]
        if unknown:
            raise ValueError(f"Ensemble: unbekannte Strategie(n) {unknown}. Verfuegbar: {sorted(set(REGISTRY) - set(EXCLUDED))}")
        solo = self.p("solo_member", None) or None
        if solo is not None and solo not in names:
            raise ValueError(f"Ensemble: {solo} darf nur allein einsteigen, wenn es auch Mitglied ist")
        self.solo_member: str | None = solo
        params = self.p("member_params", None) or {}
        self.members: list[Strategy] = []
        for n in names:
            member_cfg = cfg.model_copy(update={"name": n, **(params.get(n) or {})})
            self.members.append(REGISTRY[n](member_cfg, session, timeframe))
        # Linien fuer den Chart: die der Mitglieder, nur Tages-VWAP und Trend-EMA sind anfangs sichtbar.
        # Mitglieder mit eigener Darstellung (z. B. SuperTrend AI: farbige Linie, Signal-Marken) behalten sie.
        visible = [c for m in self.members for c in (m.chart_visible or ())]
        self.chart_visible = tuple(visible) + ("vwap", "ema_trend") if visible else None
        self.line_direction = {k: v for m in self.members for k, v in m.line_direction.items()}
        self.chart_markers = [mk for m in self.members for mk in m.chart_markers]
        seen: set[str] = set()
        self.chart_lines = []
        for m in self.members:
            for col, label, style in m.chart_lines:
                if col in seen:
                    continue
                seen.add(col)
                self.chart_lines.append((col, label, style))

    @property
    def min_votes(self) -> int:
        return max(1, int(self.p("min_votes", 2)))

    @property
    def warmup_bars(self) -> int:
        return max(m.warmup_bars for m in self.members)

    # Jede Strategie rechnet auf den Rohdaten, ihre Signale landen als Spalten v_<name>_<art> im Ergebnis.
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["atr"] = atr(out, self.cfg.atr_period)
        for m in self.members:
            p = m.prepare(df)
            buy, sell, short, cover = m.all_signals(p)
            out[f"v_{m.name}_buy"] = buy
            out[f"v_{m.name}_sell"] = sell
            if short is not None:
                out[f"v_{m.name}_short"] = short
                out[f"v_{m.name}_cover"] = cover
            extra = list(m.line_direction.values()) + [c for mk in m.chart_markers for c in (mk[0], mk[2]) if c]
            extra += list(m.exit_columns or ()) + list(m.stop_columns or ())
            for col in [c for c, _, _ in m.chart_lines] + extra:
                if col in p.columns and col not in out.columns:
                    out[col] = p[col].to_numpy()
        return out

    def _add_indicators(self, df: pd.DataFrame) -> None:  # alles passiert in prepare
        return None

    def _votes(self, df: pd.DataFrame, kind: str) -> np.ndarray:
        total = np.zeros(len(df), dtype=int)
        for m in self.members:
            col = f"v_{m.name}_{kind}"
            if col in df.columns:
                total += df[col].to_numpy().astype(int)
        return total

    def _solo(self, df: pd.DataFrame, kind: str) -> np.ndarray:
        """Hat die Solo-Strategie diese Stimme abgegeben?"""
        col = f"v_{self.solo_member}_{kind}"
        if self.solo_member is None or col not in df.columns:
            return np.zeros(len(df), dtype=bool)
        return df[col].to_numpy().astype(bool)

    def _sides(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Je Kerze: gewinnt Long bzw. Short?

        Hat die Solo-Strategie ein Signal, gilt es, gleich wie die anderen stimmen. Sonst entscheidet die Mehrheit:
        mindestens min_votes Stimmen und mehr dafuer als dagegen."""
        k = self.min_votes
        long_v, short_v = self._votes(df, "buy"), self._votes(df, "short")
        solo_long, solo_short = self._solo(df, "buy"), self._solo(df, "short")
        solo_long, solo_short = solo_long & ~solo_short, solo_short & ~solo_long
        solo_any = solo_long | solo_short
        long_win = solo_long | (~solo_any & (long_v >= k) & (long_v > short_v))
        short_win = solo_short | (~solo_any & (short_v >= k) & (short_v > long_v))
        return long_win, short_win

    def _forced_exits(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Zwingende Ausstiege der Mitglieder (z. B. X-Marken): (Long schliessen, Short schliessen)."""
        ex_l = np.zeros(len(df), dtype=bool)
        ex_s = np.zeros(len(df), dtype=bool)
        for m in self.members:
            if m.exit_columns:
                cl, cs = m.exit_columns
                if cl in df.columns:
                    ex_l |= df[cl].to_numpy().astype(bool)
                if cs in df.columns:
                    ex_s |= df[cs].to_numpy().astype(bool)
        return ex_l, ex_s

    def signals(self, df):
        long_win, short_win = self._sides(df)
        ok = (df["atr"] > 0).to_numpy()
        ex_l, _ = self._forced_exits(df)
        return long_win & ok, short_win | ex_l  # Long schliessen, wenn Short gewinnt oder eine X-Marke kommt

    def short_signals(self, df):
        long_win, short_win = self._sides(df)
        ok = (df["atr"] > 0).to_numpy()
        _, ex_s = self._forced_exits(df)
        return short_win & ok, long_win | ex_s  # Short schliessen, wenn Long gewinnt oder eine X-Marke kommt

    def trigger_at(self, df: pd.DataFrame, i: int, side: str) -> str:
        """Die Solo-Strategie, wenn sie gestimmt hat, sonst alle Strategien mit Stimme fuer diese Richtung."""
        names = self.voters(df, i, "short" if side == "short" else "buy")
        if self.solo_member in names:
            return self.solo_member
        return "+".join(names) or self.name

    def _member_for(self, trigger: str) -> Strategy | None:
        # Stop und Ziel kommen von der ausloesenden Strategie (bei mehreren: der ersten)
        by_name = {m.name: m for m in self.members}
        for name in trigger.split("+"):
            if name in by_name:
                return by_name[name]
        return None

    def entry_plan(self, df: pd.DataFrame, i: int, side: str, entry: float, atr_value: float) -> tuple[float, float | None]:
        m = self._member_for(self.trigger_at(df, i, side))
        if m is not None:
            return m.entry_plan(df, i, side, entry, atr_value)
        return super().entry_plan(df, i, side, entry, atr_value)

    def cost_check(self, trigger: str) -> bool:
        m = self._member_for(trigger)
        return m.cost_check(trigger) if m is not None else True

    def trail_line_for(self, trigger: str) -> str | None:
        # Hat eine Strategie mit eigener Linie (SuperTrend AI) ausgeloest, zieht der Stop an ihrer Linie nach
        by_name = {m.name: m for m in self.members}
        for name in trigger.split("+"):
            m = by_name.get(name)
            if m is not None and m.trail_line:
                return m.trail_line
        return None

    def voters(self, df: pd.DataFrame, i: int, kind: str) -> list[str]:
        return [m.name for m in self.members if f"v_{m.name}_{kind}" in df.columns and bool(df[f"v_{m.name}_{kind}"].iloc[i])]

    def decide(self, df: pd.DataFrame, i: int = -1, context: dict | None = None) -> Decision:
        d = super().decide(df, i, context)
        idx = i if i >= 0 else len(df) + i
        # Ein- und Ausstieg haben dieselbe Ursache: die Stimmen fuer Long bzw. Short
        kind = {Signal.BUY: "buy", Signal.SHORT: "short", Signal.SELL: "short", Signal.COVER: "buy"}.get(d.signal)
        if kind:
            names = self.voters(df, idx, kind)
            d.reason = f"{len(names)} von {len(self.members)} Strategien: {', '.join(names)}"
            if self.solo_member in names:
                d.reason += f" ({self.solo_member} reicht allein)"
            if d.signal in (Signal.SELL, Signal.COVER):
                ex_l, ex_s = self._forced_exits(df.iloc[idx:idx + 1])
                if (d.signal == Signal.SELL and ex_l[0]) or (d.signal == Signal.COVER and ex_s[0]):
                    d.reason = "X-Marke: Umkehrwarnung (WaveTrend), Trade geschlossen"
        return d

    def explain(self, df: pd.DataFrame, i: int = -1) -> list[str]:
        idx = i if i >= 0 else len(df) + i
        if idx < 0 or df["atr"].iloc[idx] != df["atr"].iloc[idx]:
            return ["Indikatoren laden noch (Warmup)"]
        k, n = self.min_votes, len(self.members)
        long_names, short_names = self.voters(df, idx, "buy"), self.voters(df, idx, "short")
        out = [
            f"Schiedsrichter (Regeln): Einstieg ab {k} von {n} Stimmen in eine Richtung, mehr dafuer als dagegen. "
            f"Ausstieg erst bei so einer Mehrheit fuer die Gegenrichtung, sonst ueber Stop-Loss, Ziel oder Trailing-Stop",
            f"Long-Stimmen: {len(long_names)}" + (f" ({', '.join(long_names)})" if long_names else ""),
        ]
        if self.cfg.allow_short:
            out.append(f"Short-Stimmen: {len(short_names)}" + (f" ({', '.join(short_names)})" if short_names else ""))
        if self.solo_member:
            out.append(f"Signal von {self.solo_member} reicht allein, die anderen Stimmen koennen es nicht aufhalten")
        return out

    def member_votes(self, df: pd.DataFrame, i: int = -1) -> list[dict]:
        """Stimme jeder Strategie an Kerze i, fuer die Strategie-Seite der App."""
        idx = i if i >= 0 else len(df) + i
        out = []
        for m in self.members:
            def flag(kind: str) -> bool:
                col = f"v_{m.name}_{kind}"
                return col in df.columns and bool(df[col].iloc[idx])

            vote = "long" if flag("buy") else "short" if flag("short") else None
            out.append({"strategy": m.name, "vote": vote, "solo": m.name == self.solo_member})
        return out
