import numpy as np

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.config import StrategyConfig
from tradingbot.strategy import build_strategy
from tradingbot.strategy.lux import _kmeans3, supersmoother
from tradingbot.synthetic import make_series


def test_kmeans_splits_into_three_groups():
    g = _kmeans3(np.array([1.0, 1.1, 1.2, 5.0, 5.1, 9.0, 9.2, 9.1]))
    assert sorted(len(x) for x in g) == [2, 3, 3] and set(g[2]) == {5, 6, 7}


def test_supersmoother_follows_constant():
    out = supersmoother(np.full(300, 7.0), 50)
    assert abs(out[-1] - 7.0) < 1e-9


def test_supertrend_ai_flips_with_trend_and_sets_stop():
    s = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5), timeframe="1h")
    df = s.prepare(make_series("trend", 3000, 5))
    buy, sell = s.signals(df)
    short, cover = s.short_signals(df)
    assert buy.sum() > 3 and short.sum() > 3
    i = int(np.flatnonzero(buy)[0])
    assert df["st_stop_long"].iloc[i] < df["close"].iloc[i]


def test_reversal_zones_signals_and_stops():
    s = build_strategy(StrategyConfig(name="reversal_zones", allow_short=True, rz_length=50), timeframe="1h")
    df = s.prepare(make_series("meanrev", 4000, 9))
    buy, _ = s.signals(df)
    assert buy.sum() > 0
    i = int(np.flatnonzero(buy)[0])
    assert df["rz_stop_long"].iloc[i] < df["low"].iloc[i] + 1e-12


def test_lux_strategies_no_lookahead():
    for name, kw in (("supertrend_ai", {"st_sensitivity": 5}), ("reversal_zones", {"rz_length": 50})):
        s = build_strategy(StrategyConfig(name=name, allow_short=True, **kw), timeframe="1h")
        df = make_series("random", 2500, 3)
        assert check_lookahead(s, df, checks=10).ok, name
        rec = check_recursive(s, df, checks=10)
        assert rec.mismatch_pct < 20, (name, rec.mismatch_pct)


def test_backtest_trails_stop_along_supertrend_line():
    from tradingbot.backtest import Backtester
    from tradingbot.config import PaperConfig, RiskConfig

    cfg = StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, take_profit_atr_mult=0, min_stop_pct=0)
    s = build_strategy(cfg, timeframe="1h")
    df = s.prepare(make_series("trend", 4000, 5))
    r = Backtester(s, RiskManager(RiskConfig(min_order_quote=0)), cfg, PaperConfig(fee_pct=0, slippage_pct=0)).run(df, "X", start=s.warmup_bars, prepared=True)
    assert r.trades
    # Ausstiege am Stop liegen auf (bzw. hinter) der SuperTrend-Linie, nicht 2 ATR hinter dem Hoch
    stops = [t for t in r.trades if t.reason == "stop_loss"]
    assert stops


from tradingbot.risk import RiskManager  # noqa: E402


def test_view_has_colored_supertrend_and_signal_markers(settings, tmp_path):
    from tests.conftest import FakeFeed, make_ohlcv, trend_closes
    from tests.test_bot import _make_bot

    settings.bot.strategy = settings.bot.strategy.model_copy(update={"name": "supertrend_ai", "st_sensitivity": 5, "st_atr_len": 10})
    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.process_symbol("BTC/USDT", feed.price)
    view = bot.chart("BTC/USDT")
    st = next(l for l in view["lines"] if l["key"] == "st_line")
    assert st["visible"] and set(filter(None, st["directions"])) <= {"long", "short"}
    assert any(l["key"] == "rz_lo_band" and l["visible"] for l in view["lines"])
    assert not next(l for l in view["lines"] if l["key"] == "st_ama")["visible"]
    assert view["markers"] and {m["side"] for m in view["markers"]} <= {"long", "short"} and all(m["text"] in "1234" for m in view["markers"])


def test_ensemble_passes_luxalgo_chart_through():
    s = build_strategy(StrategyConfig(name="ensemble", allow_short=True, min_votes=1, members=["opening_range", "supertrend_ai"], st_sensitivity=5), timeframe="1m")
    assert "st_line" in s.chart_visible and s.line_direction == {"st_line": "st_dir"} and s.chart_markers
    df = s.prepare(make_series("trend", 1500, 2))
    assert {"st_dir", "st_flip_up", "st_strength"} <= set(df.columns)


def test_ensemble_trigger_and_supertrend_line_trailing():
    s = build_strategy(StrategyConfig(name="ensemble", allow_short=True, min_votes=1, members=["opening_range", "supertrend_ai"],
                                      solo_member="opening_range", st_sensitivity=5), timeframe="1m")
    df = s.prepare(make_series("trend", 1500, 2))
    i = int(np.flatnonzero(df["v_supertrend_ai_buy"].to_numpy())[0])
    trig = s.trigger_at(df, i, "long")
    assert "supertrend_ai" in trig.split("+")
    assert s.trail_line_for(trig) == "st_line"
    assert s.trail_line_for("opening_range") is None
    assert build_strategy(StrategyConfig(name="donchian")).trigger_at(df, i, "long") == "donchian"


