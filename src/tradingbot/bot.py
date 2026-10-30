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
from .models import Fill, Position, Signal, Trade, side_sign, utcnow_iso
from .notify import Notifier
from .protections import Protections
from .risk import RiskManager, cap_stop
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
        # je Paar: abgeschlossene Kerzen und das fertig gerechnete Ergebnis der Strategie fuer die letzte Kerze
        self._candles: dict = {}
        self._prepared: dict = {}
        self._live_prepared: dict = {}  # Echtzeit: (letzte geschlossene Kerze, df, Zeitpunkt, letzte Kerze) je Paar
        # Chart in anderen Zeitrahmen: eigener Datenzugang (wird in build_bot gesetzt) und kurzer Zwischenspeicher
        self.chart_feed = None
        self.signal_feed = None  # eigener Datenzugang fuer Signal-Kerzen (z. B. Spot), sonst der Handels-Feed
        self._chart_cache: dict = {}
        self._reset_request: bool | None = None
        self._fee_request = False
        # Zusatzpruefung vor jedem Einstieg: (Paar, Richtung) -> (erlaubt, Grund); z. B. News-Richtung
        self.entry_guard = None
        self._track_cache: dict[str, tuple[int, list[float]]] = {}
        # Zusatzwissen fuer die Strategie je Paar, z. B. Liquiditaet fuer die KI: (Paar, Kerzen) -> Liste von Saetzen
        self.context_extra = None
        # Gedaechtnis ueber alle Zeitrahmen (MultiBot): () -> [(Trade, Zeitrahmen)], und Lehren der Fehleranalyse
        self.trade_memory = None
        self.lessons = None
        # Vorpruefung vor der Strategie (spart KI-Aufrufe): Paar -> (erlaubt, Grund), z. B. Leistungsbremse aller Konten
        self.entry_precheck = None
        if hasattr(self.strategy, "cost_pct"):
            self.strategy.cost_pct = 2.0 * (self.cfg.paper.fee_pct + self.cfg.paper.slippage_pct)
        # wird zu Beginn jeder Runde aufgerufen, z. B. um die Aktienliste des Tages zu erneuern
        self.before_cycle = None
        # Echtzeit: (Kerze, Richtung) des letzten Ausstiegs je Paar, "any" nach einem Einstieg
        self._acted_candle: dict[str, tuple[int, str]] = {}
        # Marktansicht fuers Dashboard: was der Bot je Paar zuletzt gesehen und entschieden hat
        self.views: dict[str, dict] = {}
        self._status: dict[str, str] = {}
        self.exit_code = 0  # 3 = Neustart gewuenscht (neue Einstellungen aus der App)
        self.session: Session | None = Session(self.cfg.daytrading) if self.cfg.daytrading.enabled else None

    # ----------------------------------------------------------------- helpers
    def equity(self, prices: dict[str, float] | None = None) -> float:
        eq = self.broker.balance_quote()
        for symbol, pos in self.state.positions.items():
            price = prices.get(symbol) if prices and symbol in prices else self.feed.fetch_price(symbol)
            eq += pos.value(price)
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
            pnl_pct = side_sign(pos.side) * (price / pos.entry_price - 1.0) * 100.0 if pos.entry_price else 0.0
            position = {
                "amount": pos.amount,
                "side": pos.side,
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
            "liquidity": self._extra(symbol),
            "cost_pct": 2.0 * (self.cfg.paper.fee_pct + self.cfg.paper.slippage_pct),
            "recent_trades": self._recent_trades(symbol),
            "lessons": self.lessons() if self.lessons is not None and getattr(self.strategy, "live_only", False) else [],
        }

    def _recent_trades(self, symbol: str) -> list[str]:
        """Die letzten eigenen Trades als kurze Saetze (fuer die KI): dieses Paar zuerst, dann die uebrigen."""
        n = getattr(getattr(self.cfg, "ai", None), "memory_trades", 0)
        if not n or not getattr(self.strategy, "live_only", False):
            return []
        # mit MultiBot die Trades aller Zeitrahmen (ein 4h-Stop ist auch fuer 1m eine Lehre), sonst die eigenen
        pool = self.trade_memory() if self.trade_memory is not None else [(t, "") for t in self.state.trades]
        trades = [x for x in pool if x[0].symbol == symbol][-n // 2:] or []
        rest = n - len(trades)
        if rest > 0:
            trades += [x for x in pool if x[0].symbol != symbol][-rest:]
        out = []
        for t, tf in sorted(trades, key=lambda x: x[0].closed_at)[-n:]:
            mins = (parse_iso(t.closed_at) - parse_iso(t.opened_at)).total_seconds() / 60
            out.append(f"{tf + ' ' if tf else ''}{t.symbol.split('/')[0]} {t.side} {t.opened_at[5:16]}: Einstieg {t.entry_price:.6g}, Ausstieg {t.exit_price:.6g}, "
                       f"{t.pnl_pct:+.2f} % nach Kosten, {mins:.0f} min, Grund: {t.reason[:60]}")
        return out

    def _extra(self, symbol: str) -> list[str]:
        if self.context_extra is None:
            return []
        try:
            return self.context_extra(symbol, self._candles.get(symbol))
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: Zusatzwissen nicht abrufbar (%s)", symbol, exc)
            return []

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
            market=[self._view_summary(self.views[s]) for s in self.cfg.symbols if s in self.views],
            exit_cost_rate=self._exit_cost_rate(),
            tz=self.cfg.daytrading.timezone,
        )
        return self._snapshot

    @staticmethod
    def _view_summary(view: dict) -> dict:
        """Marktansicht ohne Kerzen: fuer die Uebersicht, die alle paar Sekunden neu laedt."""
        return {k: v for k, v in view.items() if k not in ("candles", "lines")}

    CHART_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
    CHART_CANDLES = 500

    def chart(self, symbol: str, timeframe: str | None = None, since: int | None = None) -> dict:
        out = self._chart_full(symbol, timeframe)
        return self._chart_tail(out, since) if since else out

    def _chart_full(self, symbol: str, timeframe: str | None = None) -> dict:
        """Kerzen, Linien und Trades eines Paars fuer den interaktiven Chart.

        Mit `since` (ms) nur die Kerzen ab diesem Zeitpunkt samt Linien, ohne Trades und Signal-Marken
        ("delta": true). Liegt `since` vor der ersten Kerze, kommt alles.

        Im Zeitrahmen des Bots kommt alles, was er sieht (mit den Linien der Strategie). In anderen Zeitrahmen
        (bis 1 Tag) laedt der Chart die Kerzen eigens, die Linien der Strategie gibt es dort nicht."""
        view = self.views.get(symbol)
        trades = [t.to_dict() for t in self.state.trades if t.symbol == symbol][-200:]
        tf = timeframe or self.cfg.timeframe
        if tf == self.cfg.timeframe:
            if view is None:
                return {"symbol": symbol, "timeframe": tf, "candles": [], "lines": [], "trades": trades, "position": None}
            out = {**view, "trades": trades}
            # Der Bot rechnet nur mit abgeschlossenen Kerzen. Fuer den Chart die laufende Kerze live anhaengen,
            # die Linien bekommen dort Luecken (die Strategie kennt die Kerze ja noch nicht).
            extra = self._live_candles(symbol, tf, view["candles"][-1][0] if view["candles"] else None)
            if extra:
                out["candles"] = view["candles"] + extra
                lines = []
                for line in view["lines"]:
                    line = {**line, "values": line["values"] + [None] * len(extra)}
                    if "directions" in line:
                        line["directions"] = line["directions"] + [None] * len(extra)
                    lines.append(line)
                out["lines"] = lines
            return out
        if tf not in self.CHART_TIMEFRAMES:
            raise ValueError(f"Zeitrahmen {tf} gibt es nicht")
        candles = list(self._chart_candles(symbol, tf))
        extra = self._live_candles(symbol, tf, candles[-1][0] if candles else None, inclusive=True)
        if extra:
            candles = [k for k in candles if k[0] < extra[0][0]] + extra
        base = view or {"symbol": symbol, "position": None, "explain": [], "status": ""}
        return {
            **base, "timeframe": tf, "candles": candles, "lines": [], "trades": trades,
            "lines_note": f"Linien der Strategie nur im Zeitrahmen des Bots ({self.cfg.timeframe})",
        }

    LIVE_TTL = 1.5  # Sekunden: so oft holt der Chart hoechstens die juengsten Kerzen neu

    @staticmethod
    def _chart_tail(out: dict, since: int) -> dict:
        candles = out.get("candles") or []
        k = next((i for i, c in enumerate(candles) if c[0] >= since), len(candles))
        if k == 0:
            return out
        lines = []
        for line in out.get("lines") or []:
            line = {**line, "values": line["values"][k:]}
            if "directions" in line:
                line["directions"] = line["directions"][k:]
            lines.append(line)
        tail = {key: v for key, v in out.items() if key not in ("trades", "markers")}
        return {**tail, "candles": candles[k:], "lines": lines, "delta": True}

    def _chart_symbol(self, symbol: str) -> str:
        # Chart zeigt dieselben Kerzen wie die Signale (bei Spot-Signalen das Spot-Paar)
        return symbol.split(":")[0] if self.signal_feed is not None else symbol

    def _live_candles(self, symbol: str, tf: str, last_ts: int | None, inclusive: bool = False) -> list:
        """Die juengsten Kerzen inklusive der laufenden, nur die nach `last_ts` (bzw. ab `last_ts`)."""
        now = time.time()
        key = ("live", symbol, tf)
        hit = self._chart_cache.get(key)
        if hit is None or now - hit[0] >= self.LIVE_TTL:
            feed = self.chart_feed or self.feed
            fetch = getattr(feed, "fetch_chart", None)
            if fetch is None:
                return []
            try:
                df = fetch(self._chart_symbol(symbol), tf, limit=3)
            except Exception as exc:  # noqa: BLE001 - der Chart darf nie den Bot stoeren
                log.debug("%s: laufende Kerze nicht abrufbar (%s)", symbol, exc)
                return []
            rows = [[int(t), float(o), float(h), float(lo), float(c)] for t, o, h, lo, c in df[["timestamp", "open", "high", "low", "close"]].itertuples(index=False)]
            self._chart_cache[key] = (now, rows)
        else:
            rows = hit[1]
        if last_ts is None:
            return rows
        return [r for r in rows if (r[0] >= last_ts if inclusive else r[0] > last_ts)]

    def _chart_candles(self, symbol: str, tf: str) -> list:
        """Kerzen fuer den Chart, kurz zwischengespeichert. Eigener Datenzugang, damit der Chart den Handel nie bremst."""
        tf_s = timeframe_seconds(tf)
        ttl = 15 if tf_s <= 900 else 60
        now = time.time()
        hit = self._chart_cache.get((symbol, tf))
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        feed = self.chart_feed or self.feed
        # mit der laufenden Kerze, falls der Datenzugang das kann (bei 1 Tag sonst nur bis gestern)
        fetch = getattr(feed, "fetch_chart", None) or feed.fetch_ohlcv
        df = fetch(self._chart_symbol(symbol), tf, limit=self.CHART_CANDLES)
        candles = [[int(t), float(o), float(h), float(lo), float(c)] for t, o, h, lo, c in df[["timestamp", "open", "high", "low", "close"]].itertuples(index=False)]
        self._chart_cache[(symbol, tf)] = (now, candles)
        return candles

    def _exit_cost_rate(self) -> float:
        return (self.cfg.paper.fee_pct + self.cfg.paper.slippage_pct) / 100.0

    def _risk_with_costs(self, entry: float, stop: float, amount: float, entry_fee: float) -> float:
        """Was ein Stop-Loss wirklich kostet: Kursabstand plus Einstiegsgebuehr plus Gebuehr und Abweichung beim Ausstieg."""
        return abs(entry - stop) * amount + entry_fee + amount * stop * self._exit_cost_rate()

    def request_restart(self) -> None:
        """Beendet den Bot mit Code 3. start-24-7.bat, Docker und Cloudflare starten ihn dann neu."""
        log.warning("Neustart angefordert (neue Einstellungen)")
        self.exit_code = 3
        self.stop()

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

    def request_account_reset(self, clear_trades: bool) -> dict:
        """Konto-Reset anfordern. Er laeuft zu Beginn der naechsten Runde im Bot-Thread, damit er sich nicht mit
        einer laufenden Runde ueberschneidet."""
        if self.broker.name != "paper" or not hasattr(self.broker, "reset"):
            raise ValueError("Zuruecksetzen geht nur im Papierhandel")
        self._reset_request = bool(clear_trades)
        log.warning("Konto-Reset angefordert (Trades loeschen: %s)", clear_trades)
        return {"ok": True, "pending": True, "positions": len(self.state.positions)}

    def _track_record(self, symbol: str, df) -> tuple[bool, str]:
        """Bilanz-Filter: dieselbe Strategie auf den Kerzen vor dem Signal nachspielen (Backtest mit den aktuellen
        Einstellungen) und die letzten K Trades ansehen. Ergebnis je Paar und Kerze zwischengespeichert."""
        k = self.cfg.protections.track_record_trades
        if k <= 0 or getattr(self.strategy, "live_only", False):  # KI laesst sich nicht nachspielen
            return True, ""
        last_ts = int(df["timestamp"].iloc[-1])
        hit = self._track_cache.get(symbol)
        if hit is None or hit[0] != last_ts:
            from .backtest import Backtester

            past = df.iloc[:-1].reset_index(drop=True)  # nur abgeschlossene Kerzen vor dem Signal
            start = self.strategy.warmup_bars
            pnl: list[float] = []
            if len(past) > start + 10:
                try:
                    bt = Backtester(self.strategy, RiskManager(self.cfg.risk), self.cfg.strategy, self.cfg.paper,
                                    daytrading=self.cfg.daytrading if self.session is not None else None,
                                    leverage=self.cfg.leverage)
                    pnl = [t.pnl_pct for t in bt.run(past, symbol, start=start, prepared=True).trades
                           if t.reason != "end_of_data"]
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: Bilanz-Filter nicht berechenbar (%s), Einstieg erlaubt", symbol, exc)
            self._track_cache[symbol] = hit = (last_ts, pnl)
        pnl = hit[1][-k:]
        if len(pnl) < k:
            return True, ""
        avg = sum(pnl) / len(pnl)
        if avg > 0:
            return True, ""
        return False, f"Bilanz-Filter: die letzten {k} Trades dieser Regel im Schnitt {avg:+.2f} %".replace(".", ",")

    def request_fee_reprice(self) -> dict:
        """Neuberechnung der Gebuehren anfordern; laeuft zu Beginn der naechsten Runde im Bot-Thread."""
        if self.broker.name != "paper":
            raise ValueError("Gebuehren nachrechnen geht nur im Papierhandel")
        self._fee_request = True
        log.warning("Gebuehren-Neuberechnung angefordert (%.3f %% je Order)", self.cfg.paper.fee_pct)
        return {"ok": True, "pending": True, "fee_pct": self.cfg.paper.fee_pct, "trades": len(self.state.trades)}

    def reprice_fees(self) -> dict:
        """Rechnet die Gebuehren aller gespeicherten Trades und offenen Positionen mit dem aktuellen Satz
        (paper.fee_pct) neu, als waeren sie schon damit gehandelt worden. Gewinn, Kapital, Tagesstart und
        Kapitalverlauf ziehen um den Unterschied nach; Kurse und Mengen bleiben."""
        rate = self.cfg.paper.fee_pct / 100.0
        changes: list[tuple[datetime, float]] = []
        for t in self.state.trades:
            new = (t.entry_price + t.exit_price) * t.amount * rate
            delta = t.fees - new
            if abs(delta) < 1e-12:
                continue
            t.fees = new
            t.pnl_quote += delta
            t.pnl_pct = t.pnl_quote / (t.entry_price * t.amount) * 100.0 if t.entry_price * t.amount else 0.0
            changes.append((parse_iso(t.closed_at), delta))
        for pos in self.state.positions.values():
            new = pos.entry_price * pos.amount * rate
            delta = pos.entry_fee - new
            if abs(delta) >= 1e-12:
                pos.entry_fee = new
                changes.append((parse_iso(pos.opened_at), delta))
        total = sum(d for _, d in changes)
        if hasattr(self.broker, "cash"):
            self.broker.cash += total
        else:
            self.broker.balances[self.broker.quote] = self.broker.balance_quote() + total
        self.broker._save()
        # Tagesstart: nur um Trades vor dem heutigen Tagesbeginn verschieben
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self.cfg.daytrading.timezone) if self.session is not None else timezone.utc
        day_start = self._now().astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.state.day_start_equity:
            self.state.day_start_equity += sum(d for ts, d in changes if ts < day_start)
        for point in self.state.equity_history:
            try:
                at = datetime.fromisoformat(point[0])
            except ValueError:
                continue
            point[1] = round(point[1] + sum(d for ts, d in changes if ts <= at), 4)
        self._save()
        self.build_snapshot()
        log.warning("Gebuehren neu berechnet (%.3f %%): %d Trades/Positionen, Kapital %+.2f %s",
                    self.cfg.paper.fee_pct, len(changes), total, self.cfg.quote_currency)
        return {"changed": len(changes), "delta": total}

    def _do_account_reset(self, clear_trades: bool) -> None:
        for symbol, pos in list(self.state.positions.items()):
            try:
                self._close_position(symbol, pos, "reset")
            except Exception:  # noqa: BLE001
                log.exception("%s: Position liess sich beim Reset nicht schliessen", symbol)
                self.state.positions.pop(symbol, None)
        self.broker.reset(self.cfg.paper.starting_balance)
        if clear_trades:
            self.state.trades = []
        self.state.equity_history = []
        self.state.day_start_equity = None
        self.state.day = None
        self.state.entries_halted_for_day = False
        self.state.entries_today = 0
        self.protections = Protections(self.cfg.protections, timeframe_seconds(self.cfg.timeframe) * 1000)
        self.last_equity = None
        self._save()
        log.warning("Konto auf %.2f %s zurueckgesetzt", self.cfg.paper.starting_balance, self.cfg.quote_currency)

    def delete_oldest_trades(self, count: int) -> dict:
        """Entfernt die aeltesten abgeschlossenen Trades aus dem Verlauf. Kapital und offene Positionen bleiben."""
        count = max(0, min(int(count), len(self.state.trades)))
        removed = self.state.trades[:count]
        del self.state.trades[:count]
        self._save()
        self.build_snapshot()
        log.warning("%d Trades aus dem Verlauf geloescht (Dashboard)", count)
        return {
            "deleted": count,
            "remaining": len(self.state.trades),
            "from": removed[0].closed_at if removed else None,
            "to": removed[-1].closed_at if removed else None,
        }

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
        if self.before_cycle is not None:
            try:
                self.before_cycle()
            except Exception as exc:  # noqa: BLE001
                log.warning("Vorbereitung der Runde fehlgeschlagen: %s", exc)
        if self._reset_request is not None:
            clear, self._reset_request = self._reset_request, None
            self._do_account_reset(clear)
        if self._fee_request:
            self._fee_request = False
            self.reprice_fees()
        prices: dict[str, float] = {}
        for symbol in self.cfg.symbols:
            try:
                prices[symbol] = self.feed.fetch_price(symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("Kurs fuer %s nicht abrufbar: %s", symbol, exc)

        equity = self.equity(prices) if prices else (self.state.day_start_equity or 0.0)
        self._roll_day(equity)

        slow = getattr(self.strategy, "live_only", False)
        for n, symbol in enumerate(self.cfg.symbols):
            if symbol not in prices:
                continue
            if slow and n:
                # KI-Abfragen dauern: vor jedem weiteren Paar Stops/Ziele aller offenen Positionen mit frischem Kurs
                # pruefen und den Kurs dieses Paars neu holen (sonst laufen Stops minutenlang unbeachtet durch)
                self._check_exits(prices)
                try:
                    prices[symbol] = self.feed.fetch_price(symbol)
                except Exception:  # noqa: BLE001
                    pass
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

    CANDLE_TOPUP = 5  # so viele Kerzen holt ein Abruf, wenn der Rest schon im Speicher liegt

    def _fetch_candles(self, symbol: str):
        """Abgeschlossene Kerzen fuer die Strategie. Beim ersten Mal alle, danach nur die neuesten (spart Abrufe:
        bei 1m und drei Tagen Vorlauf sonst 5 Anfragen je Paar und Runde)."""
        import pandas as pd

        need = self.strategy.warmup_bars + 10
        if self.cfg.protections.track_record_trades > 0 and not getattr(self.strategy, "live_only", False):
            # Vorgeschichte fuer den Bilanz-Filter: track_record_bars gilt fuer 1m-Kerzen, groessere entsprechend weniger
            need += max(200, self.cfg.protections.track_record_bars * 60 // timeframe_seconds(self.cfg.timeframe))
        tf = self.cfg.timeframe
        cached = self._candles.get(symbol)
        if cached is not None and len(cached) >= need:
            feed, sym = self._signal_src(symbol)
            new = feed.fetch_ohlcv(sym, tf, limit=self.CANDLE_TOPUP)
            tf_ms = timeframe_seconds(tf) * 1000
            if new.empty or int(new["timestamp"].iloc[0]) <= int(cached["timestamp"].iloc[-1]) + tf_ms:
                df = pd.concat([cached, new], ignore_index=True).drop_duplicates("timestamp", keep="last")
                df = df.sort_values("timestamp").tail(need).reset_index(drop=True)
                self._candles[symbol] = df
                return df
            log.info("%s: Luecke in den Kerzen, lade alle neu", symbol)
        feed, sym = self._signal_src(symbol)
        df = feed.fetch_ohlcv(sym, tf, limit=need)
        self._candles[symbol] = df
        return df

    def _signal_src(self, symbol: str):
        """(Datenzugang, Symbol) fuer die Kerzen der Strategie. Bei Spot-Signalen: BTC/USDT:USDT -> BTC/USDT."""
        if self.signal_feed is not None:
            return self.signal_feed, symbol.split(":")[0]
        return self.feed, symbol

    def _forming_candle(self, symbol: str, after_ts: int):
        """Die noch laufende Kerze (Eroeffnung, Hoch, Tief, aktueller Kurs) nach `after_ts`, sonst None."""
        feed, sym = self._signal_src(symbol)
        fetch = getattr(feed, "fetch_chart", None)
        if fetch is None:
            return None
        try:
            live = fetch(sym, self.cfg.timeframe, limit=2)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: laufende Kerze nicht abrufbar (%s)", symbol, exc)
            return None
        live = live[live["timestamp"] > after_ts]
        return live if not live.empty else None

    def _check_exits(self, prices: dict[str, float]) -> None:
        """Nur Stop und Ziel der offenen Positionen mit aktuellem Kurs pruefen (ohne Strategie, ohne KI)."""
        for symbol, pos in list(self.state.positions.items()):
            try:
                price = self.feed.fetch_price(symbol)
            except Exception:  # noqa: BLE001
                continue
            prices[symbol] = price
            try:
                if pos.stop_hit(price):
                    self._close_position(symbol, pos, "stop_loss")
                elif pos.take_profit_hit(price):
                    self._close_position(symbol, pos, "take_profit")
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Stop von %s", symbol)

    def process_symbol(self, symbol: str, price: float) -> None:
        raw = self._fetch_candles(symbol)
        if raw.empty:
            log.warning("%s: keine Kerzen erhalten", symbol)
            return
        # KI-Strategien fragen das Modell bei jeder Neuberechnung: nur bei Kerzenschluss, nie in Echtzeit
        realtime = self.cfg.strategy.realtime_signals and not getattr(self.strategy, "live_only", False)
        if realtime:
            # Echtzeit: die laufende Kerze rechnet mit. Neu gerechnet wird bei jedem Kerzenschluss und dazwischen
            # hoechstens alle Zehntel-Kerze (1m: 6 s, 15m: 90 s, 1h: 6 min); das spart viel Rechenzeit.
            # Stops und Ziele prueft der Bot trotzdem in jeder Runde mit dem aktuellen Kurs.
            import pandas as pd

            closed_ts = int(raw["timestamp"].iloc[-1])
            refresh = max(5.0, timeframe_seconds(self.cfg.timeframe) / 10)
            hit = self._live_prepared.get(symbol)
            if hit is not None and hit[0] == closed_ts and time.time() - hit[2] < refresh:
                df, last_ts = hit[1], hit[3]
            else:
                live = self._forming_candle(symbol, closed_ts)
                if live is not None:
                    raw = pd.concat([raw, live], ignore_index=True)
                df = self.strategy.prepare(raw)
                last_ts = int(raw["timestamp"].iloc[-1])
                self._live_prepared[symbol] = (closed_ts, df, time.time(), last_ts)
        else:
            last_ts = int(raw["timestamp"].iloc[-1])
            # Signale haengen nur an abgeschlossenen Kerzen: innerhalb einer Kerze nicht neu rechnen
            hit = self._prepared.get(symbol)
            if hit is not None and hit[0] == last_ts:
                df = hit[1]
            else:
                df = self.strategy.prepare(raw)
                self._prepared[symbol] = (last_ts, df)
        is_new_candle = self.state.last_candle_ts.get(symbol) != last_ts

        pos = self.state.positions.get(symbol)
        if pos is not None:
            # Echtzeit: Ausstiegssignale bei jeder Runde pruefen, nicht nur bei neuer Kerze
            self._manage_position(symbol, pos, price, df, is_new_candle or realtime)
            if realtime and symbol not in self.state.positions:
                # Ausstieg: in derselben Kerze nur noch in die Gegenrichtung (Umkehr), sofort in dieser Runde
                self._acted_candle[symbol] = (last_ts, pos.side)
                self._maybe_enter(symbol, price, df, block_side=pos.side)
                if symbol in self.state.positions:
                    self._acted_candle[symbol] = (last_ts, "any")
        else:
            acted = self._acted_candle.get(symbol)
            same = realtime and acted is not None and acted[0] == last_ts
            if is_new_candle or (realtime and not same) or (same and acted[1] != "any"):
                # Echtzeit: hoechstens ein Einstieg je Paar und Kerze; nach einem Ausstieg nur die Gegenrichtung
                self._maybe_enter(symbol, price, df, block_side=acted[1] if same and not is_new_candle else None)
                if realtime and symbol in self.state.positions:
                    self._acted_candle[symbol] = (last_ts, "any")

        self.state.last_candle_ts[symbol] = last_ts
        self._build_view(symbol, price, df)

    VIEW_CANDLES = 600  # bei 1m-Kerzen 10 Stunden, das Dashboard zeigt davon 1, 3 oder 10 Stunden

    def _build_view(self, symbol: str, price: float, df) -> None:
        """Haelt fest, was der Bot sieht. Fehler hier duerfen den Handel nie stoeren."""
        try:
            tail = df.tail(self.VIEW_CANDLES)

            def clean(values) -> list:
                return [None if v is None or v != v else round(float(v), 8) for v in values]

            candles = [[int(t), *clean(row)] for t, row in zip(tail["timestamp"], tail[["open", "high", "low", "close"]].to_numpy())]
            # Beim Ensemble sind anfangs nur Tages-VWAP und Trend-EMA sichtbar, der Rest laesst sich einblenden
            many = len(self.strategy.chart_lines) > 4
            shown = self.strategy.chart_visible
            lines = []
            for col, label, style in self.strategy.chart_lines:
                if col not in tail.columns:
                    continue
                visible = col in shown if shown is not None else (not many) or col in ("vwap", "ema_trend")
                line = {"key": col, "label": label, "style": style, "visible": visible, "values": clean(tail[col].tolist())}
                dcol = self.strategy.line_direction.get(col)
                if dcol and dcol in tail.columns:
                    line["directions"] = [None if v != v else ("long" if v >= 0.5 else "short") for v in tail[dcol].tolist()]
                lines.append(line)
            markers = []
            for col, side, text_col in self.strategy.chart_markers:
                if col not in tail.columns:
                    continue
                for t, flag, txt in zip(tail["timestamp"], tail[col], tail[text_col] if text_col else [None] * len(tail)):
                    if bool(flag):
                        markers.append({"time": int(t), "side": side, "text": "" if txt is None else str(int(txt))})
            votes = self.strategy.member_votes(df) if hasattr(self.strategy, "member_votes") else None
            pos = self.state.positions.get(symbol)
            session = None
            if self.session is not None:
                dt = self.cfg.daytrading
                session = {
                    "window": f"{dt.entry_start}-{dt.entry_end}",
                    "flat": dt.flat_time,
                    "entry_open": self.session.entry_allowed(self._now()),
                    "entries_today": self.state.entries_today,
                    "max_entries": dt.max_trades_per_day,
                }
            self.views[symbol] = {
                "symbol": symbol,
                "price": price,
                "timeframe": self.cfg.timeframe,
                "strategy": self.strategy.name,
                "candles": candles,
                "lines": lines,
                "explain": self.strategy.explain(df),
                "status": self._status.get(symbol, "Wartet auf die naechste abgeschlossene Kerze"),
                "position": (
                    {"side": pos.side, "entry": pos.entry_price, "stop": pos.stop_loss, "take_profit": pos.take_profit, "opened_at": pos.opened_at,
                     "strategy": pos.strategy}
                    if pos is not None
                    else None
                ),
                "session": session,
                "votes": votes,
                "markers": markers,
                "min_votes": getattr(self.strategy, "min_votes", None) if votes is not None else None,
                "updated": utcnow_iso(),
            }
        except Exception:  # noqa: BLE001
            log.exception("Marktansicht fuer %s fehlgeschlagen", symbol)

    # --------------------------------------------------- Stop-Loss bei der Boerse
    def _exchange_stops_enabled(self) -> bool:
        return bool(self.cfg.execution.stoploss_on_exchange and self.broker.supports_exchange_stop())

    def _arm_exchange_stop(self, symbol: str, pos: Position) -> None:
        if not self._exchange_stops_enabled() or pos.is_short:
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
        self._status[symbol] = (
            f"{'Short' if pos.is_short else 'Long'} offen: Einstieg {pos.entry_price:.4f}, Stop {pos.stop_loss:.4f}"
            + (f", Take-Profit {pos.take_profit:.4f}" if pos.take_profit is not None else "")
        )
        if self.session is not None:
            reason = self.session.must_close(self._now(), parse_iso(pos.opened_at))
            if reason:
                log.info("%s: Tageshandel, Position wird geschlossen (%s)", symbol, reason)
                self._close_position(symbol, pos, reason)
                return
        # Strategie mit eigener Trailing-Linie (z. B. SuperTrend): Stop an der Linie der letzten Kerze nachziehen
        line_col = self.strategy.trail_line_for(pos.strategy)
        if line_col and self.cfg.strategy.trailing_stop and line_col in df.columns:
            v = float(df[line_col].iloc[-1])
            if v == v:
                if pos.is_short and v > price and v < pos.stop_loss:
                    log.info("%s: Stop an der Linie (Short) %.4f -> %.4f", symbol, pos.stop_loss, v)
                    pos.set_stop(v, "line")
                elif not pos.is_short and v < price and v > pos.stop_loss:
                    log.info("%s: Stop an der Linie %.4f -> %.4f", symbol, pos.stop_loss, v)
                    pos.set_stop(v, "line")
                    if self._raise_exchange_stop(symbol, pos):
                        return
        # bester Kurs seit Einstieg: bei Long das Hoch, bei Short das Tief
        elif (price < pos.highest_price) if pos.is_short else (price > pos.highest_price):
            pos.highest_price = price
            if self.cfg.strategy.trailing_stop:
                atr_now = float(df["atr"].iloc[-1])
                if not math.isnan(atr_now) and atr_now > 0:
                    if pos.is_short:
                        new_stop = self.strategy.trailing_stop_short(pos.highest_price, atr_now)
                        if new_stop < pos.stop_loss:
                            log.info("%s: Trailing-Stop (Short) %.4f -> %.4f", symbol, pos.stop_loss, new_stop)
                            pos.set_stop(new_stop, "trailing")
                    else:
                        new_stop = self.strategy.trailing_stop(pos.highest_price, atr_now)
                        if new_stop > pos.stop_loss:
                            log.info("%s: Trailing-Stop %.4f -> %.4f", symbol, pos.stop_loss, new_stop)
                            pos.set_stop(new_stop, "trailing")
                            if self._raise_exchange_stop(symbol, pos):
                                return

        if pos.stop_hit(price):
            self._close_position(symbol, pos, "stop_loss")
            return
        if pos.take_profit_hit(price):
            self._close_position(symbol, pos, "take_profit")
            return
        if is_new_candle:
            decision = self.strategy.decide(df, context=self._context(symbol, price, pos))
            exit_signal = Signal.COVER if pos.is_short else Signal.SELL
            if decision.signal == exit_signal:
                self._close_position(symbol, pos, f"signal: {decision.reason}")
            elif decision.adjust:
                self._adjust_levels(symbol, pos, price, decision)

    def _adjust_levels(self, symbol: str, pos: Position, price: float, decision) -> None:
        """Neuer Stop/neues Ziel von der Strategie (z. B. KI): Stop nur naeher an den Kurs, beides auf der richtigen Seite."""
        sign = -1 if pos.is_short else 1
        new_stop = decision.stop_loss
        if new_stop and sign * (price - new_stop) > 0 and sign * (new_stop - pos.stop_loss) > 0:
            log.info("%s: Stop von der Strategie %.4f -> %.4f", symbol, pos.stop_loss, new_stop)
            pos.set_stop(new_stop, "strategy")
            if not pos.is_short:
                self._raise_exchange_stop(symbol, pos)
        new_tp = decision.take_profit
        if new_tp and sign * (new_tp - price) > 0 and new_tp != pos.take_profit:
            log.info("%s: Ziel von der Strategie %s -> %.4f", symbol, pos.take_profit, new_tp)
            pos.take_profit = new_tp
        self._save()

    def _maybe_enter(self, symbol: str, price: float, df, block_side: str | None = None) -> None:
        if self.state.entries_paused:
            log.info("%s: Einstiege pausiert", symbol)
            self._status[symbol] = "Kein Einstieg: Einstiege sind pausiert"
            return
        if self.state.entries_halted_for_day:
            log.info("%s: Tagesverlustlimit erreicht, kein Einstieg", symbol)
            self._status[symbol] = "Kein Einstieg: Tagesverlustlimit erreicht"
            return
        if self.session is not None:
            dt = self.cfg.daytrading
            if not self.session.entry_allowed(self._now()):
                log.info("%s: kein Einstieg, ausserhalb der Einstiegszeit %s-%s", symbol, dt.entry_start, dt.entry_end)
                self._status[symbol] = f"Kein Einstieg: ausserhalb der Einstiegszeit {dt.entry_start}-{dt.entry_end}"
                return
            if dt.max_trades_per_day and self.state.entries_today >= dt.max_trades_per_day:
                log.info("%s: kein Einstieg, max. %d Einstiege pro Tag erreicht", symbol, dt.max_trades_per_day)
                self._status[symbol] = f"Kein Einstieg: {dt.max_trades_per_day} Einstiege heute erreicht"
                return
        allowed, why = self.protections.entry_allowed(symbol, int(time.time() * 1000))
        if not allowed:
            log.info("%s: kein Einstieg, %s", symbol, why)
            self._status[symbol] = f"Kein Einstieg: {why}"
            return
        if not self.risk.can_open_new(len(self.state.positions)):
            log.info("%s: kein Einstieg, max. offene Positionen erreicht", symbol)
            self._status[symbol] = "Kein Einstieg: maximale Zahl offener Positionen erreicht"
            return
        if self.entry_precheck is not None:
            try:
                allowed, why = self.entry_precheck(symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: Vorpruefung fehlgeschlagen (%s)", symbol, exc)
                allowed = True
            if not allowed:
                self._status[symbol] = f"Kein Einstieg: {why}"
                return
        decision = self.strategy.decide(df, context=self._context(symbol, price, None))
        is_short = decision.signal == Signal.SHORT
        if decision.signal != Signal.BUY and not (is_short and self.cfg.strategy.allow_short):
            log.debug("%s: %s (%s)", symbol, decision.signal.value, decision.reason)
            self._status[symbol] = "Wartet: keine Einstiegsbedingung erfuellt" if decision.reason == "kein Signal" else f"Wartet: {decision.reason}"
            return
        if decision.atr is None or decision.atr <= 0:
            return
        side = "short" if is_short else "long"
        if block_side in (side, "any"):
            self._status[symbol] = f"Wartet: in dieser Kerze schon {'Short' if side == 'short' else 'Long'} gehandelt"
            return
        sign = side_sign(side)
        paper = self.cfg.paper
        idx = len(df) - 1
        trigger = self.strategy.trigger_at(df, idx, side)
        if self.entry_guard is not None:
            try:
                allowed, why = self.entry_guard(symbol, side)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: Zusatzpruefung fehlgeschlagen (%s), Einstieg erlaubt", symbol, exc)
                allowed = True
            if not allowed:
                log.info("%s: %s-Signal verworfen, %s", symbol, side, why)
                self._status[symbol] = f"{side.capitalize()}-Signal verworfen: {why}"
                return
        ok, why = self._track_record(symbol, df)
        if not ok:
            log.info("%s: %s-Signal verworfen, %s", symbol, side, why)
            self._status[symbol] = f"{side.capitalize()}-Signal verworfen: {why}"
            return
        plan_stop, plan_tp = self.strategy.entry_plan(df, idx, side, price, decision.atr)
        worth_it, target_pct, cost_pct = self.risk.target_check(
            price, abs(plan_tp - price) if plan_tp is not None else abs(price - plan_stop), paper.fee_pct, paper.slippage_pct
        )
        if not worth_it and self.strategy.cost_check(trigger):
            ratio = self.cfg.risk.min_target_cost_ratio
            log.info("%s: %s-Signal verworfen, Ziel %.3f %% unter %.1f x Kosten (%.3f %%)", symbol, side, target_pct, ratio, cost_pct)
            self._status[symbol] = (
                f"{side.capitalize()}-Signal verworfen: Ziel {target_pct:.2f} % lohnt nicht, "
                f"noetig sind {ratio:g} x Kosten = {ratio * cost_pct:.2f} %"
            )
            return

        equity = self.equity()
        free_quote = self.broker.balance_quote()
        stop, _ = cap_stop(price, plan_stop, side, self.cfg.leverage)
        amount = self.risk.position_size(
            equity, free_quote, price, stop, self.broker.limits(symbol), side=side, leverage=self.cfg.leverage
        )
        if amount <= 0:
            log.info("%s: %s-Signal, aber Positionsgroesse unter Mindestorder (Kapital %.2f)", symbol, side, equity)
            self._status[symbol] = f"{side.capitalize()}-Signal, aber die Positionsgroesse liegt unter der Mindestorder"
            return

        fill = self.broker.open_position(symbol, side, amount)
        fill_stop, take_profit = self.strategy.entry_plan(df, idx, side, fill.price, decision.atr)
        entry_stop, moved = cap_stop(fill.price, fill_stop, side, self.cfg.leverage)
        if moved:
            log.info("%s: Stop vor die Liquidation gelegt (Hebel %gx): %.4f", symbol, self.cfg.leverage, entry_stop)
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
            side=side,
            leverage=self.cfg.leverage,
            strategy=trigger,
            initial_stop=entry_stop,
        )
        self.state.positions[symbol] = pos
        self.state.entries_today += 1
        self._arm_exchange_stop(symbol, pos)
        self._save()
        tp_text = f"{take_profit:.4f}" if take_profit is not None else "aus"
        msg = (
            f"{'SHORT' if is_short else 'KAUF'} {symbol}: {fill.amount:.6f} @ {fill.price:.4f} "
            f"(Groesse {fill.amount * fill.price:.2f} {self.cfg.quote_currency}, "
            f"Risiko bis Stop inkl. Kosten {self._risk_with_costs(fill.price, entry_stop, fill.amount, fill.fee):.2f} {self.cfg.quote_currency}) "
            f"(Stop {entry_stop:.4f}, TP {tp_text}, {decision.reason})"
        )
        self.notifier.send(msg)
        self._status[symbol] = f"{'Short' if is_short else 'Long'} eroeffnet: {decision.reason}"

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
            fill = self.broker.close_position(symbol, pos.side, pos.amount)
        fees = fill.fee + pos.entry_fee
        pnl = side_sign(pos.side) * (fill.price - pos.entry_price) * pos.amount - fees
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
            side=pos.side,
            strategy=pos.strategy,
            leverage=pos.leverage,
            initial_stop=pos.initial_stop,
            stop_kind=pos.stop_kind if reason.startswith("stop_loss") else "",
        )
        self.state.trades.append(trade)
        self.protections.on_exit(symbol, reason, int(time.time() * 1000))
        del self.state.positions[symbol]
        self._status[symbol] = f"{'Short' if pos.is_short else 'Long'} geschlossen: {reason}"
        self._save()
        self.notifier.send(
            f"{'SHORT ZU' if pos.is_short else 'VERKAUF'} {symbol}: {pos.amount:.6f} @ {fill.price:.4f} "
            f"(Groesse {pos.amount * pos.entry_price:.2f} {self.cfg.quote_currency}) -> PnL {pnl:+.2f} {self.cfg.quote_currency} ({pnl_pct:+.2f}%), Grund: {reason}"
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
