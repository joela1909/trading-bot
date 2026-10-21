from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .data import DataFeed, timeframe_seconds
from .exchange import Broker
from .models import Fill, Position, Signal, Trade, utcnow_iso
from .notify import Notifier
from .protections import Protections
from .risk import RiskManager
from .session import Session, parse_iso
from .snapshot import make_snapshot
from .state import BotState
from .strategy import Strategy

log = logging.getLogger(__name__)


class TradingBot:
    def __init__(
        self,
        settings: Settings,
        feed: DataFeed,
        broker: Broker,
        strategy: Strategy,
        risk: RiskManager,
        state: BotState,
        notifier: Notifier,
        state_path: str | Path,
    ):
        self.settings = settings
        self.cfg = settings.bot
        self.feed = feed
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.state = state
        self.notifier = notifier
        self.state_path = Path(state_path)
        self._stop = False
        self.protections = Protections(self.cfg.protections, timeframe_seconds(self.cfg.timeframe) * 1000)
        self.protections.load(state.protections)
        self.strategy.import_state(state.strategy_state)
        self._save_lock = threading.Lock()
        self.last_prices: dict[str, float] = {}
        self.last_equity: float | None = None
        self.last_free_quote: float | None = None
        self.last_cycle: str | None = None
        self.running = False
        self._snapshot: dict | None = None
        self.session: Session | None = Session(self.cfg.daytrading) if self.cfg.daytrading.enabled else None

    # ----------------------------------------------------------------- helpers
    def equity(self, prices: dict[str, float] | None = None) -> float:
        eq = self.broker.balance_quote()
        for symbol, pos in self.state.positions.items():
            price = prices.get(symbol) if prices and symbol in prices else self.feed.fetch_price(symbol)
            eq += pos.amount * price
        return eq

    def _save(self) -> None:
        with self._save_lock:
            self.state.protections = self.protections.to_dict()
            self.state.strategy_state = self.strategy.export_state()
            self.state.save(self.state_path)

    EQUITY_POINT_SECONDS = 300
    EQUITY_MAX_POINTS = 6000

    def _record_equity(self, equity: float) -> None:
        history = self.state.equity_history
        now = datetime.now(timezone.utc)
        if history:
            try:
                last = datetime.fromisoformat(history[-1][0])
                if (now - last).total_seconds() < self.EQUITY_POINT_SECONDS:
                    return
            except ValueError:
                pass
        history.append([now.isoformat(timespec="seconds"), round(equity, 4)])
        if len(history) > self.EQUITY_MAX_POINTS:
            half = len(history) // 2
            history[:] = history[:half:2] + history[half:]

    def _ai_meta(self) -> dict | None:
        if not getattr(self.strategy, "live_only", False):
            return None
        ai = getattr(self.strategy, "ai", None)
        if ai is None:
            return None
        return {"model": ai.model, "effort": ai.effort, "max_calls": ai.max_calls_per_day, "min_confidence": ai.min_confidence}

    def _context(self, symbol: str, price: float, pos: Position | None) -> dict:
        position = None
        if pos is not None:
            pnl_pct = (price / pos.entry_price - 1.0) * 100.0 if pos.entry_price else 0.0
            position = {
                "amount": pos.amount,
                "entry_price": pos.entry_price,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "pnl_pct": pnl_pct,
            }
        return {
            "symbol": symbol,
            "price": price,
            "position": position,
            "equity": self.last_equity,
            "free_quote": self.last_free_quote,
            "quote": self.cfg.quote_currency,
            "timeframe": self.cfg.timeframe,
            "open_positions": len(self.state.positions),
        }

    def build_snapshot(self) -> dict:
        self._snapshot = make_snapshot(
            state=self.state,
            mode=self.broker.name,
            exchange=self.cfg.exchange,
            timeframe=self.cfg.timeframe,
            quote=self.cfg.quote_currency,
            symbols=self.cfg.symbols,
            equity=self.last_equity,
            free_quote=self.last_free_quote,
            prices=self.last_prices,
            last_cycle=self.last_cycle,
            running=self.running,
            controls=True,
            ai_meta=self._ai_meta(),
        )
        return self._snapshot

    def get_snapshot(self) -> dict:
        return self._snapshot or self.build_snapshot()

    def pause(self) -> dict:
        self.state.entries_paused = True
        self._save()
        log.warning("Neue Einstiege pausiert (Dashboard)")
        return self.build_snapshot()

    def resume(self) -> dict:
        self.state.entries_paused = False
        self._save()
        log.warning("Neue Einstiege wieder aktiv (Dashboard)")
        return self.build_snapshot()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _roll_day(self, equity: float) -> None:
        now = self._now()
        today = self.session.day(now) if self.session else now.strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day = today
            self.state.day_start_equity = equity
            self.state.entries_halted_for_day = False
            self.state.entries_today = 0
            log.info("Neuer Handelstag %s, Startkapital %.2f %s", today, equity, self.cfg.quote_currency)

    def reconcile(self) -> None:
        """Gleicht gespeicherte Positionen mit den tatsaechlichen Bestaenden ab (z. B. nach manuellem Verkauf)."""
        for symbol, pos in list(self.state.positions.items()):
            try:
                held = self.broker.balance_base(symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("Bestand fuer %s nicht abrufbar: %s", symbol, exc)
                continue
            if held < pos.amount * 0.99:
                if held * pos.entry_price < self.cfg.risk.min_order_quote:
                    log.warning("%s: Position laut Zustand %.8f, aber Bestand %.8f. Position wird verworfen.", symbol, pos.amount, held)
                    del self.state.positions[symbol]
                else:
                    log.warning("%s: Positionsgroesse von %.8f auf Bestand %.8f korrigiert.", symbol, pos.amount, held)
                    pos.amount = held
        self._save()

    # ------------------------------------------------------------------ cycle
    def run_once(self) -> float:
        prices: dict[str, float] = {}
        for symbol in self.cfg.symbols:
            try:
                prices[symbol] = self.feed.fetch_price(symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("Kurs fuer %s nicht abrufbar: %s", symbol, exc)

        equity = self.equity(prices) if prices else (self.state.day_start_equity or 0.0)
        self._roll_day(equity)

        for symbol in self.cfg.symbols:
            if symbol not in prices:
                continue
            try:
                self.process_symbol(symbol, prices[symbol])
            except Exception:  # noqa: BLE001
                log.exception("Fehler bei %s", symbol)

        final_equity = self.equity(prices) if prices else equity
        if prices:
            self.protections.update_equity(final_equity, int(time.time() * 1000))
        self.last_prices = dict(prices)
        self.last_equity = final_equity
        try:
            self.last_free_quote = self.broker.balance_quote()
        except Exception as exc:  # noqa: BLE001
            log.debug("Guthaben nicht abrufbar: %s", exc)
        self.last_cycle = utcnow_iso()
        if prices:
            self._record_equity(final_equity)
        self._save()
        self.build_snapshot()
        return final_equity

    def process_symbol(self, symbol: str, price: float) -> None:
        df = self.feed.fetch_ohlcv(symbol, self.cfg.timeframe, limit=self.strategy.warmup_bars + 10)
        if df.empty:
            log.warning("%s: keine Kerzen erhalten", symbol)
            return
        df = self.strategy.prepare(df)
        last_ts = int(df["timestamp"].iloc[-1])
        is_new_candle = self.state.last_candle_ts.get(symbol) != last_ts

        pos = self.state.positions.get(symbol)
        if pos is not None:
            self._manage_position(symbol, pos, price, df, is_new_candle)
        elif is_new_candle:
            self._maybe_enter(symbol, price, df)

        self.state.last_candle_ts[symbol] = last_ts

    # --------------------------------------------------- Stop-Loss bei der Boerse
    def _exchange_stops_enabled(self) -> bool:
        return bool(self.cfg.execution.stoploss_on_exchange and self.broker.supports_exchange_stop())

    def _arm_exchange_stop(self, symbol: str, pos: Position) -> None:
        if not self._exchange_stops_enabled():
            return
        order_id = self.broker.place_stop(symbol, pos.amount, pos.stop_loss)
        if order_id:
            pos.stop_order_id, pos.exchange_stop = order_id, pos.stop_loss
        else:
            pos.stop_order_id = pos.exchange_stop = None
            self.notifier.send(f"WARNUNG {symbol}: Stop-Loss konnte nicht bei der Boerse hinterlegt werden. Es gilt nur der Stop im Bot.")

    def _sync_exchange_stop(self, symbol: str, pos: Position) -> bool:
        """Prueft die hinterlegte Stop-Order. True, wenn sie ausgeloest wurde und die Position damit geschlossen ist."""
        if not pos.stop_order_id:
            if self._exchange_stops_enabled():
                self._arm_exchange_stop(symbol, pos)
            return False
        status, fill = self.broker.stop_status(symbol, pos.stop_order_id)
        if status == "filled" and fill is not None:
            self._close_position(symbol, pos, "stop_loss (Boerse)", fill=fill)
            return True
        if status == "gone":
            log.warning("%s: Stop-Order bei der Boerse nicht mehr vorhanden, wird neu gesetzt", symbol)
            pos.stop_order_id = pos.exchange_stop = None
            self._arm_exchange_stop(symbol, pos)
        elif status == "unknown":
            log.warning("%s: Status der Stop-Order unbekannt, der Stop im Bot bleibt aktiv", symbol)
        return False

    def _raise_exchange_stop(self, symbol: str, pos: Position) -> bool:
        """Zieht die Stop-Order bei der Boerse nach. True, wenn sie dabei schon ausgeloest war und die Position geschlossen ist."""
        if not pos.stop_order_id or pos.exchange_stop is None:
            return False
        threshold = self.cfg.execution.stoploss_replace_threshold_pct / 100.0
        if pos.stop_loss <= pos.exchange_stop * (1.0 + threshold):
            return False
        result = self.broker.cancel_stop(symbol, pos.stop_order_id)
        if result == "filled":
            status, fill = self.broker.stop_status(symbol, pos.stop_order_id)
            if fill is not None:
                self._close_position(symbol, pos, "stop_loss (Boerse)", fill=fill)
                return True
            return False
        if result == "failed":
            log.warning("%s: alte Stop-Order liess sich nicht stornieren, sie bleibt bestehen", symbol)
            return False
        pos.stop_order_id = pos.exchange_stop = None
        self._arm_exchange_stop(symbol, pos)
        return False

    def _manage_position(self, symbol: str, pos: Position, price: float, df, is_new_candle: bool) -> None:
        if self._sync_exchange_stop(symbol, pos):
            return
        if self.session is not None:
            reason = self.session.must_close(self._now(), parse_iso(pos.opened_at))
            if reason:
                log.info("%s: Tageshandel, Position wird geschlossen (%s)", symbol, reason)
                self._close_position(symbol, pos, reason)
                return
        if price > pos.highest_price:
            pos.highest_price = price
            if self.cfg.strategy.trailing_stop:
                atr_now = float(df["atr"].iloc[-1])
                if not math.isnan(atr_now) and atr_now > 0:
                    new_stop = self.strategy.trailing_stop(pos.highest_price, atr_now)
                    if new_stop > pos.stop_loss:
                        log.info("%s: Trailing-Stop %.4f -> %.4f", symbol, pos.stop_loss, new_stop)
                        pos.stop_loss = new_stop
                        if self._raise_exchange_stop(symbol, pos):
                            return

        if price <= pos.stop_loss:
            self._close_position(symbol, pos, "stop_loss")
            return
        if pos.take_profit is not None and price >= pos.take_profit:
            self._close_position(symbol, pos, "take_profit")
            return
        if is_new_candle:
            decision = self.strategy.decide(df, context=self._context(symbol, price, pos))
            if decision.signal == Signal.SELL:
                self._close_position(symbol, pos, f"signal: {decision.reason}")

    def _maybe_enter(self, symbol: str, price: float, df) -> None:
        if self.state.entries_paused:
            log.info("%s: Einstiege pausiert", symbol)
            return
        if self.state.entries_halted_for_day:
            log.info("%s: Tagesverlustlimit erreicht, kein Einstieg", symbol)
            return
        if self.session is not None:
            dt = self.cfg.daytrading
            if not self.session.entry_allowed(self._now()):
                log.info("%s: kein Einstieg, ausserhalb der Einstiegszeit %s-%s", symbol, dt.entry_start, dt.entry_end)
                return
            if dt.max_trades_per_day and self.state.entries_today >= dt.max_trades_per_day:
                log.info("%s: kein Einstieg, max. %d Einstiege pro Tag erreicht", symbol, dt.max_trades_per_day)
                return
        allowed, why = self.protections.entry_allowed(symbol, int(time.time() * 1000))
        if not allowed:
            log.info("%s: kein Einstieg, %s", symbol, why)
            return
        if not self.risk.can_open_new(len(self.state.positions)):
            log.info("%s: kein Einstieg, max. offene Positionen erreicht", symbol)
            return
        decision = self.strategy.decide(df, context=self._context(symbol, price, None))
        if decision.signal != Signal.BUY:
            log.debug("%s: %s (%s)", symbol, decision.signal.value, decision.reason)
            return
        if decision.atr is None or decision.atr <= 0:
            return

        equity = self.equity()
        free_quote = self.broker.balance_quote()
        stop = price - self.cfg.strategy.stop_atr_mult * decision.atr
        amount = self.risk.position_size(equity, free_quote, price, stop, self.broker.limits(symbol))
        if amount <= 0:
            log.info("%s: Kaufsignal, aber Positionsgroesse unter Mindestorder (Kapital %.2f)", symbol, equity)
            return

        fill = self.broker.buy(symbol, amount)
        entry_stop = fill.price - self.cfg.strategy.stop_atr_mult * decision.atr
        tp_mult = self.cfg.strategy.take_profit_atr_mult
        take_profit = fill.price + tp_mult * decision.atr if tp_mult > 0 else None
        pos = Position(
            symbol=symbol,
            amount=fill.amount,
            entry_price=fill.price,
            stop_loss=entry_stop,
            take_profit=take_profit,
            opened_at=fill.timestamp,
            highest_price=fill.price,
            entry_fee=fill.fee,
            atr_at_entry=decision.atr,
        )
        self.state.positions[symbol] = pos
        self.state.entries_today += 1
        self._arm_exchange_stop(symbol, pos)
        self._save()
        tp_text = f"{take_profit:.4f}" if take_profit is not None else "aus"
        msg = (
            f"KAUF {symbol}: {fill.amount:.6f} @ {fill.price:.4f} "
            f"(Stop {entry_stop:.4f}, TP {tp_text}, {decision.reason})"
        )
        self.notifier.send(msg)

    def _close_position(self, symbol: str, pos: Position, reason: str, fill: Fill | None = None) -> None:
        if fill is None and pos.stop_order_id:
            result = self.broker.cancel_stop(symbol, pos.stop_order_id)
            if result == "filled":
                status, stop_fill = self.broker.stop_status(symbol, pos.stop_order_id)
                if stop_fill is not None:
                    fill, reason = stop_fill, "stop_loss (Boerse)"
            elif result == "failed":
                log.warning("%s: Stop-Order bei der Boerse liess sich nicht stornieren, der Verkauf kann scheitern", symbol)
            if fill is None:
                pos.stop_order_id = pos.exchange_stop = None
        if fill is None:
            fill = self.broker.sell(symbol, pos.amount)
        fees = fill.fee + pos.entry_fee
        pnl = (fill.price - pos.entry_price) * pos.amount - fees
        invested = pos.entry_price * pos.amount
        pnl_pct = pnl / invested * 100.0 if invested else 0.0
        trade = Trade(
            symbol=symbol,
            amount=pos.amount,
            entry_price=pos.entry_price,
            exit_price=fill.price,
            pnl_quote=pnl,
            pnl_pct=pnl_pct,
            fees=fees,
            opened_at=pos.opened_at,
            closed_at=fill.timestamp,
            reason=reason,
        )
        self.state.trades.append(trade)
        self.protections.on_exit(symbol, reason, int(time.time() * 1000))
        del self.state.positions[symbol]
        self._save()
        self.notifier.send(
            f"VERKAUF {symbol}: {pos.amount:.6f} @ {fill.price:.4f} -> PnL {pnl:+.2f} {self.cfg.quote_currency} ({pnl_pct:+.2f}%), Grund: {reason}"
        )

        if self.state.day_start_equity and not self.state.entries_halted_for_day:
            equity = self.equity()
            if self.risk.daily_loss_exceeded(self.state.day_start_equity, equity):
                self.state.entries_halted_for_day = True
                self._save()
                self.notifier.send(
                    f"Tagesverlustlimit erreicht ({equity:.2f} von {self.state.day_start_equity:.2f}). "
                    "Keine neuen Einstiege mehr bis zum naechsten Tag."
                )

    # ------------------------------------------------------------------- loop
    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        self.reconcile()
        self.running = True
        self.build_snapshot()
        self.notifier.send(
            f"Bot gestartet ({self.broker.name}-Modus, {self.cfg.exchange}, {', '.join(self.cfg.symbols)}, {self.cfg.timeframe})"
            + (
                f", Tageshandel {self.cfg.daytrading.entry_start}-{self.cfg.daytrading.entry_end}, "
                f"flat ab {self.cfg.daytrading.flat_time} {self.cfg.daytrading.timezone}"
                if self.session
                else ""
            )
        )
        interval = max(5, self.cfg.poll_interval_seconds)
        while not self._stop:
            started = time.monotonic()
            try:
                equity = self.run_once()
                log.info(
                    "Zyklus fertig: Kapital %.2f %s, offene Positionen %d, Trades gesamt %d",
                    equity, self.cfg.quote_currency, len(self.state.positions), len(self.state.trades),
                )
            except Exception:  # noqa: BLE001
                log.exception("Unerwarteter Fehler im Zyklus")
            remaining = interval - (time.monotonic() - started)
            while remaining > 0 and not self._stop:
                time.sleep(min(1.0, remaining))
                remaining -= 1.0
        self.running = False
        self.build_snapshot()
        self.notifier.send(f"Bot gestoppt um {utcnow_iso()}")
