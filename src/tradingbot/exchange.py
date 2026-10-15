from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from .data import DataFeed
from .models import Fill, MarketLimits

log = logging.getLogger(__name__)


class InsufficientFunds(Exception):
    pass


class Broker(Protocol):
    name: str

    def balance_quote(self) -> float: ...
    def balance_base(self, symbol: str) -> float: ...
    def buy(self, symbol: str, amount: float) -> Fill: ...
    def sell(self, symbol: str, amount: float) -> Fill: ...
    def limits(self, symbol: str) -> MarketLimits: ...


class PaperBroker:
    """Simuliert Market-Orders zu echten Kursen inkl. Gebuehr und Slippage."""

    name = "paper"

    def __init__(
        self,
        feed: DataFeed,
        quote_currency: str,
        starting_balance: float,
        fee_pct: float,
        slippage_pct: float,
        state_path: str | Path | None = None,
    ):
        self.feed = feed
        self.quote = quote_currency
        self.fee_rate = fee_pct / 100.0
        self.slippage = slippage_pct / 100.0
        self.state_path = Path(state_path) if state_path else None
        self.balances: dict[str, float] = {quote_currency: float(starting_balance)}
        if self.state_path and self.state_path.exists():
            self.balances = json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.balances, indent=2), encoding="utf-8")

    def balance_quote(self) -> float:
        return float(self.balances.get(self.quote, 0.0))

    def balance_base(self, symbol: str) -> float:
        return float(self.balances.get(symbol.split("/")[0], 0.0))

    def buy(self, symbol: str, amount: float) -> Fill:
        base = symbol.split("/")[0]
        price = self.feed.fetch_price(symbol) * (1.0 + self.slippage)
        cost = amount * price
        fee = cost * self.fee_rate
        if cost + fee > self.balance_quote() + 1e-9:
            raise InsufficientFunds(f"Paper: {cost + fee:.2f} {self.quote} benoetigt, {self.balance_quote():.2f} verfuegbar")
        self.balances[self.quote] = self.balance_quote() - cost - fee
        self.balances[base] = self.balances.get(base, 0.0) + amount
        self._save()
        return Fill(symbol, "buy", amount, price, cost, fee)

    def sell(self, symbol: str, amount: float) -> Fill:
        base = symbol.split("/")[0]
        held = self.balance_base(symbol)
        if amount > held + 1e-12:
            raise InsufficientFunds(f"Paper: {amount} {base} verkaufen, aber nur {held} vorhanden")
        price = self.feed.fetch_price(symbol) * (1.0 - self.slippage)
        proceeds = amount * price
        fee = proceeds * self.fee_rate
        self.balances[base] = held - amount
        self.balances[self.quote] = self.balance_quote() + proceeds - fee
        self._save()
        return Fill(symbol, "sell", amount, price, proceeds, fee)

    def limits(self, symbol: str) -> MarketLimits:
        try:
            return self.feed.market_limits(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("Marktlimits fuer %s nicht verfuegbar (%s), nutze Standard", symbol, exc)
            return MarketLimits()


class LiveBroker:
    """Echte Market-Orders ueber ccxt. Nur mit bestaetigtem Live-Modus verwenden."""

    name = "live"

    def __init__(self, exchange_id: str, api_key: str, api_secret: str, quote_currency: str, password: str = "", ccxt_exchange=None):
        if ccxt_exchange is None:
            import ccxt

            params = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            if password:
                params["password"] = password
            ccxt_exchange = getattr(ccxt, exchange_id)(params)
        self.exchange = ccxt_exchange
        self.quote = quote_currency
        self.exchange.load_markets()

    def balance_quote(self) -> float:
        bal = self.exchange.fetch_balance()
        return float((bal.get("free") or {}).get(self.quote) or 0.0)

    def balance_base(self, symbol: str) -> float:
        bal = self.exchange.fetch_balance()
        return float((bal.get("free") or {}).get(symbol.split("/")[0]) or 0.0)

    def _order_to_fill(self, symbol: str, side: str, order: dict, fallback_amount: float) -> Fill:
        if order.get("status") not in (None, "closed", "filled") and not order.get("filled"):
            order = self.exchange.fetch_order(order["id"], symbol)
        amount = float(order.get("filled") or fallback_amount)
        price = order.get("average") or order.get("price")
        cost = order.get("cost")
        if price is None and cost and amount:
            price = float(cost) / amount
        if price is None:
            price = self.exchange.fetch_ticker(symbol)["last"]
        if cost is None:
            cost = amount * float(price)
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or 0.0)
        if fee_info.get("currency") and fee_info.get("currency") != self.quote and fee:
            fee = fee * float(price) if fee_info.get("currency") == symbol.split("/")[0] else 0.0
        return Fill(symbol, side, amount, float(price), float(cost), fee, order_id=str(order.get("id")))

    def buy(self, symbol: str, amount: float) -> Fill:
        amount_str = self.exchange.amount_to_precision(symbol, amount)
        log.warning("LIVE KAUF %s %s", amount_str, symbol)
        order = self.exchange.create_market_buy_order(symbol, float(amount_str))
        return self._order_to_fill(symbol, "buy", order, float(amount_str))

    def sell(self, symbol: str, amount: float) -> Fill:
        amount_str = self.exchange.amount_to_precision(symbol, amount)
        log.warning("LIVE VERKAUF %s %s", amount_str, symbol)
        order = self.exchange.create_market_sell_order(symbol, float(amount_str))
        return self._order_to_fill(symbol, "sell", order, float(amount_str))

    def limits(self, symbol: str) -> MarketLimits:
        market = self.exchange.market(symbol)
        lim = market.get("limits", {}) or {}
        prec = (market.get("precision", {}) or {}).get("amount")
        if prec is None:
            prec = 8
        elif isinstance(prec, float) and prec < 1:
            import math

            prec = max(0, round(-math.log10(prec)))
        return MarketLimits(
            min_amount=float((lim.get("amount") or {}).get("min") or 0.0),
            min_cost=float((lim.get("cost") or {}).get("min") or 0.0),
            amount_precision=int(prec),
        )
