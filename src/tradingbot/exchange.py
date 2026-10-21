from __future__ import annotations

import json
import logging
import time
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
    def open_position(self, symbol: str, side: str, amount: float) -> Fill: ...
    def close_position(self, symbol: str, side: str, amount: float) -> Fill: ...
    def limits(self, symbol: str) -> MarketLimits: ...
    def supports_exchange_stop(self) -> bool: ...
    def place_stop(self, symbol: str, amount: float, stop_price: float) -> str | None: ...
    def cancel_stop(self, symbol: str, order_id: str) -> str: ...
    def stop_status(self, symbol: str, order_id: str) -> tuple[str, Fill | None]: ...


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

    # Einheitliche Schnittstelle fuer den Bot. Spot kennt nur Long.
    def open_position(self, symbol: str, side: str, amount: float) -> Fill:
        if side != "long":
            raise ValueError("Spot-Handel kann keine Short-Positionen eroeffnen, dafuer market: futures verwenden")
        return self.buy(symbol, amount)

    def close_position(self, symbol: str, side: str, amount: float) -> Fill:
        if side != "long":
            raise ValueError("Spot-Handel hat keine Short-Positionen")
        return self.sell(symbol, amount)

    def supports_exchange_stop(self) -> bool:
        return False

    def place_stop(self, symbol: str, amount: float, stop_price: float) -> str | None:
        return None

    def cancel_stop(self, symbol: str, order_id: str) -> str:
        return "gone"

    def stop_status(self, symbol: str, order_id: str) -> tuple[str, Fill | None]:
        return "gone", None

    def limits(self, symbol: str) -> MarketLimits:
        try:
            return self.feed.market_limits(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("Marktlimits fuer %s nicht verfuegbar (%s), nutze Standard", symbol, exc)
            return MarketLimits()


class PaperFuturesBroker:
    """Simuliert USDT-Perpetual-Futures: Long und Short, Sicherheit = Nennwert / Hebel, Gebuehr auf den Nennwert.

    Finanzierungsgebuehren (Funding) und Liquidation werden nicht simuliert. Bei Hebel 1 und Stop-Loss
    liegt die Liquidation weit hinter dem Stop."""

    name = "paper"

    def __init__(
        self,
        feed: DataFeed,
        quote_currency: str,
        starting_balance: float,
        fee_pct: float,
        slippage_pct: float,
        leverage: float = 1.0,
        state_path: str | Path | None = None,
    ):
        self.feed = feed
        self.quote = quote_currency
        self.fee_rate = fee_pct / 100.0
        self.slippage = slippage_pct / 100.0
        self.leverage = max(1.0, float(leverage))
        self.state_path = Path(state_path) if state_path else None
        self.cash = float(starting_balance)
        # je Paar: Menge mit Vorzeichen (+ Long, - Short), Einstiegskurs und gebundene Sicherheit
        self.positions: dict[str, dict] = {}
        if self.state_path and self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.cash = float(data.get("cash", starting_balance))
            self.positions = dict(data.get("positions", {}))

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"cash": self.cash, "positions": self.positions}, indent=2), encoding="utf-8")

    def balance_quote(self) -> float:
        """Freie Sicherheit, die nicht in Positionen gebunden ist."""
        return self.cash

    def balance_base(self, symbol: str) -> float:
        return abs(float(self.positions.get(symbol, {}).get("amount", 0.0)))

    def open_position(self, symbol: str, side: str, amount: float) -> Fill:
        if symbol in self.positions:
            raise InsufficientFunds(f"Paper-Futures: fuer {symbol} ist schon eine Position offen")
        sign = -1 if side == "short" else 1
        price = self.feed.fetch_price(symbol) * (1.0 + sign * self.slippage)
        notional = amount * price
        margin = notional / self.leverage
        fee = notional * self.fee_rate
        if margin + fee > self.cash + 1e-9:
            raise InsufficientFunds(f"Paper-Futures: {margin + fee:.2f} {self.quote} Sicherheit benoetigt, {self.cash:.2f} frei")
        self.cash -= margin + fee
        self.positions[symbol] = {"amount": sign * amount, "entry": price, "margin": margin}
        self._save()
        return Fill(symbol, "buy" if sign > 0 else "sell", amount, price, notional, fee)

    def close_position(self, symbol: str, side: str, amount: float) -> Fill:
        pos = self.positions.get(symbol)
        if pos is None:
            raise InsufficientFunds(f"Paper-Futures: keine offene Position fuer {symbol}")
        sign = 1 if pos["amount"] > 0 else -1
        if (side == "short") != (sign < 0):
            raise ValueError(f"Paper-Futures: {symbol} ist {'short' if sign < 0 else 'long'}, nicht {side}")
        held = abs(pos["amount"])
        amount = min(amount, held)
        price = self.feed.fetch_price(symbol) * (1.0 - sign * self.slippage)
        notional = amount * price
        fee = notional * self.fee_rate
        share = amount / held
        pnl = sign * (price - pos["entry"]) * amount
        self.cash += pos["margin"] * share + pnl - fee
        if share >= 1.0 - 1e-12:
            del self.positions[symbol]
        else:
            pos["amount"] -= sign * amount
            pos["margin"] *= 1.0 - share
        self._save()
        return Fill(symbol, "sell" if sign > 0 else "buy", amount, price, notional, fee)

    def buy(self, symbol: str, amount: float) -> Fill:
        return self.open_position(symbol, "long", amount)

    def sell(self, symbol: str, amount: float) -> Fill:
        return self.close_position(symbol, "long", amount)

    def supports_exchange_stop(self) -> bool:
        return False

    def place_stop(self, symbol: str, amount: float, stop_price: float) -> str | None:
        return None

    def cancel_stop(self, symbol: str, order_id: str) -> str:
        return "gone"

    def stop_status(self, symbol: str, order_id: str) -> tuple[str, Fill | None]:
        return "gone", None

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
        """Gesamtbestand inklusive gesperrter Mengen, damit z. B. hinterlegte Stop-Orders nicht wie ein Verkauf wirken."""
        bal = self.exchange.fetch_balance()
        base = symbol.split("/")[0]
        total = (bal.get("total") or {}).get(base)
        if total is None:
            total = (bal.get("free") or {}).get(base)
        return float(total or 0.0)

    def _free_base(self, symbol: str) -> float:
        bal = self.exchange.fetch_balance()
        return float((bal.get("free") or {}).get(symbol.split("/")[0]) or 0.0)

    def _settle(self, symbol: str, order: dict) -> dict:
        """Manche Boersen (z. B. Bitget) liefern beim Anlegen nur die Order-ID. Dann die fertige Order nachladen."""
        for attempt in range(4):
            done = order.get("filled") and (order.get("average") or order.get("price") or order.get("cost"))
            if done and order.get("status") in (None, "closed", "filled"):
                return order
            if attempt:
                time.sleep(0.6)
            try:
                order = self.exchange.fetch_order(order["id"], symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("Order %s nicht abrufbar: %s", order.get("id"), exc)
        return order

    def _order_to_fill(self, symbol: str, side: str, order: dict, fallback_amount: float) -> Fill:
        order = self._settle(symbol, order)
        base = symbol.split("/")[0]
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
        fee_currency = fee_info.get("currency")
        if fee and fee_currency == base:
            fee_in_quote = fee * float(price)
            if side == "buy":
                amount -= fee
            fee = fee_in_quote
        elif fee and fee_currency and fee_currency != self.quote:
            fee = 0.0
        return Fill(symbol, side, amount, float(price), float(cost), fee, order_id=str(order.get("id")))

    def buy(self, symbol: str, amount: float) -> Fill:
        amount_str = self.exchange.amount_to_precision(symbol, amount)
        price = float(self.exchange.fetch_ticker(symbol)["last"])
        log.warning("LIVE KAUF %s %s", amount_str, symbol)
        # Der Preis wird mitgegeben: Bitget und einige andere Boersen brauchen ihn, um bei Market-Kaeufen die Kosten zu berechnen.
        order = self.exchange.create_order(symbol, "market", "buy", float(amount_str), price)
        return self._order_to_fill(symbol, "buy", order, float(amount_str))

    def sell(self, symbol: str, amount: float) -> Fill:
        free = self._free_base(symbol)
        if 0 < free < amount:
            log.warning("%s: Bestand %.8f unter Positionsmenge %.8f (z. B. Gebuehr in %s), verkaufe den Bestand", symbol, free, amount, symbol.split("/")[0])
            amount = free
        amount_str = self.exchange.amount_to_precision(symbol, amount)
        log.warning("LIVE VERKAUF %s %s", amount_str, symbol)
        order = self.exchange.create_order(symbol, "market", "sell", float(amount_str))
        return self._order_to_fill(symbol, "sell", order, float(amount_str))

    # Einheitliche Schnittstelle fuer den Bot. Spot kennt nur Long.
    def open_position(self, symbol: str, side: str, amount: float) -> Fill:
        if side != "long":
            raise ValueError("Spot-Handel kann keine Short-Positionen eroeffnen, dafuer market: futures verwenden")
        return self.buy(symbol, amount)

    def close_position(self, symbol: str, side: str, amount: float) -> Fill:
        if side != "long":
            raise ValueError("Spot-Handel hat keine Short-Positionen")
        return self.sell(symbol, amount)

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

    # ------------------------------------------------------- Stop-Loss bei der Boerse
    _PARAM_VARIANTS = (None, {"trigger": True}, {"stop": True})

    def supports_exchange_stop(self) -> bool:
        return bool(self.exchange.has.get("createStopLossOrder"))

    def place_stop(self, symbol: str, amount: float, stop_price: float) -> str | None:
        """Hinterlegt einen Stop-Loss (Market-Verkauf bei Erreichen des Preises) direkt bei der Boerse."""
        if not self.supports_exchange_stop():
            return None
        try:
            amount_str = self.exchange.amount_to_precision(symbol, amount)
            price_str = self.exchange.price_to_precision(symbol, stop_price)
            order = self.exchange.create_order(symbol, "market", "sell", float(amount_str), None, {"stopLossPrice": float(price_str)})
            log.warning("Stop-Loss bei der Boerse hinterlegt: %s %s @ %s (Order %s)", amount_str, symbol, price_str, order.get("id"))
            return str(order["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Stop-Loss bei der Boerse fuer %s nicht moeglich: %s", symbol, exc)
            return None

    def _fetch_stop_order(self, symbol: str, order_id: str) -> dict | None:
        for params in self._PARAM_VARIANTS:
            try:
                return self.exchange.fetch_order(order_id, symbol, params) if params else self.exchange.fetch_order(order_id, symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("fetch_order %s mit %s fehlgeschlagen: %s", order_id, params, exc)
        return None

    def stop_status(self, symbol: str, order_id: str) -> tuple[str, Fill | None]:
        order = self._fetch_stop_order(symbol, order_id)
        if order is None:
            return "unknown", None
        status = order.get("status")
        if status == "closed" and float(order.get("filled") or 0) > 0:
            return "filled", self._order_to_fill(symbol, "sell", order, float(order.get("filled") or 0))
        if status in ("canceled", "cancelled", "expired", "rejected"):
            return "gone", None
        return "open", None

    def cancel_stop(self, symbol: str, order_id: str) -> str:
        """Gibt 'canceled', 'filled' (schon ausgeloest), 'gone' (nicht mehr vorhanden) oder 'failed' zurueck."""
        for params in self._PARAM_VARIANTS:
            try:
                if params:
                    self.exchange.cancel_order(order_id, symbol, params)
                else:
                    self.exchange.cancel_order(order_id, symbol)
                return "canceled"
            except Exception as exc:  # noqa: BLE001
                log.debug("cancel_order %s mit %s fehlgeschlagen: %s", order_id, params, exc)
        status, _ = self.stop_status(symbol, order_id)
        return {"filled": "filled", "gone": "gone"}.get(status, "failed")
