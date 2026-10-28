"""Mehrere Zeitrahmen gleichzeitig: je Zeitrahmen ein eigener Bot mit eigenem Papierkonto und eigenen Trades.

Der Bot im eingestellten Zeitrahmen (primary) behaelt die bisherigen Zustandsdateien, die anderen bekommen
Dateien mit Endung _<Zeitrahmen>. Fuer App und Befehle sieht MultiBot aus wie ein einzelner Bot: Kapital und
Kapitalverlauf sind die Summe, Positionen und Trades tragen ihren Zeitrahmen, der Chart nimmt den Bot des
gewaehlten Zeitrahmens (mit dessen Linien und Trades)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from bisect import bisect_right
from datetime import datetime
from typing import Callable

from .bot import TradingBot
from .config import Settings
from .data import timeframe_seconds
from .snapshot import MAX_TRADES, analyze, compute_stats, downsample

log = logging.getLogger(__name__)


def settings_for(settings: Settings, timeframe: str) -> Settings:
    """Einstellungen fuer einen Zeitrahmen (eigene Kopie, damit sich die Bots nichts teilen)."""
    bot = settings.bot.model_copy(deep=True, update={"timeframe": timeframe})
    return settings.model_copy(update={"bot": bot})


def ordered_timeframes(primary: str, extra: list[str]) -> list[str]:
    tfs = list(dict.fromkeys([primary, *extra]))
    return sorted(tfs, key=timeframe_seconds)


def _merge_history(histories: list[list]) -> list[list]:
    """Summe mehrerer Kapitalverlaeufe: zu jedem Zeitpunkt der letzte bekannte Wert jedes Kontos."""
    series = []
    for h in histories:
        pts = [(datetime.fromisoformat(t), float(v)) for t, v in h]
        if pts:
            series.append(pts)
    if not series:
        return []
    times = sorted({t for s in series for t, _ in s})
    out = []
    for t in times:
        total, ok = 0.0, True
        for s in series:
            k = bisect_right([x for x, _ in s], t) - 1
            total += s[k][1] if k >= 0 else s[0][1]
        if ok:
            out.append([t.isoformat(timespec="seconds"), round(total, 4)])
    return out


class MultiBot:
    """Mehrere TradingBots (einer je Zeitrahmen) mit der Schnittstelle eines einzelnen Bots."""

    def __init__(self, bots: dict[str, TradingBot], primary: str):
        self.bots = bots
        self.primary_tf = primary
        self.threads: list[threading.Thread] = []

    @classmethod
    def build(cls, settings: Settings, timeframes: list[str], builder: Callable[[Settings, str], TradingBot]) -> "MultiBot":
        primary = settings.bot.timeframe
        bots = {}
        for tf in ordered_timeframes(primary, timeframes):
            suffix = "" if tf == primary else f"_{tf}"
            bots[tf] = builder(settings_for(settings, tf), suffix)
        return cls(bots, primary)

    # ---------------------------------------------------------- Weiterreichen an den Haupt-Bot
    @property
    def primary(self) -> TradingBot:
        return self.bots[self.primary_tf]

    def __getattr__(self, name):
        # alles Uebrige (cfg, strategy, feed, state, exit_code, ...) vom Bot im eingestellten Zeitrahmen
        d = self.__dict__
        if "bots" not in d or "primary_tf" not in d:
            raise AttributeError(name)
        return getattr(d["bots"][d["primary_tf"]], name)

    @property
    def timeframes(self) -> list[str]:
        return list(self.bots)

    def all_bots(self) -> list[TradingBot]:
        return list(self.bots.values())

    def all_trades(self) -> list:
        """Abgeschlossene Trades aller Zeitrahmen als (Trade, Zeitrahmen), aelteste zuerst."""
        out = [(t, tf) for tf, b in self.bots.items() for t in b.state.trades]
        return sorted(out, key=lambda x: x[0].closed_at)

    def ai_decisions(self) -> list[dict]:
        out = []
        for b in self.all_bots():
            adv = getattr(b.strategy, "advisor", None)
            if adv is not None:
                out += list(adv.decisions)
        return out

    def enable_learning(self) -> None:
        """Nur fuer die KI-Strategie: gemeinsames Gedaechtnis, Fehleranalyse und Schutz ueber alle Konten."""
        from .reflection import PortfolioGuard, Reflector, chain_guards

        p = self.primary
        if not getattr(p.strategy, "live_only", False):
            return
        ai = p.cfg.ai
        guard = PortfolioGuard(self.all_bots, p.cfg.protections.max_same_direction, p.cfg.protections.reentry_block_minutes)
        self.reflector = Reflector(p.strategy.advisor.complete_text, self.all_trades, self.ai_decisions,
                                   Path(p.state_path).with_name("ai_lessons.json"), ai.reflect_every, ai.reflect_trades, ai.max_lessons)
        before = p.before_cycle

        def cycle():
            if before is not None:
                before()
            self.reflector.maybe_start()
        p.before_cycle = cycle
        for b in self.all_bots():
            b.trade_memory = self.all_trades
            b.lessons = lambda: self.reflector.lessons
            b.entry_guard = chain_guards(b.entry_guard, guard)

    def open_symbols(self) -> set[str]:
        return {s for b in self.all_bots() for s in b.state.positions}

    # ---------------------------------------------------------- Lauf
    def run_forever(self) -> None:
        for tf, b in self.bots.items():
            if tf == self.primary_tf:
                continue
            t = threading.Thread(target=self._run_one, args=(tf, b), name=f"bot-{tf}", daemon=True)
            t.start()
            self.threads.append(t)
        try:
            self.primary.run_forever()
        finally:
            for b in self.all_bots():
                b.stop()

    @staticmethod
    def _run_one(tf: str, b: TradingBot) -> None:
        try:
            b.run_forever()
        except Exception:  # noqa: BLE001
            log.exception("Bot %s abgestuerzt", tf)

    def stop(self) -> None:
        for b in self.all_bots():
            b.stop()

    def request_restart(self) -> None:
        for b in self.all_bots():
            if b is not self.primary:
                b.stop()
        self.primary.request_restart()

    # ---------------------------------------------------------- Befehle fuer alle Zeitrahmen
    def pause(self) -> dict:
        for b in self.all_bots():
            b.pause()
        return self.get_snapshot()

    def resume(self) -> dict:
        for b in self.all_bots():
            b.resume()
        return self.get_snapshot()

    def request_account_reset(self, clear_trades: bool) -> dict:
        out = {}
        for b in self.all_bots():
            out = b.request_account_reset(clear_trades)
        return {**out, "positions": sum(len(b.state.positions) for b in self.all_bots())}

    def request_fee_reprice(self) -> dict:
        out = {}
        for b in self.all_bots():
            out = b.request_fee_reprice()
        return {**out, "trades": sum(len(b.state.trades) for b in self.all_bots())}

    def delete_oldest_trades(self, count: int) -> dict:
        res = [b.delete_oldest_trades(count) for b in self.all_bots()]
        return {"deleted": sum(r["deleted"] for r in res), "remaining": sum(r["remaining"] for r in res)}

    def chart(self, symbol: str, timeframe: str | None = None, since: int | None = None) -> dict:
        b = self.bots.get(timeframe or self.primary_tf, self.primary)
        return b.chart(symbol, timeframe, since)

    # ---------------------------------------------------------- Snapshot (Summe aller Zeitrahmen)
    def get_snapshot(self) -> dict:
        snaps = {tf: b.get_snapshot() for tf, b in self.bots.items()}
        base = dict(snaps[self.primary_tf])

        def total(key):
            vals = [s.get(key) for s in snaps.values()]
            return sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None

        equity, start = total("equity"), None
        histories = [b.state.equity_history for b in self.all_bots()]
        merged = _merge_history(histories)
        if merged:
            start = merged[0][1]
        day_start = total("day_start_equity")
        trades = []
        for tf, b in self.bots.items():
            trades += [(t, tf) for t in b.state.trades]
        trades.sort(key=lambda x: x[0].closed_at)
        positions = []
        for tf, s in snaps.items():
            positions += [{**p, "timeframe": tf} for p in s.get("positions", [])]
        tz = self.primary.cfg.daytrading.timezone
        base.update(
            equity=equity,
            free_quote=total("free_quote"),
            start_equity=start,
            return_pct=(equity / start - 1.0) * 100.0 if equity and start else None,
            day_start_equity=day_start,
            day_pnl_pct=(equity / day_start - 1.0) * 100.0 if equity and day_start else None,
            paused=all(s.get("paused") for s in snaps.values()),
            halted=any(s.get("halted") for s in snaps.values()),
            positions=positions,
            trades=[{**t.to_dict(), "timeframe": tf} for t, tf in reversed(trades[-MAX_TRADES:])],
            stats=compute_stats([t for t, _ in trades]),
            analytics=analyze([t for t, _ in trades], merged, tz),
            equity_history=downsample(merged),
            market_by_timeframe={tf: s.get("market", []) for tf, s in snaps.items()},
            timeframes=self.timeframes,
            lessons=self.reflector.snapshot() if getattr(self, "reflector", None) else None,
            accounts=[{"timeframe": tf, "equity": s.get("equity"), "return_pct": s.get("return_pct"),
                       "positions": len(s.get("positions", [])), "trades": len(self.bots[tf].state.trades),
                       "last_cycle": s.get("last_cycle")} for tf, s in snaps.items()],
        )
        return base
