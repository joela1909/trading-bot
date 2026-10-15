from tradingbot.models import Signal
from tradingbot.strategy import build_strategy
from tests.conftest import make_ohlcv, trend_closes


def _signals(strategy, df):
    df = strategy.prepare(df)
    return [(i, strategy.decide(df, i)) for i in range(len(df))]


def test_buy_then_sell_on_trend_reversal(small_strategy_cfg):
    strategy = build_strategy(small_strategy_cfg)
    df = make_ohlcv(trend_closes())
    sigs = _signals(strategy, df)
    buys = [i for i, d in sigs if d.signal == Signal.BUY]
    sells = [i for i, d in sigs if d.signal == Signal.SELL]
    assert buys, "kein Kaufsignal gefunden"
    assert sells, "kein Verkaufssignal gefunden"
    assert 40 <= buys[0] < 80, buys
    assert sells[-1] > buys[0]


def test_buy_decision_has_stop_below_and_tp_above_close(small_strategy_cfg):
    strategy = build_strategy(small_strategy_cfg)
    df = strategy.prepare(make_ohlcv(trend_closes()))
    for i in range(len(df)):
        d = strategy.decide(df, i)
        if d.signal == Signal.BUY:
            close = float(df["close"].iloc[i])
            assert d.stop_loss < close < d.take_profit
            assert d.atr > 0
            break
    else:
        raise AssertionError("kein Kaufsignal")


def test_hold_during_warmup(small_strategy_cfg):
    strategy = build_strategy(small_strategy_cfg)
    df = strategy.prepare(make_ohlcv([100.0, 101.0, 102.0]))
    assert strategy.decide(df).signal == Signal.HOLD


def test_rsi_filter_blocks_entry(small_strategy_cfg):
    strict = small_strategy_cfg.model_copy(update={"rsi_max_entry": 1.0})
    strategy = build_strategy(strict)
    df = strategy.prepare(make_ohlcv(trend_closes()))
    assert all(strategy.decide(df, i).signal != Signal.BUY for i in range(len(df)))


def test_unknown_strategy_raises(small_strategy_cfg):
    import pytest

    with pytest.raises(ValueError):
        build_strategy(small_strategy_cfg.model_copy(update={"name": "nope"}))
