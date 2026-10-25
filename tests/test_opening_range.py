import numpy as np
import pandas as pd
import pytest

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.config import StrategyConfig
from tradingbot.models import Signal
from tradingbot.strategy import build_strategy
from tradingbot.synthetic import make_series

DAY0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
MIN = 60_000


def _bars(closes, start=DAY0, spread=0.2):
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "timestamp": (start + np.arange(len(closes)) * MIN).astype("int64"),
        "open": opens, "high": np.maximum(opens, closes) + spread, "low": np.minimum(opens, closes) - spread,
        "close": closes, "volume": 100.0,
    })


def _strategy(**kw):
    cfg = StrategyConfig(name="opening_range", allow_short=True, atr_period=5, orb_minutes=30, **kw)
    return build_strategy(cfg, timeframe="1m")


def _day(after):
    """30 Kerzen Spanne zwischen 99 und 101 (mit Dochten: ORH 101,2 / ORL 98,8), danach der gegebene Verlauf."""
    rng = [100, 101, 99, 100.5, 99.5] * 6
    return list(rng) + list(after)


def test_range_levels_and_targets():
    s = _strategy()
    df = s.prepare(_bars(_day([100] * 20)))
    assert np.isnan(df["orh"].iloc[29]), "waehrend der Spanne noch keine Werte"
    r = df.iloc[35]
    assert r["orh"] == pytest.approx(101.2) and r["orl"] == pytest.approx(98.8)
    assert r["orm"] == pytest.approx(100.0)
    assert r["or_t1_up"] == pytest.approx(101.2 + 0.5 * 2.4) and r["or_t1_down"] == pytest.approx(98.8 - 1.2)


def test_breakout_fires_once_until_price_returns_below_mid():
    # Ausbruch, bleibt oben (kein zweites Signal), faellt unter die Mitte (wieder scharf), zweiter Ausbruch
    after = [100.5, 101.5, 101.8, 102.0, 101.6, 101.9, 100.6, 99.8, 100.5, 101.6, 101.7]
    s = _strategy()
    df = s.prepare(_bars(_day(after)))
    buy, _ = s.signals(df)
    idx = list(np.flatnonzero(buy))
    assert idx == [31, 39], idx
    short, _ = s.short_signals(df)
    assert not short.any()


def test_short_breakout_and_exit_at_mid():
    after = [99.5, 98.5, 98.0, 98.3, 99.0, 100.2]
    s = _strategy()
    df = s.prepare(_bars(_day(after)))
    short, cover = s.short_signals(df)
    assert list(np.flatnonzero(short)) == [31]
    assert cover[35] and not cover[33], "Ausstieg erst, wenn der Kurs zurueck ueber die Mitte geht"
    d = s.decide(df, 31, context={"position": None})
    assert d.signal == Signal.SHORT and d.reason.startswith("Ausbruch unter")


def test_daily_bias_needs_target_one_against_the_trend():
    # Tag 1: Spanne um 90. Tag 2: Spanne um 100 -> Mitte hoeher -> Aufwaertstag.
    day1 = [90, 91, 89, 90.5, 89.5] * 6 + list(np.linspace(90, 100, 1440 - 30))  # ohne Kurssprung in Tag 2
    day2 = _day([98.5, 98.0, 99.5, 97.0])  # unter ORL (98,8), aber Ziel 1 unten = 97,6 erst bei 97,0
    s_free, s_bias = _strategy(), _strategy(orb_bias=True)
    bars = _bars(day1 + day2)
    first = len(day1)
    free_short = np.flatnonzero(s_free.short_signals(s_free.prepare(bars))[0])
    bias_df = s_bias.prepare(bars)
    bias_short = np.flatnonzero(s_bias.short_signals(bias_df)[0])
    assert bias_df["or_day_dir"].iloc[first + 31] == 1
    assert first + 30 in free_short, "ohne Bias: Short schon beim Bruch von ORL"
    assert first + 30 not in bias_short and first + 33 in bias_short, "mit Bias: erst bei Ziel 1 unten"


def test_custom_session_start_and_timezone():
    # Spanne 15:30-16:00 New York. Davor gibt es keine Werte, danach schon.
    s = _strategy(orb_start="15:30", orb_timezone="America/New_York")
    start = DAY0 + (20 * 60 + 30) * MIN  # 2026-01-01 20:30 UTC = 15:30 New York (Winterzeit)
    df = s.prepare(_bars(_day([100] * 10), start=start))
    assert bool(df["or_in_range"].iloc[0]) and not bool(df["or_in_range"].iloc[30])
    assert df["orh"].iloc[32] == pytest.approx(101.2)


def test_no_lookahead_and_live_window_matches_backtest():
    for bias in (False, True):
        s = build_strategy(StrategyConfig(name="opening_range", allow_short=True, orb_bias=bias), timeframe="1m")
        df = make_series("meanrev", 4000, 3, tf_ms=MIN)
        assert check_lookahead(s, df, checks=20).ok
        assert check_recursive(s, df, checks=20).ok


def test_europe_and_us_sessions_hand_over():
    # Montag 2026-01-05. Europa 09:00 Berlin = 08:00 UTC, USA 09:30 New York = 14:30 UTC (Winterzeit)
    s = _strategy(orb_sessions=["09:00 Europe/Berlin", "09:30 America/New_York"], orb_weekdays=True)
    monday = DAY0 + 4 * 1440 * MIN
    closes = 100 + np.sin(np.arange(3 * 1440) / 40.0)
    df = s.prepare(_bars(closes, start=monday))
    starts = [i for i in range(1, len(df)) if df["or_in_range"].iloc[i] and not df["or_in_range"].iloc[i - 1]]
    assert [i % 1440 for i in starts[:4]] == [8 * 60, 14 * 60 + 30, 8 * 60, 14 * 60 + 30]
    assert df["or_session"].iloc[8 * 60 + 40] == 0 and df["or_session"].iloc[14 * 60 + 40] == 1
    assert df["or_session"].iloc[1440 + 7 * 60] == 1, "nachts gilt noch die US-Spanne"
    assert any("USA" in line for line in s.explain(df, 14 * 60 + 40))


def test_weekdays_only_skips_weekend_ranges():
    s = _strategy(orb_sessions=["09:00 Europe/Berlin"], orb_weekdays=True)
    saturday = DAY0 + 2 * 1440 * MIN  # 2026-01-03
    df = s.prepare(_bars(100 + np.sin(np.arange(2 * 1440) / 30.0), start=saturday))
    assert not df["or_in_range"].any()
    assert df["orh"].isna().all()


def test_two_sessions_no_lookahead():
    s = build_strategy(StrategyConfig(name="opening_range", allow_short=True, orb_sessions=["09:00 Europe/Berlin", "09:30 America/New_York"],
                                      orb_weekdays=True), timeframe="1m")
    df = make_series("meanrev", 9000, 4, tf_ms=MIN)
    assert check_lookahead(s, df, checks=10).ok
    assert check_recursive(s, df, checks=10).ok
