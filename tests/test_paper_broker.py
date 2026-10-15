import pytest

from tradingbot.exchange import InsufficientFunds, PaperBroker
from tests.conftest import FakeFeed, make_ohlcv


def _broker(tmp_path, balance=1000.0, fee=0.1, slip=0.0):
    feed = FakeFeed(make_ohlcv([100.0] * 5), price=100.0)
    return feed, PaperBroker(feed, "USDT", balance, fee, slip, state_path=tmp_path / "bal.json")


def test_buy_and_sell_update_balances(tmp_path):
    feed, b = _broker(tmp_path)
    fill = b.buy("BTC/USDT", 2.0)
    assert fill.price == 100.0 and fill.cost == 200.0 and fill.fee == pytest.approx(0.2)
    assert b.balance_quote() == pytest.approx(1000 - 200 - 0.2)
    assert b.balance_base("BTC/USDT") == 2.0

    feed.price = 110.0
    fill = b.sell("BTC/USDT", 2.0)
    assert fill.cost == pytest.approx(220.0) and fill.fee == pytest.approx(0.22)
    assert b.balance_quote() == pytest.approx(1000 - 200.2 + 220 - 0.22)
    assert b.balance_base("BTC/USDT") == 0.0


def test_slippage_applied(tmp_path):
    _, b = _broker(tmp_path, slip=1.0)
    assert b.buy("BTC/USDT", 1.0).price == pytest.approx(101.0)
    assert b.sell("BTC/USDT", 1.0).price == pytest.approx(99.0)


def test_insufficient_funds(tmp_path):
    _, b = _broker(tmp_path, balance=50.0)
    with pytest.raises(InsufficientFunds):
        b.buy("BTC/USDT", 1.0)
    with pytest.raises(InsufficientFunds):
        b.sell("BTC/USDT", 1.0)


def test_balances_persist(tmp_path):
    feed, b = _broker(tmp_path)
    b.buy("BTC/USDT", 1.0)
    b2 = PaperBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    assert b2.balance_base("BTC/USDT") == 1.0
    assert b2.balance_quote() == pytest.approx(b.balance_quote())
