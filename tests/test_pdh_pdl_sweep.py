import numpy as np
import pandas as pd
import pytest

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.backtest import Backtester
from tradingbot.config import DaytradingConfig, PaperConfig, RiskConfig, StrategyConfig
from tradingbot.models import Signal
from tradingbot.risk import RiskManager
from tradingbot.strategy import REGISTRY, build_strategy
from tradingbot.synthetic import make_series

DAY0 = 1_704_153_600_000  # 2024-01-02 00:00 UTC
BAR = 900_000  # 15 Minuten
PER_DAY = 96
SWEEP = PER_DAY + 40  # Index der Ausbruchskerze am zweiten Tag


def _cfg(**kw):
    base = dict(name="pdh_pdl_sweep", allow_short=True, daily_atr_period=2, tz="UTC")
    base.update(kw)
    return StrategyConfig(**base)


def _frame(rows):
    ts = DAY0 + np.arange(len(rows)) * BAR
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _flat(n):
    return [(105.0, 105.2, 104.8, 105.0)] * n


def _market(second_sweep=False, next_open=None, next_low=100.5, next_high=None, sweep_close=108.2):
    """Tag 1: Hoch 110 und Tief 100. Tag 2: Ausbruch ueber 110, Schluss darunter, danach faellt der Kurs."""
    day1 = _flat(PER_DAY)
    day1[10] = (105.0, 110.0, 104.9, 105.0)
    day1[20] = (105.0, 105.1, 100.0, 105.0)
    day2 = _flat(PER_DAY)
    day2[40] = (109.0, 112.0, 108.5, sweep_close)
    day2[41] = (next_open if next_open is not None else sweep_close - 0.01, next_high or sweep_close, next_low, next_low + 0.5)
    if second_sweep:
        day2[60] = (109.0, 112.0, 108.5, sweep_close)
        day2[61] = (sweep_close - 0.01, sweep_close, next_low, next_low + 0.5)
    return _frame(day1 + day2)


def _bt(cfg, daytrading=None, fee=0.0, slip=0.0):
    strat = build_strategy(cfg, session=daytrading)
    return strat, Backtester(strat, RiskManager(RiskConfig()), cfg, PaperConfig(starting_balance=1000, fee_pct=fee, slippage_pct=slip), daytrading=daytrading)


def test_signal_only_on_the_failed_sweep_bar_and_pdh_is_the_prior_day_high():
    strat = build_strategy(_cfg())
    df = strat.prepare(_market())
    short, _ = strat.short_signals(df)
    buy, sell = strat.signals(df)
    assert list(np.flatnonzero(short)) == [SWEEP]
    assert not buy.any() and not sell.any()
    assert df["pdh"].iloc[SWEEP] == 110.0 and df["pdl"].iloc[SWEEP] == 100.0
    assert np.isnan(df["pdh"].iloc[0]), "am ersten Tag gibt es keinen Vortag"


def test_bullish_pdl_sweep_gives_a_long_signal():
    day1 = _flat(PER_DAY)
    day1[10] = (105.0, 110.0, 104.9, 105.0)
    day1[20] = (105.0, 105.1, 100.0, 105.0)
    day2 = _flat(PER_DAY)
    day2[40] = (99.5, 101.5, 98.0, 100.8)  # unter 100 ausgebrochen, darueber geschlossen, bullish
    strat = build_strategy(_cfg())
    df = strat.prepare(_frame(day1 + day2))
    assert list(np.flatnonzero(strat.signals(df)[0])) == [SWEEP]
    assert not strat.short_signals(df)[0].any()


