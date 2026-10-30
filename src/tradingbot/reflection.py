"""Fehleranalyse der KI: Fakten aus den abgeschlossenen Trades, Lehren der KI daraus und ein Schutz ueber alle Konten.

Nach jeweils `ai.reflect_every` neuen Trades bekommt die KI ihre letzten Trades samt Einstiegsgruenden und harten
Kennzahlen und schreibt daraus wenige, pruefbare Regeln. Diese Lehren stehen danach in jeder Entscheidungsanfrage.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import remote_state
from .models import Trade
from .session import parse_iso

log = logging.getLogger(__name__)

REFLECT_PROMPT = """Du bist der Lernteil eines automatischen Krypto-Handelsbots (USDT-Perpetual-Futures, Papierhandel).
Du bekommst die letzten abgeschlossenen Trades des Bots mit dem Einstiegsgrund, den die KI damals genannt hat,
und harte Kennzahlen dazu. Arbeite die Fehler auf:
- Finde die Muster hinter den Verlusten (Richtung gegen den Markt, zu enger Stop, viele gleichartige Einstiege
  auf einmal, Wiedereinstieg nach einem Stop, zu kleines Ziel fuer die Kosten, falscher Zeitrahmen ...).
- Stuetze dich auf die Zahlen, nicht auf Vermutungen. Was gut lief, darf bleiben.
- Schreibe daraus hoechstens {n} Regeln. Jede Regel ist konkret und pruefbar: Bedingung -> Handlung,
  z. B. "Kein Long, wenn der 1h-Trend abwaerts zeigt, auch nicht nach einem Sweep".
- Keine allgemeinen Ratschlaege wie "sei vorsichtig".

