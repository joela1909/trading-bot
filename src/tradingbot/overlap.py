"""Wie oft sind mehrere Strategien gleichzeitig einer Meinung? Auswertung auf Basis der Backtests."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
import pandas as pd

from .backtest import Backtester
from .config import PaperConfig, ProtectionConfig, RiskConfig, StrategyConfig
from .risk import RiskManager
from .strategy import REGISTRY, build_strategy


@dataclass
class OverlapResult:
    names: list[str]
    bars: int
    window: int
    buy_events: dict[str, int]
    sell_events: dict[str, int]
    buy_k: np.ndarray
    sell_k: np.ndarray
    buy_window_k: np.ndarray
    long_k: np.ndarray
    entry_k: np.ndarray
    entry_window_k: np.ndarray
    time_long: dict[str, float]
    state_agreement: dict[tuple[str, str], float] = field(default_factory=dict)
    entry_overlap: dict[tuple[str, str], float] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.names)

    def pct(self, counts: np.ndarray, k: int) -> float:
        return float(counts[k:].sum()) / self.bars * 100.0 if self.bars else 0.0


def _recent(events: np.ndarray, window: int) -> np.ndarray:
    """True, wenn in den letzten `window` Kerzen (inklusive der aktuellen) mindestens ein Ereignis war. Nur Vergangenheit."""
    return np.convolve(events.astype(int), np.ones(window, dtype=int))[: len(events)] > 0


def analyze_symbol(
    df: pd.DataFrame,
    symbol: str,
    names: list[str],
    base_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    paper_cfg: PaperConfig,
    protections: ProtectionConfig | None,
    window: int = 6,
) -> OverlapResult:
    n = len(names)
    strategies = [build_strategy(base_cfg.model_copy(update={"name": name})) for name in names]
    start = max(s.warmup_bars for s in strategies)
    if len(df) < start + 50:
        raise ValueError(f"{symbol}: zu wenig Kerzen ({len(df)}), mindestens {start + 50} noetig")
    total = len(df) - start

    buys, sells, longs = [], [], []
    for strat in strategies:
        prepared = strat.prepare(df)
        buy, sell = strat.signals(prepared)
        bt = Backtester(strat, RiskManager(risk_cfg), strat.cfg, paper_cfg, protections_cfg=protections)
        res = bt.run(prepared, symbol, start=start, end=len(df), prepared=True, signals=(buy, sell))
        buys.append(buy[start:])
        sells.append(sell[start:])
        longs.append(res.in_position)

    buy_m, sell_m, long_m = np.array(buys), np.array(sells), np.array(longs)
    recent_m = np.array([_recent(b, window) for b in buy_m])
    entry_m = long_m & ~np.concatenate([np.zeros((n, 1), dtype=bool), long_m[:, :-1]], axis=1)
    recent_entry_m = np.array([_recent(e, window) for e in entry_m])

    def distribution(mask: np.ndarray) -> np.ndarray:
        return np.bincount(mask.sum(axis=0), minlength=n + 1)

    agreement, overlap = {}, {}
    for i, j in combinations(range(n), 2):
        agreement[(names[i], names[j])] = float((long_m[i] == long_m[j]).mean() * 100.0)
        union = (recent_entry_m[i] | recent_entry_m[j]).sum()
        overlap[(names[i], names[j])] = float((recent_entry_m[i] & recent_entry_m[j]).sum() / union * 100.0) if union else 0.0

    return OverlapResult(
        names=names,
        bars=total,
        window=window,
        buy_events={nm: int(buy_m[i].sum()) for i, nm in enumerate(names)},
        sell_events={nm: int(sell_m[i].sum()) for i, nm in enumerate(names)},
        buy_k=distribution(buy_m),
        sell_k=distribution(sell_m),
        buy_window_k=distribution(recent_m),
        long_k=distribution(long_m),
        entry_k=distribution(entry_m),
        entry_window_k=distribution(recent_entry_m),
        time_long={nm: float(long_m[i].mean() * 100.0) for i, nm in enumerate(names)},
        state_agreement=agreement,
        entry_overlap=overlap,
        symbols=[symbol],
    )


def combine(results: list[OverlapResult]) -> OverlapResult:
    first = results[0]
    total_bars = sum(r.bars for r in results)

    def add(attr: str) -> np.ndarray:
        return sum(getattr(r, attr) for r in results)

    def weighted(attr: str) -> dict:
        return {k: sum(getattr(r, attr)[k] * r.bars for r in results) / total_bars for k in getattr(first, attr)}

    return OverlapResult(
        names=first.names,
        bars=total_bars,
        window=first.window,
        buy_events={k: sum(r.buy_events[k] for r in results) for k in first.names},
        sell_events={k: sum(r.sell_events[k] for r in results) for k in first.names},
        buy_k=add("buy_k"),
        sell_k=add("sell_k"),
        buy_window_k=add("buy_window_k"),
        long_k=add("long_k"),
        entry_k=add("entry_k"),
        entry_window_k=add("entry_window_k"),
        time_long=weighted("time_long"),
        state_agreement=weighted("state_agreement"),
        entry_overlap=weighted("entry_overlap"),
        symbols=[s for r in results for s in r.symbols],
    )


def format_overlap(r: OverlapResult, synthetic: bool = False, per_symbol: list[OverlapResult] | None = None) -> str:
    n = r.n
    lines: list[str] = []
    if synthetic:
        lines += ["> **ACHTUNG: SYNTHETISCHE DATEN.** Die Zahlen stammen aus kuenstlichen Kursreihen und gelten nicht fuer echte Maerkte.", ""]
    lines += [
        "# Uebereinstimmung der Strategien",
        "",
        f"- **Strategien ({n}):** {', '.join(r.names)} mit ihren Standardparametern",
        f"- **Daten:** {', '.join(r.symbols)}, zusammen {r.bars} ausgewertete Kerzen",
        f"- **Zeitfenster fuer Naeherungen:** {r.window} Kerzen rueckwaerts, nur Vergangenheit",
        "",
        "## Wie oft geben alle dasselbe Signal?",
        "",
        "| Frage | Alle " + str(n) + " | Mindestens " + str(max(n - 1, 2)) + " | Mindestens 3 | Mindestens 2 |",
        "|---|---|---|---|---|",
    ]

    def row(label: str, counts: np.ndarray) -> str:
        cells = [r.pct(counts, n), r.pct(counts, max(n - 1, 2)), r.pct(counts, 3), r.pct(counts, 2)]
        return f"| {label} | " + " | ".join(f"{c:.3f}%" for c in cells) + " |"

    lines += [
        row("Kaufsignal in derselben Kerze", r.buy_k),
        row(f"Kaufsignal innerhalb von {r.window} Kerzen", r.buy_window_k),
        row("Verkaufssignal in derselben Kerze", r.sell_k),
        row("Einstieg im Backtest in derselben Kerze", r.entry_k),
        row(f"Einstieg im Backtest innerhalb von {r.window} Kerzen", r.entry_window_k),
        row("Gleichzeitig in einer Position", r.long_k),
        "",
        f"Gleichzeitig ohne Position (alle {n} flach): **{r.long_k[0] / r.bars * 100:.1f}%** der Zeit.",
        "",
        "## Wie viele Strategien sind gleichzeitig in einer Position?",
        "",
        "| Anzahl | Anteil der Zeit |",
        "|---|---|",
    ]
    for k in range(n + 1):
        lines.append(f"| {k} | {r.long_k[k] / r.bars * 100:.1f}% |")

    lines += ["", "## Signale und Zeit in der Position je Strategie", "", "| Strategie | Kaufsignale | Verkaufssignale | Zeit in Position |", "|---|---|---|---|"]
    for nm in r.names:
        lines.append(f"| {nm} | {r.buy_events[nm]} | {r.sell_events[nm]} | {r.time_long[nm]:.1f}% |")

    lines += ["", "## Paarvergleich", "", "| Paar | Gleicher Zustand | Einstiege nahe beieinander |", "|---|---|---|"]
    for (a, b), v in sorted(r.state_agreement.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {a} und {b} | {v:.1f}% | {r.entry_overlap[(a, b)]:.1f}% |")
    lines += [
        "",
        "Gleicher Zustand heisst: beide in einer Position oder beide ohne Position. Einstiege nahe beieinander ist der Anteil der Kerzen,",
        f"in denen beide innerhalb der letzten {r.window} Kerzen eingestiegen sind, gemessen an allen Kerzen, in denen mindestens eine eingestiegen ist.",
    ]
    if per_symbol:
        lines += ["", "## Je Coin: alle Strategien gleichzeitig in einer Position", ""]
        for pr in per_symbol:
            lines.append(f"- {pr.symbols[0]}: {pr.long_k[pr.n] / pr.bars * 100:.2f}% der Zeit, mindestens 3 Strategien: {pr.pct(pr.long_k, 3):.1f}%")
    return "\n".join(lines) + "\n"
