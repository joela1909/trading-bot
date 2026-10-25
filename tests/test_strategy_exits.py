import numpy as np

from tradingbot.backtest import Backtester
from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
from tradingbot.risk import RiskManager
from tradingbot.strategy import build_strategy
from tradingbot.synthetic import make_series


def test_own_exits_override_global_values():
    cfg = StrategyConfig(name="rsi_reversion", stop_atr_mult=2.0, take_profit_atr_mult=4.0, min_stop_pct=0.5,
                         exits={"rsi_reversion": {"stop_atr_mult": 1.5, "take_profit_atr_mult": None}})
    s = build_strategy(cfg)
    assert s.cfg.stop_atr_mult == 1.5 and s.cfg.take_profit_atr_mult == 4.0 and s.cfg.min_stop_pct == 0.5
    assert build_strategy(StrategyConfig(name="donchian", exits=cfg.exits)).cfg.stop_atr_mult == 2.0


def test_ensemble_uses_stop_and_target_of_trigger():
    cfg = StrategyConfig(name="ensemble", allow_short=True, min_votes=1, members=["donchian", "rsi_reversion"],
                         stop_atr_mult=2.0, take_profit_atr_mult=4.0, min_stop_pct=0.0,
                         exits={"donchian": {"stop_atr_mult": 3.0, "take_profit_atr_mult": 9.0}})
    s = build_strategy(cfg, timeframe="1h")
    df = s.prepare(make_series("trend", 1500, 3))
    i = int(np.flatnonzero(df["v_donchian_buy"].to_numpy() & ~df["v_rsi_reversion_buy"].to_numpy())[0])
    a = float(df["atr"].iloc[i])
    stop, tp = s.entry_plan(df, i, "long", 100.0, a)
    assert abs((100.0 - stop) - 3.0 * a) < 1e-9 and abs((tp - 100.0) - 9.0 * a) < 1e-9


def test_supertrend_sets_stop_at_line_without_target_or_cost_filter():
    s = build_strategy(StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5, take_profit_atr_mult=1.0,
                                      stop_atr_mult=2.0, min_stop_pct=5.0), timeframe="1h")
    df = s.prepare(make_series("trend", 3000, 5))
    i = int(np.flatnonzero(s.signals(df)[0])[0])
    c, line = float(df["close"].iloc[i]), float(df["st_stop_long"].iloc[i])
    stop, tp = s.entry_plan(df, i, "long", c, float(df["atr"].iloc[i]))
    assert tp is None and abs(stop - min(line, c - 0.1 * float(df["atr"].iloc[i]))) < 1e-9
    assert not s.cost_check("supertrend_ai")
    ens = build_strategy(StrategyConfig(name="ensemble", min_votes=1, members=["donchian", "supertrend_ai"], st_sensitivity=5))
    assert not ens.cost_check("supertrend_ai") and ens.cost_check("donchian") and not ens.cost_check("supertrend_ai+donchian")


def test_backtest_skips_cost_filter_for_supertrend():
    cfg = StrategyConfig(name="supertrend_ai", allow_short=True, st_sensitivity=5)
    s = build_strategy(cfg, timeframe="1h")
    df = s.prepare(make_series("trend", 3000, 5))
    r = Backtester(s, RiskManager(RiskConfig(min_order_quote=0, min_target_cost_ratio=20.0)), cfg, PaperConfig()).run(
        df, "X", start=s.warmup_bars, prepared=True)
    assert r.trades
