"""Prueft Strategien auf zwei typische Fehlerquellen, die Backtests unbemerkt verfaelschen.

1. Blick in die Zukunft: Ein Signal darf sich nicht aendern, wenn spaetere Kerzen hinzukommen.
2. Startabhaengigkeit: Der Live-Bot sieht nur die letzten Kerzen, der Backtest die ganze Historie.
   Beide muessen zum selben Signal kommen, sonst handelt der Bot anders als getestet.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .strategy import Strategy

RECURSIVE_TOLERANCE_PCT = 2.0


@dataclass
class LookaheadResult:
    ok: bool
    checked: int
    mismatches: list[int] = field(default_factory=list)


@dataclass
class RecursiveResult:
    ok: bool
    checked: int
    mismatch_pct: float
    window: int


def _sample_indices(start: int, end: int, count: int) -> list[int]:
    if end <= start:
        return []
    return sorted({int(x) for x in np.linspace(start, end, min(count, end - start + 1))})


def check_lookahead(strategy: Strategy, df: pd.DataFrame, checks: int = 30) -> LookaheadResult:
    full = strategy.prepare(df)
    full_buy, full_sell = strategy.signals(full)
    idxs = _sample_indices(strategy.warmup_bars + 5, len(df) - 1, checks)
    # zusaetzlich alle Stellen mit Signal, denn dort entscheidet sich ein Fehler
    signal_idxs = [int(i) for i in np.flatnonzero(full_buy | full_sell) if i > strategy.warmup_bars]
    idxs = sorted(set(idxs) | set(signal_idxs[:: max(1, len(signal_idxs) // checks)]))
    bad: list[int] = []
    for i in idxs:
        part = strategy.prepare(df.iloc[: i + 1].reset_index(drop=True))
        buy, sell = strategy.signals(part)
        if bool(buy[-1]) != bool(full_buy[i]) or bool(sell[-1]) != bool(full_sell[i]):
            bad.append(i)
    return LookaheadResult(ok=not bad, checked=len(idxs), mismatches=bad)


def check_recursive(strategy: Strategy, df: pd.DataFrame, checks: int = 60) -> RecursiveResult:
    """Vergleicht das Signal aus der ganzen Historie mit dem Signal aus nur den Kerzen, die der Live-Bot laedt."""
    window = strategy.warmup_bars + 10
    full = strategy.prepare(df)
    full_buy, full_sell = strategy.signals(full)
    candidates = _sample_indices(window, len(df) - 1, checks)
    signal_idxs = [int(i) for i in np.flatnonzero(full_buy | full_sell) if i >= window]
    idxs = sorted(set(candidates) | set(signal_idxs[:: max(1, len(signal_idxs) // checks)]))
    if not idxs:
        return RecursiveResult(ok=True, checked=0, mismatch_pct=0.0, window=window)
    mismatches = 0
    for i in idxs:
        part = strategy.prepare(df.iloc[i - window + 1 : i + 1].reset_index(drop=True))
        buy, sell = strategy.signals(part)
        if bool(buy[-1]) != bool(full_buy[i]) or bool(sell[-1]) != bool(full_sell[i]):
            mismatches += 1
    pct = mismatches / len(idxs) * 100.0
    return RecursiveResult(ok=pct <= RECURSIVE_TOLERANCE_PCT, checked=len(idxs), mismatch_pct=pct, window=window)