def test_close_outside_range_or_wrong_body_is_not_a_signal():
    strat = build_strategy(_cfg())
    # Schluss bleibt ueber dem Vortageshoch: echter Ausbruch, kein Fade
    df = strat.prepare(_market(sweep_close=110.5))
    assert not strat.short_signals(df)[0][SWEEP]
    # Kerze schliesst unter dem Hoch, ist aber bullish: kein Signal
    rows = _flat(PER_DAY)
    rows[10] = (105.0, 110.0, 104.9, 105.0)
    day2 = _flat(PER_DAY)
    day2[40] = (107.0, 112.0, 106.5, 109.0)
    df = strat.prepare(_frame(rows + day2))
    assert not strat.short_signals(df)[0][SWEEP]


def test_engulfing_filter_blocks_unless_previous_candle_is_swallowed():
    day = _market()
    strat = build_strategy(_cfg(require_engulf=True))
    assert not strat.short_signals(strat.prepare(day))[0].any(), "Vorkerze ist flach, nicht bullish"
    rows = [tuple(r) for r in day[["open", "high", "low", "close"]].to_numpy()]
    rows[SWEEP - 1] = (107.5, 109.0, 107.0, 108.9)  # bullishe Vorkerze
    rows[SWEEP] = (109.2, 112.0, 108.0, 107.4)  # bearishe Kerze umschliesst sie, Hoch ueber 110, Schluss darunter
    df = strat.prepare(_frame(rows))
    assert list(np.flatnonzero(strat.short_signals(df)[0])) == [SWEEP]
    full = build_strategy(_cfg(require_engulf=True, engulf_full_range=True))
    rows[SWEEP] = (109.2, 112.0, 107.2, 107.4)  # Tief liegt ueber dem Tief der Vorkerze, Range umschliesst nicht
    assert not full.short_signals(full.prepare(_frame(rows)))[0].any()


def test_levels_use_daily_atr_multiples_from_signal_close():
    strat = build_strategy(_cfg(sl_atr_mult=0.3, tp_atr_mult=0.1))
    df = strat.prepare(_market())
    ls, lt, ss, st = strat.entry_levels(df)
    close, atr = df["close"].iloc[SWEEP], df["atr_d"].iloc[SWEEP]
    assert atr > 0 and ss[SWEEP] == pytest.approx(close + 0.3 * atr) and st[SWEEP] == pytest.approx(close - 0.1 * atr)
    # Modus 0: Extrem der Signalkerze als Stop, Gegenlinie als Ziel
    strat0 = build_strategy(_cfg(sl_mode=0, tp_mode=0))
    df0 = strat0.prepare(_market())
    _, _, ss0, st0 = strat0.entry_levels(df0)
    assert ss0[SWEEP] == 112.0 and st0[SWEEP] == 100.0


def test_entry_fills_at_next_open_and_exits_at_the_target_price():
    strat = build_strategy(_cfg())
    df0 = strat.prepare(_market())
    atr = df0["atr_d"].iloc[SWEEP]
    fill_open = 108.2 - 0.02 * atr  # unter dem Schlusskurs, aber ueber dem Ziel
    data = _market(next_open=fill_open)
    strat, bt = _bt(_cfg())
    res = bt.run(data, "X/USDT")
    assert res.n_trades == 1
    t = res.trades[0]
    assert t.side == "short" and t.entry_price == pytest.approx(fill_open), "Einstieg zur Eroeffnung der naechsten Kerze, nicht zum Schlusskurs"
    assert t.entry_price != pytest.approx(108.2)
    assert t.reason == "take_profit" and t.exit_price == pytest.approx(108.2 - 0.1 * atr) and t.pnl_quote > 0
    expected_open = pd.to_datetime(DAY0 + (SWEEP + 1) * BAR, unit="ms", utc=True).isoformat(timespec="minutes")
    assert t.opened_at == expected_open


def test_gap_beyond_the_target_cancels_the_order():
    data = _market(next_open=107.0)  # eroeffnet weit unter dem Ziel: der Auftrag ist ungueltig
    _, bt = _bt(_cfg())
    assert bt.run(data, "X/USDT").n_trades == 0


