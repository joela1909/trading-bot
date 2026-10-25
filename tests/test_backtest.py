import numpy as np

from tradingbot.backtest import Backtester
from tradingbot.config import PaperConfig, RiskConfig
from tradingbot.risk import RiskManager
from tradingbot.strategy import build_strategy
from tests.conftest import make_ohlcv, trend_closes


def test_backtest_produces_trades_and_consistent_equity(small_strategy_cfg):
    df = make_ohlcv(np.concatenate([trend_closes(), trend_closes()]))
    bt = Backtester(build_strategy(small_strategy_cfg), RiskManager(RiskConfig(min_order_quote=1.0)), small_strategy_cfg, PaperConfig(starting_balance=1000, fee_pct=0.1, slippage_pct=0.05))
    res = bt.run(df, "BTC/USDT")
    assert res.n_trades >= 2
    assert res.end_equity == float(res.equity_curve.iloc[-1])
    realized = sum(t.pnl_quote for t in res.trades)
    assert abs((res.end_equity - res.start_balance) - realized) < 1e-6
    assert 0 <= res.max_drawdown_pct <= 100
    assert 0 <= res.win_rate_pct <= 100
    assert "Rendite" in res.summary()


def test_backtest_no_trades_on_flat_market(small_strategy_cfg):
    df = make_ohlcv([100.0] * 200)
    bt = Backtester(build_strategy(small_strategy_cfg), RiskManager(RiskConfig()), small_strategy_cfg, PaperConfig())
    res = bt.run(df, "BTC/USDT")
    assert res.n_trades == 0
    assert res.end_equity == res.start_balance


def test_backtest_rejects_too_little_data(small_strategy_cfg):
    import pytest

    bt = Backtester(build_strategy(small_strategy_cfg), RiskManager(RiskConfig()), small_strategy_cfg, PaperConfig())
    with pytest.raises(ValueError):
        bt.run(make_ohlcv([100.0] * 5), "BTC/USDT")


def test_stop_gap_fills_at_open_not_at_stop():
    import pandas as pd

    from tradingbot.backtest import Backtester
    from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
    from tradingbot.risk import RiskManager
    from tradingbot.strategy import build_strategy

    cfg = StrategyConfig(name="ema_cross", trailing_stop=False, take_profit_atr_mult=0)
    strat = build_strategy(cfg)
    n = 600
    closes = [100.0] * n
    df = pd.DataFrame({"timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(n)], "open": closes, "high": [101.0] * n,
                       "low": [99.0] * n, "close": closes, "volume": 1.0})
    # Kerze 250 springt unter den Stop: Eroeffnung 90, Stop lag bei 95
    df.loc[550, ["open", "high", "low", "close"]] = [90.0, 91.0, 89.0, 90.5]
    df = strat.prepare(df)
    buy = [False] * n
    buy[540] = True
    sell = [False] * n
    bt = Backtester(strat, RiskManager(RiskConfig(min_order_quote=0)), cfg, PaperConfig(fee_pct=0, slippage_pct=0))
    r = bt.run(df, "X", start=500, prepared=True, signals=(pd.Series(buy).to_numpy(), pd.Series(sell).to_numpy()))
    t = r.trades[0]
    assert t.reason == "stop_loss" and t.exit_price == 90.0, t