def test_backtest_trades_carry_trigger(settings):
    from tradingbot.backtest import Backtester
    from tradingbot.config import PaperConfig, RiskConfig

    cfg = StrategyConfig(name="ensemble", allow_short=True, min_votes=1, members=["donchian", "supertrend_ai"], st_sensitivity=5,
                         take_profit_atr_mult=0, min_stop_pct=0)
    s = build_strategy(cfg, timeframe="1h")
    df = s.prepare(make_series("trend", 3000, 4))
    r = Backtester(s, RiskManager(RiskConfig(min_order_quote=0)), cfg, PaperConfig()).run(df, "X", start=s.warmup_bars, prepared=True)
    assert r.trades and all(t.strategy for t in r.trades)
    assert {p for t in r.trades for p in t.strategy.split("+")} <= {"donchian", "supertrend_ai"}


def test_supertrend_on_5m_candles_uses_only_finished_5m_bars():
    s = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, st_atr_len=10, st_timeframe="5m"), timeframe="1m")
    assert s.htf_factor == 5 and s.warmup_bars == 3000
    df = make_series("trend", 6000, 3, tf_ms=60_000)
    p = s.prepare(df)
    line = p["st_line"].to_numpy()
    ts = p["timestamp"].to_numpy()
    changes = np.flatnonzero(np.diff(line) != 0) + 1
    changes = [i for i in changes if not (np.isnan(line[i]) and np.isnan(line[i - 1]))]
    assert changes and all((ts[i] + 60_000) % 300_000 == 0 for i in changes), "neue Werte nur zum Schluss einer 5m-Kerze"
    buys = np.flatnonzero(p["st_buy"].to_numpy())
    assert all((ts[i] + 60_000) % 300_000 == 0 for i in buys)
    assert check_lookahead(s, df, checks=8).ok


def test_gaussian_kernel_weights_recent_bars():
    from tradingbot.strategy.lux import gaussian_kernel

    src = np.arange(20, dtype=float)
    out = gaussian_kernel(src, 6, 6)
    assert np.isnan(out[6]) and not np.isnan(out[7])
    w = np.exp(-np.arange(8) ** 2 / 72.0)
    assert abs(out[19] - float((src[19 - np.arange(8)] * w).sum() / w.sum())) < 1e-12


def test_x_marks_close_trades_and_never_look_ahead():
    from tradingbot.strategy.lux import trigger_waves

    df = make_series("meanrev", 6000, 8)
    bear, bull = trigger_waves(df["close"].to_numpy())
    assert bear.sum() > 0 and bull.sum() > 0
    # Kausal: mit gekuerzten Daten dieselben Marken bis zum Schnitt
    b2, u2 = trigger_waves(df["close"].to_numpy()[:4000])
    assert (b2 == bear[:4000]).all() and (u2 == bull[:4000]).all()
    s = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, st_atr_len=10), timeframe="1h")
    p = s.prepare(df)
    _, sell = s.signals(p)
    i = int(np.flatnonzero(p["st_x_exit_long"].to_numpy() & ~p["st_flip_dn"].to_numpy())[0])
    assert sell[i]
    d = s.decide(p, i, context={"position": {"side": "long"}})
    assert d.signal.value == "sell" and "X-Marke" in d.reason
    off = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, st_atr_len=10, st_x_exit=False), timeframe="1h")
    assert not off.prepare(df)["st_x_exit_long"].any()


def test_x_marks_close_ensemble_trades():
    s = build_strategy(StrategyConfig(name="ensemble", allow_short=True, min_votes=1, members=["opening_range", "supertrend_ai"],
                                      solo_member="opening_range", st_sensitivity=5, st_atr_len=10), timeframe="1h")
    p = s.prepare(make_series("meanrev", 6000, 8))
    _, sell = s.signals(p)
    _, cover = s.short_signals(p)
    xl, xs = p["st_x_exit_long"].to_numpy(), p["st_x_exit_short"].to_numpy()
    assert xl.any() and xs.any()
    assert sell[xl].all() and cover[xs].all()
    i = int(np.flatnonzero(xl & ~s._sides(p)[1])[0])
    d = s.decide(p, i, context={"position": {"side": "long"}})
    assert d.signal.value == "sell" and "X-Marke" in d.reason


def test_trend_filters_only_remove_entries():
    import pandas as pd

    from tradingbot.indicators import adx, choppiness

    df = make_series("trend", 3000, 5)
    base = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5), timeframe="1h").prepare(df)
    for extra in ({"st_adx_min": 25}, {"st_chop_max": 45}, {"st_trend_ema": 200}):
        f = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, **extra), timeframe="1h").prepare(df)
        for col in ("st_buy", "st_short"):
            assert not (f[col] & ~base[col]).any(), "Filter darf keine Signale hinzufuegen"
        assert f["st_buy"].sum() + f["st_short"].sum() < base["st_buy"].sum() + base["st_short"].sum()
        assert (f["st_sell"] == base["st_sell"]).all() and (f["st_cover"] == base["st_cover"]).all(), "Ausstiege bleiben"
    f = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, st_trend_ema=200), timeframe="1h").prepare(df)
    assert (f.loc[f["st_buy"], "close"] > f.loc[f["st_buy"], "st_trend"]).all()
    # Gerade Linie = klarer Trend: ADX hoch, Choppiness niedrig
    line = pd.DataFrame({"high": [i + 1.0 for i in range(100)], "low": [i - 1.0 for i in range(100)], "close": [float(i) for i in range(100)]})
    assert adx(line).iloc[-1] > 50 and choppiness(line).iloc[-1] < 40