def test_stop_is_checked_on_the_entry_bar_and_loses_when_both_could_hit():
    strat = build_strategy(_cfg())
    atr = strat.prepare(_market())["atr_d"].iloc[SWEEP]
    data = _market(next_open=108.2 - 0.02 * atr, next_low=100.5, next_high=108.2 + 5.0)  # Kerze reicht ueber Stop UND Ziel
    _, bt = _bt(_cfg())
    t = bt.run(data, "X/USDT").trades[0]
    assert t.reason == "stop_loss" and t.pnl_quote < 0, "im Zweifel zaehlt der Stop zuerst"


def test_levels_from_fill_shifts_both_levels_with_the_gap():
    strat = build_strategy(_cfg())
    atr = strat.prepare(_market())["atr_d"].iloc[SWEEP]
    fill_open = 108.2 - 0.02 * atr
    data = _market(next_open=fill_open)
    _, bt = _bt(_cfg(levels_from_fill=True))
    t = bt.run(data, "X/USDT").trades[0]
    assert t.exit_price == pytest.approx(108.2 - 0.1 * atr + (fill_open - 108.2))


def test_trailing_stop_is_off_for_fixed_levels():
    strat = build_strategy(_cfg())
    atr = strat.prepare(_market())["atr_d"].iloc[SWEEP]
    data = _market(next_open=108.2 - 0.02 * atr, next_low=100.5)
    rows = data.copy()
    cfg = _cfg(trailing_stop=True)
    _, bt = _bt(cfg)
    assert bt.run(rows, "X/USDT").trades[0].exit_price == pytest.approx(108.2 - 0.1 * atr)


def test_max_trades_per_day_comes_from_the_daytrading_config():
    strat = build_strategy(_cfg())
    atr = strat.prepare(_market(second_sweep=True))["atr_d"].iloc[SWEEP]
    data = _market(second_sweep=True)
    base = DaytradingConfig(enabled=True, timezone="UTC", entry_start="00:00", entry_end="23:30", flat_time="24:00", max_trades_per_day=0)
    _, bt0 = _bt(_cfg(), daytrading=base)
    two = bt0.run(data, "X/USDT").n_trades
    _, bt1 = _bt(_cfg(), daytrading=base.model_copy(update={"max_trades_per_day": 1}))
    one = bt1.run(data, "X/USDT").n_trades
    assert two == 2 and one == 1


def test_costs_eat_a_tiny_target():
    strat = build_strategy(_cfg())
    atr = strat.prepare(_market())["atr_d"].iloc[SWEEP]
    data = _market(next_open=108.2 - 0.02 * atr)
    _, free = _bt(_cfg(), fee=0.0, slip=0.0)
    _, costly = _bt(_cfg(), fee=0.1, slip=0.05)
    assert free.run(data, "X/USDT").trades[0].pnl_quote > 0
    assert costly.run(data, "X/USDT").trades[0].pnl_quote < free.run(data, "X/USDT").trades[0].pnl_quote


def test_live_decision_uses_strategy_levels_and_is_causal():
    strat = build_strategy(_cfg())
    df = strat.prepare(_market())
    d = strat.decide(df, SWEEP, context={"position": None})
    ls, lt, ss, st = strat.entry_levels(df)
    assert d.signal == Signal.SHORT and d.stop_loss == pytest.approx(ss[SWEEP]) and d.take_profit == pytest.approx(st[SWEEP])
    intraday = make_series("random", 5000, 3, tf_ms=BAR)
    s2 = build_strategy(_cfg(tz="America/New_York", daily_atr_period=14), timeframe="15m")
    assert check_lookahead(s2, intraday).ok
    assert check_recursive(s2, intraday).ok


def test_registered_and_grid_is_deduplicated():
    from tradingbot.research import expand_grid

    assert "pdh_pdl_sweep" in REGISTRY
    combos = expand_grid("pdh_pdl_sweep")
    assert len(combos) == len({tuple(sorted(c.items())) for c in combos}) and 20 <= len(combos) <= 80
    assert all("stop_atr_mult" not in c for c in combos)
