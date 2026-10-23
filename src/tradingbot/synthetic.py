"""Kuenstliche Kursreihen, nur zum Testen des Werkzeugs. Sie sagen nichts ueber echte Maerkte."""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_MS = 3_600_000
START_TS = 1_577_836_800_000


def _to_ohlcv(log_price: np.ndarray, rng: np.random.Generator, start_ts: int, tf_ms: int, vol: float) -> pd.DataFrame:
    close = np.exp(log_price)
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, vol * 0.5, len(close)))
    high = np.maximum(open_, close) * (1 + wick)
    low = np.minimum(open_, close) * (1 - wick)
    ts = start_ts + np.arange(len(close)) * tf_ms
    return pd.DataFrame({"timestamp": ts.astype("int64"), "open": open_, "high": high, "low": low, "close": close, "volume": 100.0})


def make_series(kind: str, n: int = 20_000, seed: int = 1, start_price: float = 100.0, start_ts: int = START_TS, tf_ms: int = TF_MS, vol: float | None = None) -> pd.DataFrame:
    """kind: 'random' (Zufallsbewegung), 'trend' (Trendphasen mit Momentum), 'meanrev' (Rueckkehr zum Mittelwert)."""
    rng = np.random.default_rng(seed)
    # Schwankung pro Kerze waechst mit der Wurzel der Kerzenlaenge: Stundenkerzen 0,6 %, 5-Minuten-Kerzen etwa 0,17 %.
    vol = vol if vol is not None else 0.006 * (tf_ms / TF_MS) ** 0.5
    k = vol / 0.006
    if kind == "random":
        log_p = np.log(start_price) + np.cumsum(rng.normal(0, vol, n))
    elif kind == "trend":
        ret = np.zeros(n)
        drift = 0.0
        remaining = 0
        for t in range(1, n):
            if remaining == 0:
                drift = rng.choice([0.0006 * k, -0.0005 * k, 0.0])
                remaining = int(rng.integers(300, 1500))
            remaining -= 1
            ret[t] = drift + 0.12 * ret[t - 1] + rng.normal(0, vol)
        log_p = np.log(start_price) + np.cumsum(ret)
    elif kind == "meanrev":
        dev = np.zeros(n)
        for t in range(1, n):
            dev[t] = 0.96 * dev[t - 1] + rng.normal(0, vol * 1.6)
        log_p = np.log(start_price) + dev
    else:
        raise ValueError(f"Unbekannte Art: {kind}")
    return _to_ohlcv(log_p, rng, start_ts, tf_ms, vol)
