import pytest

from tradingbot.backtest import Backtester
from tradingbot.config import PaperConfig, ProtectionConfig, RiskConfig, StrategyConfig
from tradingbot.protections import Protections
from tradingbot.risk import RiskManager
from tradingbot.strategy import build_strategy
from tradingbot.synthetic import make_series

H = 3_600_000


def _prot(**kw):
    return Protections(ProtectionConfig(**kw), H)


def test_cooldown_blocks_same_symbol_only():
    p = _prot(cooldown_candles=3, stoploss_guard_stops=0, max_drawdown_pct=0)
    p.on_exit("BTC/USDT", "signal", 10 * H)
    assert not p.entry_allowed("BTC/USDT", 12 * H)[0]
    assert p.entry_allowed("ETH/USDT", 12 * H)[0]
    assert p.entry_allowed("BTC/USDT", 13 * H)[0]


def test_stoploss_guard_locks_everything_then_expires():
    p = _prot(cooldown_candles=0, stoploss_guard_stops=3, stoploss_guard_window_candles=10, stoploss_guard_lock_candles=5, max_drawdown_pct=0)
    p.on_exit("A", "stop_loss", 1 * H)
    p.on_exit("B", "stop_loss", 2 * H)
    assert p.entry_allowed("C", 3 * H)[0]
    p.on_exit("C", "stop_loss", 3 * H)
    ok, why = p.entry_allowed("D", 4 * H)
    assert not ok and "Stop-Loss-Serie" in why
    assert p.entry_allowed("D", 8 * H)[0]


def test_stoploss_outside_window_does_not_count_and_other_exits_ignored():
    p = _prot(cooldown_candles=0, stoploss_guard_stops=2, stoploss_guard_window_candles=5, max_drawdown_pct=0)
    p.on_exit("A", "stop_loss", 1 * H)
    p.on_exit("A", "take_profit", 2 * H)
    p.on_exit("A", "stop_loss", 20 * H)
    assert p.entry_allowed("A", 21 * H)[0]


def test_exchange_stop_counts_as_stop_loss():
    p = _prot(cooldown_candles=0, stoploss_guard_stops=1, max_drawdown_pct=0)
    p.on_exit("A", "stop_loss (Boerse)", H)
    assert not p.entry_allowed("A", 2 * H)[0]


def test_drawdown_lock_and_peak_reset():
    p = _prot(cooldown_candles=0, stoploss_guard_stops=0, max_drawdown_pct=10, max_drawdown_lock_candles=240)  # 4 Stunden
    p.update_equity(1000, 0)
    p.update_equity(950, 1 * H)
    assert p.entry_allowed("A", 1 * H)[0]
    p.update_equity(890, 2 * H)
    ok, why = p.entry_allowed("A", 3 * H)
    assert not ok and "Drawdown" in why
    assert p.entry_allowed("A", 7 * H)[0]
    p.update_equity(885, 7 * H)
    assert p.entry_allowed("A", 8 * H)[0], "Hoch wurde zurueckgesetzt, kein Dauer-Lock"


def test_state_roundtrip():
    p = _prot(cooldown_candles=2, stoploss_guard_stops=1)
    p.on_exit("A", "stop_loss", 5 * H)
    q = _prot(cooldown_candles=2, stoploss_guard_stops=1)
    q.load(p.to_dict())
    assert q.locked_until == p.locked_until and q.last_exit == p.last_exit
    assert not q.entry_allowed("B", 6 * H)[0]


def test_backtest_with_protections_trades_less_in_chop():
    cfg = StrategyConfig(name="macd_trend")
    df = make_series("meanrev", 6000, 3)
    kw = dict(strategy=build_strategy(cfg), risk=RiskManager(RiskConfig()), strategy_cfg=cfg, paper_cfg=PaperConfig())
    free = Backtester(**kw).run(df, "X/USDT")
    strict = Backtester(**kw, protections_cfg=ProtectionConfig(cooldown_candles=24, stoploss_guard_stops=2, stoploss_guard_lock_candles=48)).run(df, "X/USDT")
    assert strict.n_trades < free.n_trades


def test_bot_respects_cooldown_after_exit(settings, tmp_path):
    from tradingbot.bot import TradingBot
    from tradingbot.exchange import PaperBroker
    from tradingbot.notify import Notifier
    from tradingbot.state import BotState
    from tests.conftest import FakeFeed, make_ohlcv, trend_closes
    from tests.test_bot import _first_buy_index

    settings.bot.protections = ProtectionConfig(cooldown_candles=1000, stoploss_guard_stops=0, max_drawdown_pct=0)
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    broker = PaperBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    bot = TradingBot(settings, feed, broker, strategy, RiskManager(settings.bot.risk), BotState(), Notifier(), tmp_path / "state.json")
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert not bot.state.positions
    bot.state.last_candle_ts.clear()
    feed.price = float(full["close"].iloc[buy_i])
    bot.run_once()
    assert not bot.state.positions, "Abkuehlphase muss den Wiedereinstieg verhindern"
    reloaded = BotState.load(tmp_path / "state.json")
    assert "BTC/USDT" in reloaded.protections["last_exit"]



def test_drawdown_lock_lasts_minutes_on_every_timeframe():
    from tradingbot.config import ProtectionConfig
    from tradingbot.protections import Protections

    cfg = ProtectionConfig(max_drawdown_pct=20.0, max_drawdown_lock_candles=240)
    p = Protections(cfg, 4 * 3_600_000)  # 4h-Kerzen
    p.update_equity(1000.0, 0)
    p.update_equity(790.0, 1000)
    assert p.locked_until == 1000 + 240 * 60_000, "4 Stunden, nicht 240 x 4 Stunden"
    old = Protections(cfg, 900_000)
    old.load({"locked_until": 5_000 + 240 * 900_000, "lock_reason": "Drawdown-Grenze"})
    assert old.locked_until == 5_000 + 240 * 60_000, "alte, zu lange Sperre wird gekuerzt"



def test_drawdown_off_lifts_running_lock():
    from tradingbot.config import ProtectionConfig
    from tradingbot.protections import Protections

    p = Protections(ProtectionConfig(max_drawdown_pct=0), 60_000)
    p.load({"locked_until": 10**13, "lock_reason": "Drawdown-Grenze", "peak": 1000.0})
    assert p.locked_until == 0 and p.peak is None and p.entry_allowed("A", 10**12)[0]
    p.update_equity(1000.0, 0)
    p.update_equity(1.0, 1)
    assert p.entry_allowed("A", 2)[0], "aus: nie sperren"
