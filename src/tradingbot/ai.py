"""Anbindung an Claude fuer den KI-Bot. Claude entscheidet nur ueber Einstieg und Ausstieg.
Positionsgroesse, Stop-Loss, Risikogrenzen und Schutzmechanismen bleiben fest im Bot."""
from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic
import pandas as pd
import requests

from .config import AiConfig

log = logging.getLogger(__name__)

ACTIONS = ("buy", "sell", "short", "cover", "hold")

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
    },
    "required": ["action", "confidence", "reason", "stop_loss", "take_profit"],
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


FUTURES_PROMPT = """Du bist der Entscheidungsteil eines automatischen Krypto-Handelsbots fuer USDT-Perpetual-Futures.
Pro neuer abgeschlossener Kerze bekommst du die Marktdaten eines Handelspaars und den Zustand der Position. Du entscheidest genau eine Aktion.

Regeln des Systems, die du nicht aendern kannst:
- Moegliche Aktionen: buy (Long eroeffnen), short (Short eroeffnen), sell (Long schliessen), cover (Short schliessen), hold.
- buy und short nur ohne offene Position. sell nur bei offenem Long, cover nur bei offenem Short. Sonst hold.
- Du legst Stop-Loss und Take-Profit als absolute Kurse fest. Positionsgroesse und Hebel berechnet der Bot aus deinem Stop.
- Beim Einstieg (buy/short) sind stop_loss und take_profit Pflicht. Long: Stop unter dem Kurs, Ziel darueber; Short umgekehrt.
- Lege den Stop hinter die naechste Liquiditaetszone bzw. Struktur (Swing-Tief/-Hoch, grosse Orderbuch-Wand), nicht mitten hinein,
  damit ein kurzer Stop-Hunt dich nicht ausstoppt. Lege das Ziel kurz vor die naechste gegenueberliegende Liquiditaet.
- Gewinn zu Risiko mindestens 1,5. Stop mindestens 0,3 % und hoechstens 5 % vom Kurs entfernt.
- Bei offener Position darfst du mit action hold neue Werte setzen: den Stop nur naeher an den Kurs (nie weiter weg), das Ziel frei.
  Willst du nichts aendern, setze beide auf null.
- Jeder Handel kostet Gebuehren und Kursabweichung, zusammen etwa 0,2 Prozent. Steige nur bei einer klaren Lage ein. Halten ist der Normalfall.
- Handle bevorzugt mit dem Trend (z. B. Kurs ueber beiden EMAs und EMAs steigend: eher Long; darunter und fallend: eher Short). In Seitwaertsphasen: hold.
- Ein offener Trade hat einen Stop-Loss im Bot. Schliesse aktiv nur, wenn die Lage sich erkennbar gegen die Position gedreht hat.
- confidence ist deine ehrliche Wahrscheinlichkeit zwischen 0 und 1, dass die Entscheidung sich lohnt. Der Bot ignoriert Entscheidungen unter seiner Schwelle.
- Nutze ausschliesslich die gelieferten Zahlen. Du kennst keine Nachrichten und keine Zukunft.
- Liquiditaet: Stops sammeln sich ueber Swing-Hochs und unter Swing-Tiefs sowie an gleichen Hochs/Tiefs. Der Kurs holt sie oft
  ab (Sweep) und dreht dann. Ein Sweep mit Umkehr ist ein Einstiegsgrund in Gegenrichtung, ein Ausbruch mit Volumen in Ausbruchsrichtung.
  Orderbuch-Ungleichgewicht, Funding und Open Interest zeigen Druck und Positionierung (hohes positives Funding = viele Longs).
- Antworte nur mit einem JSON-Objekt: {"action": "...", "confidence": 0.0, "reason": "...", "stop_loss": Zahl oder null, "take_profit": Zahl oder null}.
  reason: ein bis zwei Saetze auf Deutsch, hoechstens 300 Zeichen, mit Stop- und Ziel-Begruendung."""


