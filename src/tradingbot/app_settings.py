"""Einstellungen, die die App aendern darf.

Nur diese Felder sind aenderbar. Modus (Papier/Live), Boerse, Markt und Schluessel bleiben in config.yaml
und .env. Aenderungen landen in <state_dir>/settings_override.yaml und haben Vorrang vor config.yaml. Die Datei liegt
im Zustandsordner, damit sie auf einem Server (Docker-Volume) und auf Cloudflare (R2) erhalten bleibt.

Krypto und Aktien sind getrennt: Krypto steht oben in der Datei, Aktien unter `stocks:`. Speichern oder
Zuruecksetzen der einen Seite laesst die andere unangetastet."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import remote_state
from .config import BotConfig, Settings

OVERRIDE_FILE = "settings_override.yaml"
TIMEFRAMES = ("1m", "5m", "15m", "1h")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,12}/USDT$")
SUGGESTED_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT"]
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
SUGGESTED_STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD", "NFLX", "SPY", "QQQ"]
MARKETS = ("crypto", "stocks")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExitUpdate(_Strict):
    stop_atr_mult: float | None = Field(default=None, ge=0.5, le=20.0)
    take_profit_atr_mult: float | None = Field(default=None, ge=0.0, le=50.0)
    min_stop_pct: float | None = Field(default=None, ge=0.0, le=20.0)


class StrategyUpdate(_Strict):
    name: str
    min_votes: int = Field(default=2, ge=1, le=7)
    members: list[str] | None = None
    allow_short: bool = False
    stop_atr_mult: float = Field(default=2.0, ge=0.5, le=20.0)
    take_profit_atr_mult: float = Field(default=4.0, ge=0.0, le=50.0)
    min_stop_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    trailing_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    solo_member: str | None = None
    realtime_signals: bool = False
    signal_source: Literal["market", "spot"] = "market"
    exits: dict[str, ExitUpdate] | None = None  # Stop und Ziel je Strategie, leere Werte = Standard
    # Trendfilter fuer SuperTrend AI (0 = aus): Einstieg nur bei klarem Trend
    st_min_strength: int = Field(default=1, ge=1, le=4)
    st_adx_min: float = Field(default=0.0, ge=0.0, le=60.0)
    st_chop_max: float = Field(default=0.0, ge=0.0, le=100.0)
    st_trend_ema: int = Field(default=0, ge=0, le=2000)
    st_timeframe: Literal["5m", "15m", "1h"] | None = None  # SuperTrend auf groesseren Kerzen, None = Bot-Zeitrahmen


class RiskUpdate(_Strict):
    risk_per_trade_pct: float = Field(ge=0.1, le=5.0)
    max_position_pct: float = Field(ge=1.0, le=100.0)
    max_open_positions: int = Field(ge=1, le=10)
    max_daily_loss_pct: float = Field(ge=0.5, le=20.0)
    min_target_cost_ratio: float = Field(default=0.0, ge=0.0, le=20.0)


class DaytradingUpdate(_Strict):
    enabled: bool
    entry_start: str
    entry_end: str
    flat_time: str
    max_trades_per_day: int = Field(ge=0, le=100)


class ProtectionsUpdate(_Strict):
    # Sperre nach Stop-Loss-Serie: so viele Stops in kurzer Zeit sperren alle Einstiege (0 = aus)
    stoploss_guard_stops: int = Field(default=3, ge=0, le=20)
    # Abkuehlphase: so viele Kerzen nach einem Ausstieg kein neuer Einstieg im selben Paar (0 = aus)
    cooldown_candles: int | None = Field(default=None, ge=0, le=1000)


class FuturesUpdate(_Strict):
    leverage: float = Field(default=1.0, ge=1.0, le=100.0)


class SettingsUpdate(_Strict):
    symbols: list[str] = Field(min_length=1, max_length=10)
    timeframe: Literal["1m", "5m", "15m", "1h"]
    strategy: StrategyUpdate
    risk: RiskUpdate
    daytrading: DaytradingUpdate
    futures: FuturesUpdate | None = None
    protections: ProtectionsUpdate | None = None

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            base = s.strip().upper().split(":")[0]
            if not SYMBOL_RE.match(base):
                raise ValueError(f"'{s}' ist kein gueltiges Paar (Format z. B. BTC/USDT)")
            if base not in out:
                out.append(base)
        return out


class StockSettingsUpdate(SettingsUpdate):
    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            t = s.strip().upper()
            if not TICKER_RE.match(t):
                raise ValueError(f"'{s}' ist kein US-Tickersymbol (z. B. AAPL)")
            if t not in out:
                out.append(t)
        return out


def override_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / OVERRIDE_FILE


def load_override(state_dir: str | Path) -> dict:
    path = override_path(state_dir)
    remote_state.pull_if_missing(path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def current(settings: Settings, has_override: bool | None = None, market: str = "crypto") -> dict:
    """Aktuelle Werte und Auswahlmoeglichkeiten fuer die Einstellungsseite, je Markt (crypto | stocks)."""
    from .strategy import REGISTRY

    stocks = market == "stocks"
    if has_override is None:
        ov = load_override(settings.bot.state_dir)
        has_override = bool(ov.get("stocks")) if stocks else any(k != "stocks" for k in ov)
    if stocks:
        from .stocks import stock_settings

        settings = stock_settings(settings)
    b = settings.bot
    s = b.strategy
    members = s.model_extra.get("members") if s.model_extra else None
    return {
        "values": {
            "symbols": [x.split(":")[0] for x in b.symbols],
            "timeframe": b.timeframe,
            "strategy": {
                "name": s.name,
                "min_votes": int(getattr(s, "min_votes", 2) or 2),
                "members": list(members) if members else None,
                "allow_short": s.allow_short,
                "stop_atr_mult": s.stop_atr_mult,
                "take_profit_atr_mult": s.take_profit_atr_mult,
                "min_stop_pct": s.min_stop_pct,
                "trailing_pct": s.trailing_pct,
                "realtime_signals": s.realtime_signals,
                "signal_source": s.signal_source,
                "solo_member": (s.model_extra or {}).get("solo_member") or None,
                "exits": {n: e.model_dump() for n, e in s.exits.items()},
                **{k: (s.model_extra or {}).get(k) or d for k, d in
                   (("st_min_strength", 1), ("st_adx_min", 0), ("st_chop_max", 0), ("st_trend_ema", 0), ("st_timeframe", None))},
            },
            "risk": {
                "risk_per_trade_pct": b.risk.risk_per_trade_pct,
                "max_position_pct": b.risk.max_position_pct,
                "max_open_positions": b.risk.max_open_positions,
                "max_daily_loss_pct": b.risk.max_daily_loss_pct,
                "min_target_cost_ratio": b.risk.min_target_cost_ratio,
            },
            "costs_pct": round(2 * (b.paper.fee_pct + b.paper.slippage_pct), 4),
            "futures": {"leverage": b.futures.leverage},
            "protections": {
                "stoploss_guard_stops": b.protections.stoploss_guard_stops,
                "window_candles": b.protections.stoploss_guard_window_candles,
                "lock_candles": b.protections.stoploss_guard_lock_candles,
                "cooldown_candles": b.protections.cooldown_candles,
            },
            "daytrading": {
                "enabled": b.daytrading.enabled,
                "entry_start": b.daytrading.entry_start,
                "entry_end": b.daytrading.entry_end,
                "flat_time": b.daytrading.flat_time,
                "max_trades_per_day": b.daytrading.max_trades_per_day,
            },
        },
        "options": {
            "timeframes": list(TIMEFRAMES),
            "strategies": sorted(REGISTRY),
            "members": sorted(n for n in REGISTRY if n != "ensemble"),
            "own_exits": sorted(n for n, c in REGISTRY.items() if c.own_exits),
            "symbols": SUGGESTED_STOCKS if stocks else SUGGESTED_SYMBOLS,
            "symbol_kind": "ticker" if stocks else "pair",
            "short_possible": b.market in ("futures", "stocks"),
            "max_leverage": 100 if b.market in ("futures", "stocks") else 1,
        },
        "locked": {
            "mode": settings.mode,
            "exchange": b.exchange,
            "market": b.market,
            "timezone": b.daytrading.timezone,
        },
        "has_override": has_override,
    }


def validate_and_save(settings: Settings, base_config: dict, payload: dict, market: str = "crypto") -> dict:
    """Prueft die Eingaben vollstaendig (wie beim Start) und speichert sie. Wirft ValueError mit Klartext."""
    stocks = market == "stocks"
    try:
        update = (StockSettingsUpdate if stocks else SettingsUpdate)(**payload)
    except ValidationError as exc:
        raise ValueError(_first_error(exc)) from exc
    override = update.model_dump()
    if override.get("protections") is None:
        override.pop("protections", None)
    elif override["protections"].get("cooldown_candles") is None:
        override["protections"].pop("cooldown_candles", None)
    if override.get("futures") is None:
        override.pop("futures", None)
    elif not stocks and not settings.bot.is_futures and override["futures"]["leverage"] > 1:
        raise ValueError("Hebel gibt es nur mit market: futures")
    strat = override["strategy"]
    if not strat.get("members"):
        strat.pop("members", None)
    if strat.get("exits") is None:
        strat.pop("exits", None)
    else:
        from .strategy import REGISTRY as _reg

        bad = [n for n in strat["exits"] if n not in _reg or n == "ensemble"]
        if bad:
            raise ValueError(f"Stop und Ziel fuer unbekannte Strategie(n): {', '.join(bad)}")
    if not strat.get("solo_member"):
        strat["solo_member"] = None  # ausdruecklich keine, sonst gilt wieder die aus config.yaml
    if strat["name"] != "ensemble":
        strat.pop("members", None)
        strat.pop("min_votes", None)
        strat.pop("solo_member", None)
    existing = load_override(settings.bot.state_dir)
    if stocks:
        part = {
            # members ausdruecklich setzen: sonst erbt die Aktienseite die Auswahl der Krypto-Seite
            "symbols": override["symbols"], "timeframe": override["timeframe"], "strategy": {"members": None, **strat},
            "allow_short": strat.get("allow_short", True), "risk": override["risk"], "daytrading": override["daytrading"],
        }
        if "futures" in override:
            part["leverage"] = override["futures"]["leverage"]
        if "protections" in override:
            part["protections"] = override["protections"]
        full = {k: v for k, v in existing.items()}
        full["stocks"] = {**(existing.get("stocks") or {}), **part}
    else:
        full = dict(override)
        if existing.get("stocks"):
            full["stocks"] = existing["stocks"]
    try:
        cfg = BotConfig(**merge(base_config, full))
        if stocks:
            from .stocks import stock_settings

            stock_settings(Settings(bot=cfg, secrets=settings.secrets))
    except ValidationError as exc:
        raise ValueError(_first_error(exc)) from exc
    from .strategy import REGISTRY

    if strat["name"] not in REGISTRY:
        raise ValueError(f"Unbekannte Strategie '{strat['name']}'")
    solo = [strat["solo_member"]] if strat.get("solo_member") else []
    unknown = [m for m in (strat.get("members") or []) + solo if m not in REGISTRY or m == "ensemble"]
    if unknown:
        raise ValueError(f"Unbekannte Strategie(n) im Ensemble: {', '.join(unknown)}")
    members = strat.get("members")
    missing = [m for m in solo if members and m not in members]
    if missing:
        raise ValueError(f"{', '.join(missing)} soll allein einsteigen duerfen, ist aber kein Mitglied des Ensembles")

    _write(settings, full)
    return full


def _write(settings: Settings, data: dict) -> None:
    path = override_path(settings.bot.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False) if data else "{}\n"
    text = "# Von der App gespeichert. Hat Vorrang vor config.yaml. Loeschen = zurueck zu config.yaml.\n" + body
    path.write_text(text, encoding="utf-8")
    # auch hochladen, wenn leer: ein Neustart auf Cloudflare soll nicht die alte Fassung zurueckholen
    remote_state.push(path, text)


def reset(settings: Settings, market: str = "crypto") -> None:
    """Setzt einen Markt auf config.yaml zurueck. Der andere behaelt seine App-Einstellungen."""
    existing = load_override(settings.bot.state_dir)
    if market == "stocks":
        existing.pop("stocks", None)
        _write(settings, existing)
    else:
        _write(settings, {"stocks": existing["stocks"]} if existing.get("stocks") else {})


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(x) for x in err.get("loc", ()) if x != "__root__")
    msg = str(err.get("msg", "ungueltiger Wert")).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg
