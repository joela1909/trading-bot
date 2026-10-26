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


def _fmt_usd(x: float) -> str:
    return f"{x / 1e6:.2f} Mio" if x >= 1e6 else f"{x / 1e3:.0f} Tsd"


def liquidity_zones(df, price: float, lookback: int = 300, k: int = 3) -> list[str]:
    """Liquiditaetszonen aus den Kerzen: Swing-Hochs/-Tiefs (5-Kerzen-Fraktal), gleiche Hochs/Tiefs, Vortag."""
    import numpy as np

    if df is None or len(df) < 20:
        return []
    d = df.tail(lookback).reset_index(drop=True)
    h, lo = d["high"].to_numpy(), d["low"].to_numpy()
    n = len(d)
    highs = [float(h[i]) for i in range(2, n - 2) if h[i] == max(h[i - 2:i + 3])]
    lows = [float(lo[i]) for i in range(2, n - 2) if lo[i] == min(lo[i - 2:i + 3])]
    above = sorted(x for x in highs if x > price)[:k]
    below = sorted((x for x in lows if x < price), reverse=True)[:k]

    def pct(x):
        return f"{(x / price - 1) * 100:+.2f}%"

    out = []
    if above:
        out.append("Swing-Hochs darueber (Stops von Shorts/Kaeufer-Ausbrueche): " + ", ".join(f"{x:.6g} ({pct(x)})" for x in above))
    if below:
        out.append("Swing-Tiefs darunter (Stops von Longs): " + ", ".join(f"{x:.6g} ({pct(x)})" for x in below))

    def equal(levels):
        lv = sorted(levels)
        return sorted({round((a + b) / 2, 10) for a, b in zip(lv, lv[1:]) if abs(b / a - 1) < 0.001})

    eqh = [x for x in equal(highs) if x > price][:2]
    eql = [x for x in equal(lows) if x < price][-2:]
    if eqh:
        out.append("Gleiche Hochs (viel Liquiditaet): " + ", ".join(f"{x:.6g} ({pct(x)})" for x in eqh))
    if eql:
        out.append("Gleiche Tiefs (viel Liquiditaet): " + ", ".join(f"{x:.6g} ({pct(x)})" for x in eql))
    ts = d["timestamp"].to_numpy()
    day = ts // 86_400_000
    prev = day[-1] - 1
    mask = day == prev
    if mask.any():
        pdh, pdl = float(np.max(h[mask])), float(np.min(lo[mask]))
        out.append(f"Vortag (UTC): Hoch {pdh:.6g} ({pct(pdh)}), Tief {pdl:.6g} ({pct(pdl)})")
    return out


def orderbook_liquidity(exchange, symbol: str, price: float | None = None) -> tuple[list[str], float | None]:
    """Orderbuch-Tiefe um den Mittelkurs: Gebote/Angebote im sichtbaren Bereich, Ungleichgewicht, groesste Waende.
    Gibt (Saetze, Mittelkurs) zurueck."""
    ob = exchange.fetch_order_book(symbol, limit=1000)
    bids, asks = ob.get("bids") or [], ob.get("asks") or []
    if not bids or not asks:
        return [], price
    mid = (bids[0][0] + asks[0][0]) / 2
    reach = min(mid / bids[-1][0] - 1, asks[-1][0] / mid - 1) * 100  # so weit reicht das abgerufene Buch
    out = []
    bands = [b for b in (0.1, 0.5, 1.0) if b <= reach + 1e-9] or [round(reach, 2)]
    for band in bands:
        b = sum(p * a for p, a, *_ in bids if p >= mid * (1 - band / 100))
        a_ = sum(p * a for p, a, *_ in asks if p <= mid * (1 + band / 100))
        if b + a_ > 0:
            out.append(f"Orderbuch bis {band:g} % um den Mittelkurs {mid:.6g}: Kaeufer {_fmt_usd(b)} USDT, Verkaeufer {_fmt_usd(a_)} USDT, "
                       f"Ungleichgewicht {(b - a_) / (b + a_) * 100:+.0f} % ({'mehr Kaeufer' if b > a_ else 'mehr Verkaeufer'})")

    def walls(side, rows):
        top = sorted(((p, p * a) for p, a, *_ in rows), key=lambda r: -r[1])[:2]
        return f"Groesste {side}-Waende (sichtbar bis {reach:.2f} %): " + ", ".join(
            f"{p:.6g} ({(p / mid - 1) * 100:+.2f}%, {_fmt_usd(v)})" for p, v in top) if top else ""

    for line in (walls("Kauf", bids), walls("Verkaufs", asks)):
        if line:
            out.append(line)
    return out, mid


def derivatives_positioning(exchange, symbol: str) -> list[str]:
    """Funding und Veraenderung des Open Interest (Positionierung der Futures-Haendler)."""
    out = []
    try:
        fr = exchange.fetch_funding_rate(symbol).get("fundingRate")
        if fr is not None:
            out.append(f"Funding: {fr * 100:+.4f} % je 8 h ({'Longs zahlen, viele Longs' if fr > 0 else 'Shorts zahlen, viele Shorts'})")
    except Exception:  # noqa: BLE001
        pass
    try:
        hist = exchange.fetch_open_interest_history(symbol, "1h", limit=25)
        vals = [float(h.get("openInterestValue") or h.get("openInterestAmount") or 0) for h in hist]
        vals = [v for v in vals if v > 0]
        if len(vals) >= 2:
            out.append(f"Open Interest: {_fmt_usd(vals[-1])} USDT, 1 h {(vals[-1] / vals[-2] - 1) * 100:+.2f} %, "
                       f"24 h {(vals[-1] / vals[0] - 1) * 100:+.2f} %")
    except Exception:  # noqa: BLE001
        pass
    return out


class LiquidityContext:
    """Zusatzwissen fuer die KI: Liquiditaetszonen aus den Kerzen, Orderbuch, Funding, Open Interest."""

    def __init__(self, futures_exchange):
        self.ex = futures_exchange

    def __call__(self, symbol: str, candles) -> list[str]:
        price = float(candles["close"].iloc[-1]) if candles is not None and len(candles) else None
        book: list[str] = []
        try:
            book, mid = orderbook_liquidity(self.ex, symbol, price)
            price = mid or price  # Zonen auf den aktuellen Kurs beziehen, nicht auf den letzten Kerzenschluss
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: Orderbuch nicht abrufbar (%s)", symbol, exc)
        out = liquidity_zones(candles, price) if price else []
        return out + book + derivatives_positioning(self.ex, symbol)