def _find_decision_json(text: str) -> dict:
    """Das letzte JSON-Objekt mit "action" im Text (auch in ```json-Bloecken oder nach Ueberlegungen)."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    dec = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            found = obj
    if found is None:
        raise ValueError("kein JSON mit action")
    return found


@dataclass
class AiDecision:
    action: str
    confidence: float
    reason: str
    ok: bool = True
    stop_loss: float | None = None
    take_profit: float | None = None


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
            f"{'SHORT' if pos.get('side') == 'short' else 'LONG'} offen: Menge {_fmt(pos['amount'])}, Einstieg {_fmt(pos['entry_price'])}, aktueller Gewinn/Verlust {pos['pnl_pct']:+.2f}%, "
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
    liq = context.get("liquidity") or []

    return (
        f"Handelspaar: {symbol}, Zeitrahmen {timeframe}\n"
        f"Position: {position_text}\n"
        f"Konto: {', '.join(account)}\n\n"
        "Kennzahlen:\n" + "\n".join(f"- {s}" for s in stats) + "\n\n"
        + ("Liquiditaet:\n" + "\n".join(f"- {s}" for s in liq) + "\n\n" if liq else "")
        + f"Letzte {len(rows)} Kerzen (UTC, open,high,low,close,volume), die letzte ist die neueste:\n" + "\n".join(rows) + "\n\n"
        "Entscheide jetzt."
    )


class ClaudeAdvisor:
    def __init__(self, cfg: AiConfig, api_key: str = "", client=None, futures: bool = False):
        self.cfg = cfg
        self.system_prompt = FUTURES_PROMPT if futures else SYSTEM_PROMPT
        if cfg.provider == "workers_ai":
            self.client = client or WorkersAiClient(cfg)
        elif cfg.provider == "openrouter":
            self.client = client or OpenRouterClient(cfg, api_key)
        else:
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
        if isinstance(self.client, (WorkersAiClient, OpenRouterClient)):
            try:
                text = self.client.complete(self.system_prompt, user_prompt)
            except Exception as exc:  # noqa: BLE001
                log.warning("Workers AI: %s", exc)
                return AiDecision("hold", 0.0, f"KI nicht erreichbar ({str(exc)[:80]})", ok=False)
            return self.parse_text(text)
        try:
            response = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=self.system_prompt,
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
        return ClaudeAdvisor.parse_text(text)

    @staticmethod
    def parse_text(text) -> AiDecision:
        if isinstance(text, dict):
            data = text
            text = json.dumps(text)
        if not text:
            return AiDecision("hold", 0.0, "KI lieferte keine Antwort", ok=False)
        try:
            if not isinstance(text, str):
                raise ValueError
            data = _find_decision_json(text)  # Modelle schreiben manchmal Text oder Markdown um das JSON
            action = str(data["action"]).lower()
            confidence = float(data["confidence"])
            reason = str(data["reason"]).strip()
        except (ValueError, KeyError, TypeError):
            return AiDecision("hold", 0.0, "KI-Antwort nicht lesbar", ok=False)
        if action not in ACTIONS or confidence != confidence:
            return AiDecision("hold", 0.0, "KI-Antwort ungueltig", ok=False)

        def num(key):
            try:
                v = float(data.get(key)) if data.get(key) is not None else None
            except (TypeError, ValueError):
                return None
            return v if v is not None and v == v and v > 0 else None

        return AiDecision(action, min(max(confidence, 0.0), 1.0), reason[:300], stop_loss=num("stop_loss"), take_profit=num("take_profit"))


class WorkersAiClient:
    """Sprachmodell von Cloudflare Workers AI. Der Bot im Container schickt die Anfrage an http://ai.tradingbot/,
    der Worker leitet sie an seine AI-Bindung weiter (kein Schluessel noetig, 10.000 Neurons am Tag kostenlos)."""

    def __init__(self, cfg: AiConfig, url: str | None = None, session: requests.Session | None = None):
        self.cfg = cfg
        self.url = url or os.environ.get("TRADINGBOT_AI_URL", "")
        self.http = session or requests.Session()

    def complete(self, system: str, user: str) -> str | dict:
        if not self.url:
            raise RuntimeError("Workers AI nur auf Cloudflare verfuegbar (TRADINGBOT_AI_URL fehlt)")
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 600,
            "response_format": {"type": "json_schema", "json_schema": DECISION_SCHEMA},
        }
        r = self.http.post(self.url, json=body, timeout=self.cfg.timeout_seconds)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        data = r.json()
        result = data.get("result", data)
        return result.get("response") if isinstance(result, dict) else result


class OpenRouterClient:
    """Modelle ueber openrouter.ai (OpenAI-kompatibel). Mit `fallback_models` springt OpenRouter selbst auf das
    naechste Modell, wenn eines ueberlastet ist (freie Modelle sind zu Spitzenzeiten oft knapp)."""

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, cfg: AiConfig, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY fehlt")
        self.cfg = cfg
        self.http = session or requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {api_key}", "X-Title": "Trading Bot"})

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 4000,
        }
        if self.cfg.fallback_models:
            body["models"] = [self.cfg.model, *self.cfg.fallback_models]
        r = self.http.post(self.URL, json=body, timeout=self.cfg.timeout_seconds)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        data = r.json()
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:150])
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content") or ""