Antworte nur mit JSON: {{"summary": "<2-3 Saetze: was lief falsch, was lief gut>", "lessons": ["<Regel>", ...]}}"""


def _mins(a: str, b: str) -> float:
    return (parse_iso(b) - parse_iso(a)).total_seconds() / 60.0


def _line(label: str, trades: list[Trade]) -> str:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct > 0)
    avg = sum(t.pnl_pct for t in trades) / n if n else 0.0
    return f"{label}: {n} Trades, {wins} Gewinner ({wins / n * 100:.0f} %), im Schnitt {avg:+.2f} %" if n else f"{label}: keine Trades"


def trade_facts(trades: list[tuple[Trade, str]]) -> list[str]:
    """Harte Kennzahlen zu (Trade, Zeitrahmen), aelteste zuerst: Richtung, Zeitrahmen, Stops, Buendel, Wiedereinstiege."""
    if not trades:
        return []
    ts = [t for t, _ in trades]
    out = [_line("Gesamt", ts)]
    for side in ("long", "short"):
        part = [t for t in ts if t.side == side]
        if part:
            out.append(_line(side.capitalize(), part))
    by_tf: dict[str, list[Trade]] = defaultdict(list)
    for t, tf in trades:
        by_tf[tf or "?"].append(t)
    out += [_line(f"Zeitrahmen {tf}", part) for tf, part in by_tf.items()]
    wins = [t.pnl_pct for t in ts if t.pnl_pct > 0]
    losses = [t.pnl_pct for t in ts if t.pnl_pct <= 0]
    stops = sum(1 for t in ts if t.reason.startswith("stop_loss"))
    out.append(f"Ausstieg im Stop: {stops} von {len(ts)}; Durchschnitt Gewinner {sum(wins) / len(wins) if wins else 0:+.2f} %, "
               f"Verlierer {sum(losses) / len(losses) if losses else 0:+.2f} %")
    hold_w = [_mins(t.opened_at, t.closed_at) for t in ts if t.pnl_pct > 0]
    hold_l = [_mins(t.opened_at, t.closed_at) for t in ts if t.pnl_pct <= 0]
    if hold_w and hold_l:
        out.append(f"Haltedauer: Gewinner im Schnitt {sum(hold_w) / len(hold_w):.0f} min, Verlierer {sum(hold_l) / len(hold_l):.0f} min")

    # Buendel: mindestens 3 Einstiege in dieselbe Richtung innerhalb von 15 Minuten (ueber alle Paare und Zeitrahmen)
    entries = sorted(ts, key=lambda t: t.opened_at)
    bundles, i = [], 0
    while i < len(entries):
        group = [t for t in entries[i:] if t.side == entries[i].side and 0 <= _mins(entries[i].opened_at, t.opened_at) <= 15]
        if len(group) >= 3:
            bundles.append(group)
            i = entries.index(group[-1]) + 1
        else:
            i += 1
    if bundles:
        n = sum(len(g) for g in bundles)
        res = sum(t.pnl_pct for g in bundles for t in g)
        lost = sum(1 for g in bundles for t in g if t.pnl_pct <= 0)
        out.append(f"Buendel (>= 3 Einstiege gleicher Richtung in 15 min): {len(bundles)}x mit zusammen {n} Trades, "
                   f"{lost} davon Verlierer, Summe {res:+.2f} % - das ist im Grunde eine einzige Wette mit {n}-fachem Einsatz")

    # Wiedereinstieg: gleiches Paar, gleiche Richtung innerhalb von 60 min nach einem Stop
    again = []
    for t in ts:
        for prev in ts:
            if prev is t or prev.symbol != t.symbol or prev.side != t.side or not prev.reason.startswith("stop_loss"):
                continue
            if prev.pnl_pct <= 0 and 0 <= _mins(prev.closed_at, t.opened_at) <= 60:
                again.append(t)
                break
    if again:
        out.append(_line("Wiedereinstieg in gleiche Richtung <= 60 min nach einem Stop im selben Paar", again))

    # dasselbe Paar gleichzeitig in mehreren Zeitrahmen
    dup = [t for (t, tf) in trades if any(o is not t and o.symbol == t.symbol and otf != tf and o.side == t.side
                                          and o.opened_at <= t.opened_at < o.closed_at for o, otf in trades)]
    if dup:
        out.append(_line("Einstieg, obwohl dasselbe Paar in einem anderen Zeitrahmen schon offen war", dup))
    return out


def entry_reason(trade: Trade, decisions: list[dict]) -> str:
    """Begruendung der KI zum Einstieg: die angewandte Entscheidung im selben Paar kurz vor der Eroeffnung."""
    want = "short" if trade.side == "short" else "buy"
    best, best_gap = "", None
    for d in decisions:
        if d.get("symbol") != trade.symbol or d.get("action") != want or not d.get("applied"):
            continue
        try:
            gap = _mins(d["ts"], trade.opened_at)
        except (KeyError, ValueError):
            continue
        if -1 <= gap <= 5 and (best_gap is None or abs(gap) < best_gap):
            best, best_gap = d.get("reason", ""), abs(gap)
    return best


def describe(trade: Trade, tf: str, decisions: list[dict]) -> str:
    stop = ""
    if trade.initial_stop:
        stop = f", Stop-Abstand {abs(trade.entry_price / trade.initial_stop - 1) * 100:.2f} %"
    why = entry_reason(trade, decisions)
    return (f"{tf} {trade.symbol.split('/')[0]} {trade.side} {trade.opened_at[5:16]} -> {trade.closed_at[11:16]}: "
            f"{trade.pnl_pct:+.2f} % nach Kosten, Ausstieg {trade.reason[:40]}{stop}"
            + (f". Einstiegsgrund: {why[:150]}" if why else ""))


def _find_json(text: str) -> dict | None:
    for m in re.finditer(r"\{", text):
        depth = 0
        for j in range(m.start(), len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                try:
                    d = json.loads(text[m.start():j + 1])
                except ValueError:
                    break
                if isinstance(d, dict) and isinstance(d.get("lessons"), list):
                    return d
                break
    return None


class Reflector:
    """Laesst die KI nach je `every` neuen Trades ihre Fehler aufarbeiten; die Lehren gelten fuer alle Zeitrahmen."""

    def __init__(self, ask: Callable[[str, str], str], trades: Callable[[], list[tuple[Trade, str]]],
                 decisions: Callable[[], list[dict]], path: Path, every: int = 5, window: int = 40, max_lessons: int = 6):
        self.ask, self.trades, self.decisions = ask, trades, decisions
        self.path = Path(path)
        self.every, self.window, self.max_lessons = every, window, max_lessons
        self.data: dict = {"lessons": [], "summary": "", "at": None, "analyzed": 0, "facts": []}
        self._busy = threading.Lock()
        self._retry_at = 0.0  # nach einem Fehlschlag erst spaeter wieder fragen
        self._load()

    def _load(self) -> None:
        remote_state.pull_if_missing(self.path)
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        text = json.dumps(self.data, indent=2, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        remote_state.push(self.path, text)

    @property
    def lessons(self) -> list[str]:
        return list(self.data.get("lessons") or [])

    def due(self) -> bool:
        if self.every <= 0 or time.time() < self._retry_at:
            return False
        return len(self.trades()) - int(self.data.get("analyzed", 0)) >= self.every

    def maybe_start(self) -> bool:
        """Startet die Analyse im Hintergrund, wenn genug neue Trades da sind (blockiert den Handel nicht)."""
        if not self.due() or self._busy.locked():
            return False
        threading.Thread(target=self.run, name="ki-fehleranalyse", daemon=True).start()
        return True

    def build_prompt(self) -> tuple[str, list[str]]:
        trades = self.trades()[-self.window:]
        decisions = self.decisions()
        facts = trade_facts(trades)
        text = ("Kennzahlen:\n" + "\n".join(f"- {f}" for f in facts) + "\n\n"
                "Trades (aelteste zuerst):\n" + "\n".join(f"- {describe(t, tf, decisions)}" for t, tf in trades) + "\n\n")
        if self.lessons:
            text += "Deine bisherigen Regeln (behalte, was die Zahlen stuetzen, ersetze den Rest):\n" + "\n".join(
                f"- {x}" for x in self.lessons) + "\n\n"
        return text + "Arbeite die Fehler auf und antworte mit JSON.", facts

    def run(self) -> bool:
        if not self._busy.acquire(blocking=False):
            return False
        try:
            total = len(self.trades())
            if not total:
                return False
            prompt, facts = self.build_prompt()
            try:
                answer = self.ask(REFLECT_PROMPT.format(n=self.max_lessons), prompt)
            except Exception as exc:  # noqa: BLE001
                return self._failed(f"KI nicht erreichbar ({str(exc)[:120]})")
            d = _find_json(answer or "")
            if d is None:
                return self._failed(f"Antwort ohne Regeln: {(answer or 'leer')[:160]}")
            lessons = [str(x).strip()[:300] for x in d["lessons"] if str(x).strip()][: self.max_lessons]
            self.data.update(lessons=lessons, summary=str(d.get("summary", ""))[:600], facts=facts,
                             at=datetime.now(timezone.utc).isoformat(timespec="seconds"), analyzed=total,
                             trades_seen=min(total, self.window), error="")
            self._save()
            log.info("KI-Fehleranalyse: %d Regeln aus %d Trades", len(lessons), min(total, self.window))
            return True
        finally:
            self._busy.release()

    RETRY_SECONDS = 900

    def _failed(self, why: str) -> bool:
        log.warning("KI-Fehleranalyse: %s", why)
        self.data["error"] = why
        self._retry_at = time.time() + self.RETRY_SECONDS  # nicht in jeder Runde neu fragen
        return False

    def snapshot(self) -> dict:
        return {k: self.data.get(k) for k in ("lessons", "summary", "at", "facts", "trades_seen", "error")} | {
            "next_in": max(0, self.every - (len(self.trades()) - int(self.data.get("analyzed", 0)))),
        }


class PortfolioGuard:
    """Schutz ueber alle Zeitrahmen-Konten: ein Paar nur einmal offen, begrenzte Wetten in dieselbe Richtung
    und kein Wiedereinstieg in dieselbe Richtung kurz nach einem Stop."""

    def __init__(self, bots: Callable[[], list], max_same_direction: int = 3, reentry_minutes: int = 60,
                 trades: Callable[[], list] | None = None, perf_window: int = 0, perf_min_trades: int = 8,
                 perf_probe_hours: float = 12.0):
        self.bots = bots
        self.max_same = max_same_direction
        self.reentry = reentry_minutes
        self.trades = trades  # () -> [(Trade, Zeitrahmen)] aller Konten, fuer die Leistungsbremse
        self.perf_window, self.perf_min, self.probe_h = perf_window, perf_min_trades, perf_probe_hours

    def performance(self, tf: str, side: str) -> tuple[bool, str]:
        """Leistungsbremse: (gesperrt, Grund) fuer Zeitrahmen + Richtung nach den letzten perf_window Trades."""
        if not self.perf_window or self.trades is None:
            return False, ""
        bucket = [t for t, ttf in self.trades() if ttf == tf and t.side == side][-self.perf_window:]
        if len(bucket) < self.perf_min:
            return False, ""
        avg = sum(t.pnl_pct for t in bucket) / len(bucket)
        if avg >= 0:
            return False, ""
        ago_h = (datetime.now(timezone.utc) - parse_iso(bucket[-1].closed_at)).total_seconds() / 3600
        if self.probe_h and ago_h >= self.probe_h:
            return False, ""  # lange kein Trade: ein Probe-Trade, damit sich die Bilanz erholen kann
        wins = sum(1 for t in bucket if t.pnl_pct > 0)
        name = "Shorts" if side == "short" else "Longs"
        wait = f", Probe-Trade in {self.probe_h - ago_h:.0f} h" if self.probe_h else ""
        return True, (f"{tf}-{name} gebremst: letzte {len(bucket)} im Schnitt {avg:+.2f} %, {wins} Gewinner{wait}")

    def blocked_buckets(self, timeframes: list[str]) -> list[str]:
        return [why for tf in timeframes for side in ("long", "short") for blocked, why in [self.performance(tf, side)] if blocked]

    def precheck(self, symbol: str, tf: str, short_allowed: bool = True) -> tuple[bool, str]:
        """Vor der KI-Abfrage: Paar schon offen oder beide Richtungen gebremst -> gar nicht erst fragen."""
        if any(symbol in b.state.positions for b in self.bots()):
            return False, f"{symbol.split('/')[0]} ist in einem anderen Zeitrahmen schon offen"
        sides = ("long", "short") if short_allowed else ("long",)
        reasons = [why for side in sides for blocked, why in [self.performance(tf, side)] if blocked]
        if len(reasons) == len(sides):
            return False, "; ".join(reasons)
        return True, ""

    def __call__(self, symbol: str, side: str, tf: str | None = None) -> tuple[bool, str]:
        positions = [p for b in self.bots() for p in b.state.positions.values()]
        if any(p.symbol == symbol for p in positions):
            return False, f"{symbol.split('/')[0]} ist in einem anderen Zeitrahmen schon offen"
        if tf is not None:
            blocked, why = self.performance(tf, side)
            if blocked:
                return False, why
        same = sum(1 for p in positions if p.side == side)
        if self.max_same and same >= self.max_same:
            return False, f"schon {same} {'Shorts' if side == 'short' else 'Longs'} offen (Grenze {self.max_same}, sonst eine Wette mehrfach)"
        if self.reentry:
            now = datetime.now(timezone.utc)
            for b in self.bots():
                for t in reversed(b.state.trades[-30:]):
                    if t.symbol == symbol and t.side == side and t.reason.startswith("stop_loss") and t.pnl_pct <= 0:
                        ago = (now - parse_iso(t.closed_at)).total_seconds() / 60
                        if ago < self.reentry:
                            return False, f"vor {ago:.0f} min im Stop, Wiedereinstieg erst nach {self.reentry} min"
        return True, ""


def chain_guards(*guards):
    """Mehrere Zusatzpruefungen nacheinander; die erste Ablehnung gilt."""
    active = [g for g in guards if g is not None]

    def guard(symbol: str, side: str) -> tuple[bool, str]:
        for g in active:
            ok, why = g(symbol, side)
            if not ok:
                return ok, why
        return True, ""
    return guard
