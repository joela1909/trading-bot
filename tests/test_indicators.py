import numpy as np
import pandas as pd

from tradingbot.indicators import atr, ema, rsi
from tests.conftest import make_ohlcv


def test_ema_of_constant_series_is_constant():
    s = pd.Series([50.0] * 30)
    out = ema(s, 10)
    assert out.iloc[:9].isna().all()
    assert np.allclose(out.iloc[9:], 50.0)


def test_ema_follows_trend_direction():
    s = pd.Series(np.linspace(1, 100, 100))
    fast, slow = ema(s, 5), ema(s, 20)
    assert fast.iloc[-1] > slow.iloc[-1]


def test_rsi_rising_series_is_100_and_falling_is_0():
    up = pd.Series(np.arange(1, 40, dtype=float))
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi(up, 14).iloc[-1] == 100.0
    assert rsi(down, 14).iloc[-1] < 1.0


def test_rsi_bounded():
    rng = np.random.default_rng(1)
    s = pd.Series(100 + rng.normal(0, 1, 500).cumsum())
    r = rsi(s, 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_atr_positive_and_warmup():
    df = make_ohlcv(np.linspace(100, 110, 50))
    a = atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert (a.iloc[14:] > 0).all()
