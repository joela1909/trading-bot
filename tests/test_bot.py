import pytest

from tradingbot.bot import TradingBot
from tradingbot.exchange import PaperBroker
from tradingbot.models import Signal
from tradingbot.notify import Notifier
from tradingbot.risk import RiskManager
from tradingbot.state import BotState
from tradingbot.strategy import build_strategy
from tests.conftest import FakeFeed, make_ohlcv, trend_closes


class RecordingNotifier(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _first_buy_index(strategy, df):
    prepared = strategy.prepare(df)
    for i in range(len(prepared)):
        if strategy.decide(prepared, i).signal == Signal.BUY:
            return i
    raise AssertionError("kein Kaufsignal")


def _make_bot(settings, feed, tmp_path):
    strategy = build_strategy(settings.bot.strategy)
    broker = PaperBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    notifier = RecordingNotifier()
    bot = TradingBot(settings, feed, broker, strategy, RiskManager(settings.bot.risk), BotState(), notifier, tmp_path / "state.json")
    return bot, broker, notifier


def test_opens_position_on_buy_signal_and_closes_on_stop(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, broker, notifier = _make_bot(settings, feed, tmp_path)

    equity = bot.run_once()
    assert "BTC/USDT" in bot.state.positions
    pos = bot.state.positions["BTC/USDT"]
    assert pos.amount > 0 and pos.stop_loss < pos.entry_price < pos.take_profit
    assert any(m.startswith("KAUF") for m in notifier.messages)
    assert equity == pytest.approx(1000.0, abs=2.0)

    # gleiche Kerze erneut -> kein zweiter Einstieg, Position bleibt
    bot.run_once()
    assert len(bot.state.positions) == 1

    # Kurs faellt unter den Stop -> Position wird geschlossen
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert not bot.state.positions
    assert len(bot.state.trades) == 1
    assert bot.state.trades[0].reason == "stop_loss"
    assert bot.state.trades[0].pnl_quote < 0
    assert broker.balance_base("BTC/USDT") == 0.0


def test_take_profit_and_trailing_stop(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    initial_stop = pos.stop_loss

    feed.price = pos.entry_price * 1.02
    bot.run_once()
    assert pos.stop_loss > initial_stop, "Trailing-Stop sollte nachziehen"

    feed.price = pos.take_profit * 1.001
    bot.run_once()
    assert bot.state.trades[-1].reason == "take_profit"
    assert bot.state.trades[-1].pnl_quote > 0


def test_daily_loss_limit_halts_entries(settings, tmp_path):
    settings.bot.risk.max_daily_loss_pct = 0.01
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, notifier = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert bot.state.entries_halted_for_day
    assert any("Tagesverlustlimit" in m for m in notifier.messages)


def test_state_persists_and_reloads(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    reloaded = BotState.load(tmp_path / "state.json")
    assert "BTC/USDT" in reloaded.positions
    assert reloaded.last_candle_ts["BTC/USDT"] == int(feed.df["timestamp"].iloc[-1])


def test_reconcile_drops_position_sold_manually(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, broker, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    broker.balances["BTC"] = 0.0
    bot.reconcile()
    assert not bot.state.positions


def test_pause_blocks_new_entries_but_manages_positions(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.pause()
    bot.run_once()
    assert not bot.state.positions
    assert BotState.load(tmp_path / "state.json").entries_paused

    bot.resume()
    bot.state.last_candle_ts.clear()
    bot.run_once()
    assert "BTC/USDT" in bot.state.positions

    bot.pause()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert not bot.state.positions, "Stop-Loss muss trotz Pause greifen"


def test_cycle_builds_snapshot_and_records_equity(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    snap = bot.get_snapshot()
    assert snap["mode"] == "paper" and snap["equity"] == pytest.approx(1000.0, abs=2.0)
    assert snap["positions"] and snap["positions"][0]["price"] == feed.price
    assert len(bot.state.equity_history) == 1
    bot.run_once()
    assert len(bot.state.equity_history) == 1, "Punkte nur alle 5 Minuten"


def test_equity_history_is_thinned_when_too_long(settings, tmp_path):
    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.state.equity_history = [[f"2020-01-01T00:00:{i % 60:02d}+00:00", float(i)] for i in range(bot.EQUITY_MAX_POINTS)]
    bot._record_equity(1234.0)
    assert len(bot.state.equity_history) < bot.EQUITY_MAX_POINTS
    assert bot.state.equity_history[-1][1] == 1234.0
