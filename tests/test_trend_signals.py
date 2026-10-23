import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest import Backtester
from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
from tradingbot.indicators import adx
from tradingbot.risk import RiskManager
from tradingbot.strategy import REGISTRY, build_strategy
from tradingbot.strategy.trend_signals import _atr_trail
from tradingbot.synthetic import make_series

BAR = 3_600_000


def _cfg(**kw):
    return StrategyConfig(**{**dict(name="trend_signals", allow_short=True), **kw})


def _swing(n_up=150, n_down=150):
    """Kurs faellt ruhig, steigt dann ruhig und faellt wieder."""
    close = np.concatenate([np.linspace(100, 70, n_down), np.linspace(70, 160, n_up), np.linspace(160, 100, n_down)])
    rng = np.random.default_rng(1)
    close = close + rng.normal(0, 0.3, len(close))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high, low = np.maximum(open_, close) + 0.5, np.minimum(open_, close) - 0.5
    return pd.DataFrame({"timestamp": 1_700_000_000_000 + np.arange(len(close)) * BAR, "open": open_, "high": high, "low": low, "close": close, "volume": 100.0})


def _ref_trail(high, low, close, atr, mult):
    """Zweite, bewusst anders geschriebene Fassung des Nachziehens, mit Listen statt Arrays."""
    trend, line = [], []
    fu = fl = prev_close = None
    t = 0
    for h, l, c, a in zip(high, low, close, atr):
        if a != a:
            trend.append(0)
            line.append(float("nan"))
            continue
        mid = (h + l) / 2
        bu, bl = mid + mult * a, mid - mult * a
        if fu is None:
            fu, fl, t = bu, bl, (1 if c >= mid else -1)
        else:
            nfu = bu if (bu < fu or prev_close > fu) else fu
            nfl = bl if (bl > fl or prev_close < fl) else fl
            fu, fl = nfu, nfl
            if t == -1 and c > fu:
                t = 1
            elif t == 1 and c < fl:
                t = -1
        trend.append(t)
        line.append(fl if t == 1 else fu)
        prev_close = c
    return np.array(trend), np.array(line)


def test_trail_matches_an_independent_reference_and_ratchets():
    for seed in (1, 2, 3):
        df = make_series("random", 3000, seed)
        strat = build_strategy(_cfg())
        d = strat.prepare(df)
        t_ref, l_ref = _ref_trail(d["high"].tolist(), d["low"].tolist(), d["close"].tolist(), d["atr_trail"].tolist(), 2.0)
        assert np.array_equal(d["trail_trend"].to_numpy(), t_ref)
        assert np.allclose(d["trail_line"].to_numpy(), l_ref, equal_nan=True)
    d = build_strategy(_cfg()).prepare(_swing())
    t, line, close = d["trail_trend"].to_numpy(), d["trail_line"].to_numpy(), d["close"].to_numpy()
    for i in range(1, len(d)):
        if np.isnan(line[i]) or np.isnan(line[i - 1]) or t[i] != t[i - 1]:
            continue
        assert (line[i] >= line[i - 1] - 1e-9) if t[i] == 1 else (line[i] <= line[i - 1] + 1e-9), "die Linie zieht nur in Trendrichtung nach"
    assert (close[t == 1] > line[t == 1] - 1e-9).mean() > 0.95 and (close[t == -1] < line[t == -1] + 1e-9).mean() > 0.95


def test_signals_only_on_trend_flips_and_exits_on_the_opposite_flip():
    strat = build_strategy(_cfg())
    d = strat.prepare(_swing())
    up, down = strat._flips(d)
    buy, sell = strat.signals(d)
    short, cover = strat.short_signals(d)
    assert up.any() and down.any()
    assert not (buy & ~up).any() and not (short & ~down).any()
    assert np.array_equal(sell, down) and np.array_equal(cover, up), "Ausstieg bei jeder Gegendrehung, auch ohne Filter"


def test_filters_block_entries_but_never_exits():
    d_strat = build_strategy(_cfg())
    d = d_strat.prepare(make_series("random", 4000, 2))
    base_buy, base_sell = d_strat.signals(d)
    assert base_buy.sum() > 5
    strict = build_strategy(_cfg(adx_min=99))
    ds = strict.prepare(make_series("random", 4000, 2))
    buy, sell = strict.signals(ds)
    assert not buy.any() and np.array_equal(sell, base_sell)
    with_ema = build_strategy(_cfg(confirm_ema=100))
    de = with_ema.prepare(make_series("random", 4000, 2))
    b = with_ema.signals(de)[0]
    assert b.sum() < base_buy.sum() and (de["close"].to_numpy()[b] > de["ema_confirm"].to_numpy()[b]).all()
    q = build_strategy(_cfg(min_quality=4)).signals(build_strategy(_cfg(min_quality=4)).prepare(make_series("random", 4000, 2)))[0]
    assert q.sum() <= base_buy.sum()
    sq = build_strategy(_cfg(block_squeeze=True))
    dq = sq.prepare(make_series("random", 4000, 2))
    assert not (sq.signals(dq)[0] & dq["squeeze"].to_numpy()).any()


def test_relative_volatility_filter():
    strat = build_strategy(_cfg(max_relative_atr=0.5))
    d = strat.prepare(make_series("random", 4000, 2))
    buy = strat.signals(d)[0]
    assert (d["rel_atr"].to_numpy()[buy] <= 0.5).all()


def test_contrarian_mode_buys_oversold_extremes_and_exits_at_the_mid_line():
    strat = build_strategy(_cfg(mode=1))
    d = strat.prepare(make_series("meanrev", 5000, 4))
    buy, sell = strat.signals(d)
    short, cover = strat.short_signals(d)
    assert buy.any() and short.any()
    assert (d["close"].to_numpy()[buy] < d["bb_lo"].to_numpy()[buy]).all() and (d["rsi_sig"].to_numpy()[buy] < 30).all()
    assert (d["close"].to_numpy()[short] > d["bb_up"].to_numpy()[short]).all() and (d["rsi_sig"].to_numpy()[short] > 70).all()


def test_adx_is_bounded_and_higher_in_a_trend_than_in_a_range():
    trend = _swing(300, 1)
    flat = make_series("meanrev", 300, 3)
    a_trend, a_flat = adx(trend, 14).dropna(), adx(flat, 14).dropna()
    assert ((a_trend >= 0) & (a_trend <= 100)).all() and a_trend.iloc[-1] > a_flat.iloc[-1] + 10


def test_backtest_runs_with_both_sides_and_registry_knows_it():
    assert "trend_signals" in REGISTRY
    cfg = _cfg(trailing_stop=False)
    strat = build_strategy(cfg)
    bt = Backtester(strat, RiskManager(RiskConfig()), cfg, PaperConfig(starting_balance=1000, fee_pct=0.0, slippage_pct=0.0))
    res = bt.run(make_series("trend", 6000, 5), "X/USDT")
    assert res.n_trades > 10 and {t.side for t in res.trades} == {"long", "short"}
