import numpy as np
import pytest

from tradingbot.config import StrategyConfig
from tradingbot.strategy import build_strategy


def test_trailing_pct_keeps_fixed_distance_to_best_price():
    s = build_strategy(StrategyConfig(name="ema_cross", trailing_pct=1.0, stop_atr_mult=2.0))
    assert s.trailing_stop(200.0, 5.0) == pytest.approx(198.0), "1 % unter dem besten Kurs, ATR egal"
    assert s.trailing_stop_short(100.0, 5.0) == pytest.approx(101.0)
    off = build_strategy(StrategyConfig(name="ema_cross", trailing_pct=0.0, stop_atr_mult=2.0))
    assert off.trailing_stop(200.0, 5.0) == pytest.approx(190.0), "0 = wie bisher ueber den Stop-Abstand"


def test_trailing_with_ratchet_never_moves_back():
    """So rechnen Bot und Backtest: neuer Stop nur bei neuem Bestkurs und nur, wenn er naeher liegt (max/min)."""
    s = build_strategy(StrategyConfig(name="ema_cross", trailing_pct=1.0, stop_atr_mult=2.0, take_profit_atr_mult=0, min_stop_pct=0.0))
    prices = [100.0] * 400 + list(np.linspace(100, 110, 50)) + list(np.linspace(110, 109.5, 10)) + list(np.linspace(109.5, 112, 20)) + list(np.linspace(112, 100, 30))
    stops = []
    best = 105.0
    stop = best * 0.98
    for p in prices[400:]:
        if p > best:
            best = p
            stop = max(stop, s.trailing_stop(best, 0.5))
        stops.append(stop)
    assert all(b >= a for a, b in zip(stops, stops[1:])), "Long-Stop wandert nie nach unten"
    assert stops[-1] == pytest.approx(112 * 0.99)
