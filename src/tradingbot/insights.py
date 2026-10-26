"""Zusatzwissen vor dem Einstieg, nach den Ideen aus github.com/Jakeschincariol/day-trader-skill:

- News-Richtung (trader-news): Schlagzeilen der letzten Minuten werden nach Stimmung bewertet. Sprechen sie klar fuer
  eine Richtung, handelt der Bot nur in diese Richtung; Signale dagegen werden verworfen. Ohne klare Stimmung bleibt
  alles erlaubt.
- Scanner (trader-scanner): die meistgehandelten Paare bzw. Aktien des Tages kommen zur festen Liste dazu.

Die Stimmung ist eine einfache Wortliste, kein Sprachmodell: schnell, ohne Kosten, nachvollziehbar."""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

log = logging.getLogger(__name__)

BULLISH = (
    "surge", "surges", "soar", "soars", "rally", "rallies", "jump", "jumps", "gain", "gains", "rise", "rises", "climb",
    "climbs", "beat", "beats", "record high", "all-time high", "approve", "approves", "approved", "approval", "upgrade",
    "upgrades", "upgraded", "partnership", "inflow", "inflows", "adopt", "adoption", "bullish", "buyback", "raises guidance",
    "raised guidance", "outperform", "breakout", "rebound", "rebounds", "strong demand", "accumulate", "accumulation",
    "etf approval", "listing", "lists", "wins", "secures", "boost", "boosts", "higher",
)
BEARISH = (
    "plunge", "plunges", "drop", "drops", "fall", "falls", "slump", "slumps", "crash", "crashes", "miss", "misses",
    "downgrade", "downgrades", "downgraded", "lawsuit", "sues", "hack", "hacked", "exploit", "exploited", "ban", "bans",
    "sell-off", "selloff", "outflow", "outflows", "investigation", "probe", "charges", "bankrupt", "bankruptcy", "delist",
    "delisting", "bearish", "cuts guidance", "cut guidance", "layoff", "layoffs", "decline", "declines", "tumble",
    "tumbles", "sink", "sinks", "slide", "slides", "liquidation", "liquidations", "warning", "recall", "fraud", "lower",
    "weak", "underperform", "dump", "dumps", "halt", "halts",
)
_WORDS = [(re.compile(r"\b" + re.escape(w) + r"\b", re.I), 1) for w in BULLISH] + [
    (re.compile(r"\b" + re.escape(w) + r"\b", re.I), -1) for w in BEARISH
]


def headline_sentiment(text: str) -> int:
    """+1 je Wort fuer steigende, -1 je Wort fuer fallende Kurse."""
    return sum(sign for rx, sign in _WORDS if rx.search(text or ""))


def news_direction(headlines: list[str]) -> tuple[str | None, int]:
    """("long" | "short" | None, Gesamtpunkte) aus den Schlagzeilen."""
    score = sum(max(-2, min(2, headline_sentiment(h))) for h in headlines)  # einzelne Schlagzeile max. 2 Punkte
    if score >= 1:
        return "long", score
    if score <= -1:
        return "short", score
    return None, score


class NewsDirectionGuard:
    """Einstiegspruefung: nur in die Richtung, fuer die die juengsten Schlagzeilen sprechen.

    `fetch(symbol, minutes)` liefert Schlagzeilen; Ergebnis je Paar `cache_seconds` lang zwischengespeichert."""

    def __init__(self, fetch: Callable[[str, int], list[str]], minutes: int, cache_seconds: float = 60.0):
        self.fetch = fetch
        self.minutes = minutes
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, str | None, int, str]] = {}

    def direction(self, symbol: str) -> tuple[str | None, int, str]:
        hit = self._cache.get(symbol)
        if hit is not None and time.time() - hit[0] < self.cache_seconds:
            return hit[1], hit[2], hit[3]
        headlines = self.fetch(symbol, self.minutes)
        side, score = news_direction(headlines)
        top = max(headlines, key=lambda h: abs(headline_sentiment(h)), default="")
        self._cache[symbol] = (time.time(), side, score, top)
        return side, score, top

    def __call__(self, symbol: str, side: str) -> tuple[bool, str]:
        want, score, top = self.direction(symbol)
        if want is None or want == side:
            return True, ""
        word = "Long" if want == "long" else "Short"
        return False, f"News sprechen fuer {word} ({score:+d}): {top[:70]}"


def alpaca_crypto_symbol(symbol: str) -> str:
    """BTC/USDT:USDT -> BTCUSD (so fuehrt Alpaca News die Kryptowerte)."""
    base = symbol.split("/")[0]
    return f"{base}USD"


STABLES = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE", "PYUSD", "EUR", "EURI", "USD1", "XUSD", "RLUSD"}


def crypto_most_active(futures_exchange, spot_exchange, top: int, quote: str = "USDT") -> list[str]:
    """Die `top` USDT-Perpetuals mit dem hoechsten 24h-Umsatz, die es auch als Spot-Paar gibt (fuer Spot-Signale).
    Rueckgabe im Futures-Format, z. B. SUI/USDT:USDT."""
    tickers = futures_exchange.fetch_tickers()
    spot = set(spot_exchange.load_markets()) if spot_exchange is not None else None
    rows = []
    for sym, t in tickers.items():
        if not sym.endswith(f"/{quote}:{quote}"):
            continue
        base = sym.split("/")[0]
        if base in STABLES or (spot is not None and f"{base}/{quote}" not in spot):
            continue
        rows.append((float(t.get("quoteVolume") or 0.0), sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:top]]


class CryptoScanner:
    """Erneuert alle `every_hours` Stunden die Paarliste: feste Paare + die meistgehandelten + Paare mit offener Position."""

    def __init__(self, bot, futures_exchange, spot_exchange, top: int, every_hours: float = 6.0):
        self.bot = bot
        self.futures_exchange = futures_exchange
        self.spot_exchange = spot_exchange
        self.top = top
        self.every = every_hours * 3600
        self.base = list(bot.cfg.symbols)
        self.scanned: list[str] = []
        self._last = 0.0

    def __call__(self) -> None:
        if time.time() - self._last < self.every:
            return
        self._last = time.time()
        self.scanned = crypto_most_active(self.futures_exchange, self.spot_exchange, self.top, self.bot.cfg.quote_currency)
        keep = [s for s in self.bot.state.positions if s not in self.base]
        symbols = list(dict.fromkeys(self.base + self.scanned + keep))
        if hasattr(self.bot.cfg, "model_copy"):
            # eigene Kopie: die gespeicherten Einstellungen (App) behalten die feste Liste
            self.bot.cfg = self.bot.cfg.model_copy(update={"symbols": symbols})
        else:
            self.bot.cfg.symbols = symbols
        log.info("Krypto-Scanner: %s", ", ".join(s.split("/")[0] for s in self.scanned) or "nichts gefunden")
