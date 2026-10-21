import pytest

from tradingbot.bot import TradingBot
from tradingbot.config import ExecutionConfig
from tradingbot.exchange import LiveBroker, PaperBroker
from tradingbot.models import Fill
from tradingbot.notify import Notifier
from tradingbot.risk import RiskManager
from tradingbot.state import BotState
from tradingbot.strategy import build_strategy
from tests.conftest import FakeFeed, make_ohlcv, trend_closes
from tests.test_bot import _first_buy_index
from tests.test_live_broker import FakeCcxt


class StopBroker(PaperBroker):
    """Paper-Broker, der Stop-Orders bei einer 'Boerse' hinterlegen kann."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stops = {}
        self.log = []
        self.fail_place = False
        self.cancel_result = None

    def supports_exchange_stop(self):
        return True

    def place_stop(self, symbol, amount, stop_price):
        if self.fail_place:
            return None
        oid = f"s{len(self.stops) + 1}"
        self.stops[oid] = {"amount": amount, "price": stop_price, "status": "open"}
        self.log.append(("place", oid, stop_price))
        return oid

    def trigger(self, oid):
        st = self.stops[oid]
        base = "BTC"
        proceeds = st["amount"] * st["price"]
        fee = proceeds * self.fee_rate
        self.balances[base] = self.balances.get(base, 0.0) - st["amount"]
        self.balances[self.quote] = self.balance_quote() + proceeds - fee
        st["status"] = "filled"
        st["fill"] = Fill("BTC/USDT", "sell", st["amount"], st["price"], proceeds, fee)

    def cancel_stop(self, symbol, oid):
        st = self.stops[oid]
        self.log.append(("cancel", oid))
        if self.cancel_result:
            return self.cancel_result
        if st["status"] == "filled":
            return "filled"
        st["status"] = "canceled"
        return "canceled"

    def stop_status(self, symbol, oid):
        st = self.stops[oid]
        if st["status"] == "filled":
            return "filled", st["fill"]
        return ("open", None) if st["status"] == "open" else ("gone", None)


class Recorder(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _setup(settings, tmp_path):
    settings.bot.execution = ExecutionConfig(stoploss_on_exchange=True, stoploss_replace_threshold_pct=0.25)
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    broker = StopBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    notifier = Recorder()
    bot = TradingBot(settings, feed, broker, strategy, RiskManager(settings.bot.risk), BotState(), notifier, tmp_path / "state.json")
    return bot, feed, broker, notifier


def test_entry_places_stop_at_exchange(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    assert pos.stop_order_id == "s1" and pos.exchange_stop == pos.stop_loss
    assert broker.log[0] == ("place", "s1", pos.stop_loss)
    assert BotState.load(tmp_path / "state.json").positions["BTC/USDT"].stop_order_id == "s1"
    assert bot.get_snapshot()["positions"][0]["stop_on_exchange"] is True


def test_exchange_stop_triggering_closes_position_without_second_sell(settings, tmp_path):
    bot, feed, broker, notifier = _setup(settings, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    broker.trigger("s1")
    feed.price = pos.stop_loss * 0.98
    bot.run_once()
    assert not bot.state.positions
    trade = bot.state.trades[-1]
    assert trade.reason == "stop_loss (Boerse)" and trade.exit_price == pytest.approx(pos.stop_loss) and trade.pnl_quote < 0
    assert broker.balance_base("BTC/USDT") == pytest.approx(0.0, abs=1e-9)
    assert "BTC/USDT" in bot.protections.last_exit


def test_trailing_stop_moves_order_at_exchange(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    first = pos.stop_loss
    feed.price = pos.entry_price * 1.03
    bot.run_once()
    assert pos.stop_loss > first and pos.exchange_stop == pos.stop_loss
    assert [e[0] for e in broker.log] == ["place", "cancel", "place"]
    assert broker.stops["s1"]["status"] == "canceled" and pos.stop_order_id == "s2"


def test_small_trailing_move_does_not_spam_the_exchange(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    settings.bot.execution.stoploss_replace_threshold_pct = 50.0
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.entry_price * 1.03
    bot.run_once()
    assert [e[0] for e in broker.log] == ["place"]


def test_software_exit_cancels_the_exchange_stop_first(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.take_profit * 1.001
    bot.run_once()
    assert [e[0] for e in broker.log][:2] == ["place", "cancel"]
    assert bot.state.trades[-1].reason == "take_profit" and not bot.state.positions


def test_cancel_race_when_stop_already_filled_books_the_exchange_fill(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    broker.trigger("s1")
    broker.stops["s1"]["status"] = "filled"
    feed.price = pos.take_profit * 1.001
    pos_stop_status = broker.stop_status
    broker.stop_status = lambda symbol, oid: ("open", None) if broker.log.count(("cancel", oid)) == 0 else pos_stop_status(symbol, oid)
    bot.run_once()
    assert bot.state.trades[-1].reason == "stop_loss (Boerse)"
    assert broker.balance_base("BTC/USDT") == pytest.approx(0.0, abs=1e-9), "kein doppelter Verkauf"


def test_failed_placement_warns_and_software_stop_still_works(settings, tmp_path):
    bot, feed, broker, notifier = _setup(settings, tmp_path)
    broker.fail_place = True
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    assert pos.stop_order_id is None and any("WARNUNG" in m for m in notifier.messages)
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert bot.state.trades[-1].reason == "stop_loss" and not bot.state.positions


def test_vanished_stop_order_is_replaced(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    bot.run_once()
    broker.stops["s1"]["status"] = "canceled"
    bot.run_once()
    assert bot.state.positions["BTC/USDT"].stop_order_id == "s2"


def test_disabled_by_default_places_nothing(settings, tmp_path):
    bot, feed, broker, _ = _setup(settings, tmp_path)
    bot.cfg.execution.stoploss_on_exchange = False
    bot.run_once()
    assert broker.log == [] and bot.state.positions["BTC/USDT"].stop_order_id is None


# ----------------------------------------------------------- LiveBroker gegen simulierte ccxt-Boerse
class StopCcxt(FakeCcxt):
    def __init__(self):
        super().__init__()
        self.canceled = []

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        if params and "stopLossPrice" in params:
            oid = f"stop{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, "status": "open", "filled": 0, "params": params, "amount": amount}
            return {"id": oid}
        return super().create_order(symbol, type, side, amount, price, params)

    def fetch_order(self, oid, symbol, params=None):
        order = self.orders[oid]
        if "params" in order and not params:
            raise RuntimeError("OrderNotFound ohne trigger-Parameter")
        return order

    def cancel_order(self, oid, symbol, params=None):
        if not params:
            raise RuntimeError("OrderNotFound ohne trigger-Parameter")
        self.canceled.append(oid)
        self.orders[oid]["status"] = "canceled"


def test_live_broker_places_polls_and_cancels_trigger_orders():
    ex = StopCcxt()
    b = LiveBroker("bitget", "k", "s", "USDT", password="p", ccxt_exchange=ex)
    assert b.supports_exchange_stop()
    oid = b.place_stop("BTC/USDT", 0.5, 95.123)
    assert oid and ex.orders[oid]["params"] == {"stopLossPrice": 95.12}
    assert b.stop_status("BTC/USDT", oid) == ("open", None)
    assert b.cancel_stop("BTC/USDT", oid) == "canceled" and ex.canceled == [oid]
    assert b.stop_status("BTC/USDT", oid) == ("gone", None)


def test_live_broker_reports_filled_stop_with_fill_details():
    ex = StopCcxt()
    b = LiveBroker("bitget", "k", "s", "USDT", password="p", ccxt_exchange=ex)
    oid = b.place_stop("BTC/USDT", 0.5, 95.0)
    ex.orders[oid].update(status="closed", filled=0.5, average=94.8, cost=47.4, fee={"cost": 0.0474, "currency": "USDT"})
    status, fill = b.stop_status("BTC/USDT", oid)
    assert status == "filled" and fill.price == 94.8 and fill.amount == 0.5 and fill.fee == pytest.approx(0.0474)
    assert b.cancel_stop("BTC/USDT", oid) in ("canceled", "filled")


def test_live_broker_without_stop_support_returns_none():
    ex = StopCcxt()
    ex.has = {"createStopLossOrder": None}
    b = LiveBroker("kraken", "k", "s", "USDT", ccxt_exchange=ex)
    assert not b.supports_exchange_stop() and b.place_stop("BTC/USDT", 0.1, 90.0) is None


def test_live_broker_placement_error_returns_none():
    ex = StopCcxt()
    ex.create_order = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Mindestbetrag"))
    b = LiveBroker("bitget", "k", "s", "USDT", password="p", ccxt_exchange=ex)
    assert b.place_stop("BTC/USDT", 0.1, 90.0) is None
