import numpy as np
import pandas as pd

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.config import StrategyConfig
from tradingbot.strategy import build_strategy
from tradingbot.synthetic import make_series


def _bars(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["timestamp"] = (1_700_000_000_000 + np.arange(len(df)) * 3_600_000).astype("int64")
    df["volume"] = 1.0
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def _strategy(**kw):
    return build_strategy(StrategyConfig(name="smc", allow_short=True, atr_period=3, smc_swing=2, **kw), timeframe="1h")


def test_sweep_of_swing_low_goes_long_with_stop_under_wick():
    flat = [[100, 101, 99, 100]] * 120
    # Swing-Tief bei 95 (Kerze 125), dann Erholung, dann Docht auf 94 mit Schluss wieder ueber 95
    path = [[100, 100.5, 98, 98.5], [98.5, 99, 96, 96.5], [96.5, 97, 95, 96], [96, 98, 95.5, 97.5], [97.5, 99, 97, 98.5],
            [98.5, 99.5, 98, 99], [99, 99.2, 97, 97.5], [97.5, 97.8, 94, 96.5], [96.5, 98, 96, 97.5]]
    df = _strategy(smc_entry="sweep").prepare(_bars(flat + path))
    i = 120 + 7
    assert bool(df["smc_buy"].iloc[i]), df[["low", "close", "smc_sl"]].iloc[118:].to_string()
    assert df["smc_stop_long"].iloc[i] < 94 and df["smc_sl"].iloc[i - 1] == 95


def test_entry_plan_uses_structure_stop_and_rr_target():
    s = _strategy(smc_entry="sweep", target_rr=2.0, min_stop_pct=0.0)
    df = pd.DataFrame({"smc_stop_long": [95.0], "smc_stop_short": [np.nan]})
    stop, tp = s.entry_plan(df, 0, "long", 100.0, 1.0)
    assert stop == 95.0 and tp == 110.0
    stop, tp = s.entry_plan(df, 0, "short", 100.0, 1.0)  # kein Struktur-Stop -> ATR-Regel
    assert stop > 100 and tp is not None and tp < 100


def test_no_lookahead_all_modes():
    for mode in ("zone", "sweep", "both"):
        s = build_strategy(StrategyConfig(name="smc", allow_short=True, smc_entry=mode, smc_swing=3), timeframe="1h")
        df = make_series("random", 2500, 11)
        assert check_lookahead(s, df, checks=12).ok, mode
        rec = check_recursive(s, df, checks=12)
        assert rec.mismatch_pct < 25, (mode, rec.mismatch_pct)
