"""Anbindung an Claude fuer den KI-Bot. Claude entscheidet nur ueber Einstieg und Ausstieg.
Positionsgroesse, Stop-Loss, Risikogrenzen und Schutzmechanismen bleiben fest im Bot."""
from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic
import pandas as pd

from .config import AiConfig

log = logging.getLogger(__name__)

ACTIONS = ("buy", "sell", "hold")

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["action", "confidence", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Du bist der Entscheidungsteil eines automatischen Krypto-Handelsbots fuer den Spot-Handel.
Pro neuer abgeschlossener Kerze bekommst du die Marktdaten eines Handelspaars und den Zustand der Position. Du entscheidest genau eine Aktion.

Regeln des Systems, die du nicht aendern kannst:
- Der Bot handelt nur long und ohne Hebel. Moegliche Aktionen: buy, sell, hold.
- buy ist nur sinnvoll, wenn keine Position offen ist. sell ist nur sinnvoll, wenn eine Position offen ist. Sonst waehle hold.
- Positionsgroesse, Stop-Loss und Take-Profit berechnet der Bot selbst. Du entscheidest nur, ob jetzt ein- oder ausgestiegen wird.
- Jeder Handel kostet Gebuehren und Kursabweichung, zusammen etwa 0,3 Prozent. Steige nur ein, wenn die Lage klar guenstig ist. Halten ist der Normalfall.
- Ein offener Trade hat einen Stop-Loss im Bot. Verkaufe aktiv nur, wenn die Lage sich erkennbar verschlechtert hat oder der Trend dreht.
- confidence ist deine ehrliche Wahrscheinlichkeit zwischen 0 und 1, dass die Entscheidung sich lohnt. Der Bot ignoriert Entscheidungen unter seiner Schwelle.
- Nutze ausschliesslich die gelieferten Zahlen. Du kennst keine Nachrichten und keine Zukunft. Schreibe nichts, was die Daten nicht hergeben.
- reason: ein bis zwei Saetze auf Deutsch, hoechstens 300 Zeichen, mit den wichtigsten Zahlen."""


@dataclass
class AiDecision:
    action: str
    confidence: float
    reason: str
    ok: bool = True


def _fmt(x: float) -> str:
    return f"{x:.6g}"


def build_user_prompt(symbol: str, timeframe: str, quote: str, df: pd.DataFrame, context: dict, candles: int) -> str:
    last = df.iloc[-1]
    close = float(last["close"])
    tail = df.tail(candles)
    rows = []
    for _, r in tail.iterrows():
        t = datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        rows.append(f"{t},{_fmt(r['open'])},{_fmt(r['high'])},{_fmt(r['low'])},{_fmt(r['close'])},{_fmt(r['volume'])}")

    def change(n: int) -> str:
        if len(df) <= n:
            return "n/a"
        return f"{(close / float(df['close'].iloc[-1 - n]) - 1) * 100:+.2f}%"

    n_week = min(len(df), 168)
    week = df.tail(n_week)
    stats = [
        f"Kurs: {_fmt(close)} {quote}",
        f"Aenderung 1/6/24/168 Kerzen: {change(1)} / {change(6)} / {change(24)} / {change(168)}",
        f"Hoch/Tief der letzten {n_week} Kerzen: {_fmt(float(week['high'].max()))} / {_fmt(float(week['low'].min()))}",
    ]
    for col, label in (("ema_fast", "EMA schnell"), ("ema_slow", "EMA langsam"), ("rsi", "RSI"), ("atr", "ATR")):
        if col in df.columns and pd.notna(last[col]):
            extra = f" ({float(last[col]) / close * 100:.2f}% vom Kurs)" if col == "atr" else ""
            stats.append(f"{label}: {_fmt(float(last[col]))}{extra}")
    if "volume" in df.columns and len(df) >= 24:
        avg = float(df["volume"].tail(24).mean())
        if avg > 0:
            stats.append(f"Volumen letzte Kerze im Vergleich zum Schnitt der letzten 24: {float(last['volume']) / avg:.2f}x")

    pos = context.get("position")
    if pos:
        position_text = (
            f"LONG offen: Menge {_fmt(pos['amount'])}, Einstieg {_fmt(pos['entry_price'])}, aktueller Gewinn/Verlust {pos['pnl_pct']:+.2f}%, "
            f"Stop-Loss {_fmt(pos['stop_loss'])}" + (f", Take-Profit {_fmt(pos['take_profit'])}" if pos.get("take_profit") else "")
        )
    else:
        position_text = "Keine Position offen."

    account = []
    if context.get("equity") is not None:
        account.append(f"Kapital {context['equity']:.2f} {quote}")
    if context.get("free_quote") is not None:
        account.append(f"frei {context['free_quote']:.2f} {quote}")
    account.append(f"offene Positionen insgesamt {context.get('open_positions', 0)}")

    return (
        f"Handelspaar: {symbol}, Zeitrahmen {timeframe}\n"
        f"Position: {position_text}\n"
        f"Konto: {', '.join(account)}\n\n"
        "Kennzahlen:\n" + "\n".join(f"- {s}" for s in stats) + "\n\n"
        f"Letzte {len(rows)} Kerzen (UTC, open,high,low,close,volume), die letzte ist die neueste:\n" + "\n".join(rows) + "\n\n"
        "Entscheide jetzt."
    )


class ClaudeAdvisor:
    def __init__(self, cfg: AiConfig, api_key: str = "", client=None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic(api_key=api_key or None, timeout=cfg.timeout_seconds, max_retries=2)
        self.day: str | None = None
        self.calls = 0
        self.decisions: deque = deque(maxlen=60)

    # ------------------------------------------------------------ Zustand
    def export_state(self) -> dict:
        return {"day": self.day, "calls": self.calls, "decisions": list(self.decisions)}

    def import_state(self, state: dict) -> None:
        self.day = state.get("day")
        self.calls = int(state.get("calls", 0))
        self.decisions = deque(state.get("decisions", []), maxlen=60)

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.calls = today, 0

    def record(self, symbol: str, d: AiDecision, applied: bool, note: str = "") -> None:
        self.decisions.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbol": symbol,
                "action": d.action,
                "confidence": round(d.confidence, 3),
                "applied": applied,
                "reason": (d.reason + (f" [{note}]" if note else ""))[:400],
            }
        )

    # ----------------------------------------------------------- Abfrage
    def ask(self, user_prompt: str) -> AiDecision:
        self._roll_day()
        if self.calls >= self.cfg.max_calls_per_day:
            return AiDecision("hold", 0.0, f"Tageslimit von {self.cfg.max_calls_per_day} KI-Aufrufen erreicht", ok=False)
        self.calls += 1
        try:
            response = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            )
        except anthropic.RateLimitError:
            return AiDecision("hold", 0.0, "KI: Anfragelimit erreicht", ok=False)
        except anthropic.APITimeoutError:
            return AiDecision("hold", 0.0, "KI: Zeitueberschreitung", ok=False)
        except anthropic.APIConnectionError:
            return AiDecision("hold", 0.0, "KI nicht erreichbar", ok=False)
        except anthropic.APIStatusError as exc:
            log.error("Anthropic-API-Fehler %s: %s", exc.status_code, exc.message)
            return AiDecision("hold", 0.0, f"KI-Fehler {exc.status_code}", ok=False)
        return self.parse(response)

    @staticmethod
    def parse(response) -> AiDecision:
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            return AiDecision("hold", 0.0, "KI hat die Anfrage abgelehnt", ok=False)
        if stop == "max_tokens":
            return AiDecision("hold", 0.0, "KI-Antwort war abgeschnitten", ok=False)
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if not text:
            return AiDecision("hold", 0.0, "KI lieferte keine Antwort", ok=False)
        try:
            data = json.loads(text)
            action = str(data["action"]).lower()
            confidence = float(data["confidence"])
            reason = str(data["reason"]).strip()
        except (ValueError, KeyError, TypeError):
            return AiDecision("hold", 0.0, "KI-Antwort nicht lesbar", ok=False)
        if action not in ACTIONS or confidence != confidence:
            return AiDecision("hold", 0.0, "KI-Antwort ungueltig", ok=False)
        return AiDecision(action, min(max(confidence, 0.0), 1.0), reason[:300])
