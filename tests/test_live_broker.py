from types import SimpleNamespace

import pytest

from tradingbot.exchange import LiveBroker


class FakeCcxt:
    """Verhaelt sich wie Bitget: create_order liefert nur die ID, Gebuehren fuer Kaeufe werden in der Basiswaehrung berechnet."""

    def __init__(self, free_base=0.0, total_base=0.0):
        self.orders = {}
        self.created = []
        self.balance = {"free": {"USDT": 500.0, "BTC": free_base}, "total": {"USDT": 500.0, "BTC": total_base}}
        self.has = {"createStopLossOrder": True}

    def load_markets(self):
        pass

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}, "precision": {"amount": 6}}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.6f}"

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def fetch_balance(self):
        return self.balance

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.created.append((type, side, amount, price))
        oid = f"o{len(self.created)}"
        fee = {"cost": amount * 0.001, "currency": "BTC"} if side == "buy" else {"cost": amount * 100 * 0.001, "currency": "USDT"}
        self.orders[oid] = {"id": oid, "status": "closed", "filled": amount, "average": 100.0, "cost": amount * 100.0, "fee": fee}
        return {"id": oid, "status": None}

    def fetch_order(self, oid, symbol):
        return self.orders[oid]


def _broker(**kw):
    return LiveBroker("bitget", "k", "s", "USDT", password="p", ccxt_exchange=FakeCcxt(**kw))


def test_market_buy_passes_price_and_reads_fill_from_fetch_order():
    b = _broker()
    fill = b.buy("BTC/USDT", 0.5)
    assert b.exchange.created[0] == ("market", "buy", 0.5, 100.0)
    assert fill.price == 100.0 and fill.cost == 50.0
    assert fill.amount == pytest.approx(0.5 - 0.0005), "Gebuehr in BTC verringert die erhaltene Menge"
    assert fill.fee == pytest.approx(0.05)


def test_sell_clamps_to_free_balance_and_reports_quote_fee():
    b = _broker(free_base=0.4995, total_base=0.4995)
    fill = b.sell("BTC/USDT", 0.5)
    assert b.exchange.created[0][:3] == ("market", "sell", pytest.approx(0.4995))
    assert fill.amount == pytest.approx(0.4995) and fill.fee == pytest.approx(0.04995)


def test_balance_base_counts_locked_amounts():
    assert _broker(free_base=0.0, total_base=0.5).balance_base("BTC/USDT") == 0.5
