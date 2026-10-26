from __future__ import annotations

import re

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["paper", "live"]
PASSPHRASE_EXCHANGES = ("bitget", "okx", "kucoin", "coinbase", "coinbaseadvanced", "bitmart", "cryptocom")


class PaperConfig(BaseModel):
    starting_balance: float = 1000.0
    fee_pct: float = 0.1
    slippage_pct: float = 0.05


class ExitOverride(BaseModel):
    """Stop und Ziel fuer eine einzelne Strategie. Leer (None) = Wert aus dem Strategie-Abschnitt."""

    stop_atr_mult: float | None = Field(default=None, ge=0.5, le=20.0)
    take_profit_atr_mult: float | None = Field(default=None, ge=0.0, le=50.0)
    min_stop_pct: float | None = Field(default=None, ge=0.0, le=20.0)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "ema_cross"
    fast_ema: int = 20
    slow_ema: int = 50
    rsi_period: int = 14
    rsi_max_entry: float = 70.0
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    take_profit_atr_mult: float = 4.0
    # Stop mindestens so viele Prozent vom Kurs entfernt, auch wenn stop_atr_mult x ATR kleiner ist (0 = aus).
    # Das Ziel waechst dann im selben Verhaeltnis mit (take_profit_atr_mult / stop_atr_mult bleibt gleich).
    min_stop_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    trailing_stop: bool = True
    # Trailing-Stop mit festem Abstand in % vom besten Kurs seit Einstieg (0 = wie der Stop: ATR bzw. min_stop_pct).
    # Er wandert nur in Richtung der Position: bei Long nur nach oben, bei Short nur nach unten.
    trailing_pct: float = Field(default=0.0, ge=0.0, le=20.0)
    # Echtzeit: Signale auf der noch laufenden Kerze, Ein- und Ausstieg sofort (sonst erst beim Kerzenschluss).
    # Ein Signal kann bis zum Kerzenschluss wieder verschwinden. Der Backtest rechnet immer mit Kerzenschluss.
    realtime_signals: bool = False
    # Woher die Kerzen fuer Signale und Chart kommen: "market" = Handelsmarkt (z. B. Futures), "spot" = Spot-Paar.
    # Gehandelt wird immer auf dem eingestellten Markt; Stops und Ziele pruefen den Kurs des Handelsmarkts.
    signal_source: Literal["market", "spot"] = "market"
    # Stop und Ziel je Strategie, z. B. {"rsi_reversion": {"stop_atr_mult": 1.5, "take_profit_atr_mult": 2}}.
    # Gilt fuer die Strategie allein und als Mitglied im Ensemble; fehlende Werte kommen von oben.
    exits: dict[str, ExitOverride] = Field(default_factory=dict)
    allow_short: bool = False  # Short-Positionen, nur mit market: futures

    @field_validator("slow_ema")
    @classmethod
    def _slow_gt_fast(cls, v: int, info):
        fast = info.data.get("fast_ema")
        if fast is not None and v <= fast:
            raise ValueError("slow_ema muss groesser als fast_ema sein")
        return v


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 25.0
    max_open_positions: int = 3
    max_daily_loss_pct: float = 3.0
    min_order_quote: float = 10.0
    # Kein Einstieg, wenn das Ziel (Take-Profit, ohne Take-Profit der Stop-Abstand) kleiner ist als dieses Vielfache
    # der Handelskosten hin und zurueck (2 x Gebuehr + 2 x Kursabweichung). 0 = aus.
    min_target_cost_ratio: float = Field(default=0.0, ge=0.0, le=20.0)


def _parse_hhmm(value: str) -> int:
    try:
        hh, mm = value.split(":")
        minutes = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Uhrzeit '{value}' muss im Format HH:MM sein") from exc
    if not 0 <= minutes <= 24 * 60:
        raise ValueError(f"Uhrzeit '{value}' ausserhalb von 00:00 bis 24:00")
    return minutes


class DaytradingConfig(BaseModel):
    """Regeln fuer den Tageshandel. Alle Uhrzeiten gelten in `timezone`.

    Neue Einstiege nur zwischen entry_start und entry_end, ab flat_time werden alle Positionen geschlossen.
    Keine Position wird ueber Nacht gehalten."""

    enabled: bool = False
    timezone: str = "Europe/Berlin"
    entry_start: str = "08:00"
    entry_end: str = "21:30"
    flat_time: str = "22:00"
    max_trades_per_day: int = Field(default=6, ge=0)  # 0 = unbegrenzt
    max_hold_minutes: int = Field(default=0, ge=0)  # 0 = nur durch flat_time begrenzt

    @field_validator("timezone")
    @classmethod
    def _tz_exists(cls, v: str):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unbekannte Zeitzone '{v}'") from exc
        return v

    @model_validator(mode="after")
    def _times_ordered(self):
        if not self.entry_start_min < self.entry_end_min <= self.flat_min:
            raise ValueError("Es muss gelten: entry_start < entry_end <= flat_time (ohne Mitternacht dazwischen)")
        return self

    @property
    def entry_start_min(self) -> int:
        return _parse_hhmm(self.entry_start)

    @property
    def entry_end_min(self) -> int:
        return _parse_hhmm(self.entry_end)

    @property
    def flat_min(self) -> int:
        return _parse_hhmm(self.flat_time)


class NotificationConfig(BaseModel):
    telegram_enabled: bool = False


class ProtectionConfig(BaseModel):
    cooldown_candles: int = Field(default=2, ge=0)
    stoploss_guard_stops: int = Field(default=3, ge=0)
    stoploss_guard_window_candles: int = Field(default=24, ge=1)
    stoploss_guard_lock_candles: int = Field(default=12, ge=1)
    max_drawdown_pct: float = Field(default=20.0, ge=0)
    max_drawdown_lock_candles: int = Field(default=48, ge=1)
    # Bilanz-Filter: vor jedem Einstieg spielt der Bot dieselbe Strategie auf den letzten `track_record_bars` Kerzen
    # dieses Paars nach (wie die Time Machine). Haben ihre letzten `track_record_trades` Trades im Schnitt verloren,
    # steigt er nicht ein (0 = aus). Weniger Trades in der Vorgeschichte: Einstieg erlaubt.
    track_record_trades: int = Field(default=0, ge=0, le=50)
    track_record_bars: int = Field(default=4320, ge=100, le=50000)


class ExecutionConfig(BaseModel):
    stoploss_on_exchange: bool = False
    stoploss_replace_threshold_pct: float = Field(default=0.25, ge=0)


class AiConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    min_confidence_exit: float = Field(default=0.5, ge=0.0, le=1.0)
    max_calls_per_day: int = Field(default=200, ge=1)
    candles_in_prompt: int = Field(default=48, ge=10, le=300)
    timeout_seconds: float = Field(default=90.0, ge=5.0)
    max_tokens: int = Field(default=16000, ge=2000)


def _us_session() -> "DaytradingConfig":
    # Nach der Eroeffnungsspanne (9:30-10:00) bis kurz vor Boersenschluss, nichts ueber Nacht
    return DaytradingConfig(enabled=True, timezone="America/New_York", entry_start="10:00", entry_end="15:45",
                            flat_time="15:55", max_trades_per_day=6)


class StocksConfig(BaseModel):
    """US-Aktien im Papierhandel. Kurse von Alpaca (kostenloses Papierkonto), gehandelt wird auf einem eigenen
    Papierkonto wie bei Krypto. Strategie und Risiko wie oben, mit den Abweichungen aus strategy/risk hier."""

    enabled: bool = False
    provider: Literal["alpaca"] = "alpaca"
    data_feed: Literal["iex", "sip"] = "iex"  # iex: kostenlos in Echtzeit, sip: alle US-Boersen (Abo)
    symbols: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "SPY", "QQQ"])
    timeframe: str = "5m"
    poll_interval_seconds: int = Field(default=15, ge=5)  # Alpaca erlaubt kostenlos 200 Anfragen pro Minute
    starting_balance: float = Field(default=10000.0, gt=0)
    fee_pct: float = Field(default=0.0, ge=0)  # Alpaca: keine Kommission
    slippage_pct: float = Field(default=0.02, ge=0)
    allow_short: bool = True
    leverage: float = Field(default=1.0, ge=1.0, le=100.0)  # nur Papierhandel: wie bei Krypto-Futures simuliert
    strategy: dict = Field(default_factory=lambda: {"orb_sessions": ["09:30 America/New_York"], "orb_weekdays": True})
    risk: dict = Field(default_factory=dict)
    protections: dict = Field(default_factory=dict)  # Abweichungen von protections oben
    daytrading: "DaytradingConfig" = Field(default_factory=_us_session)
    # Scanner: taeglich die meistgehandelten Aktien zu `symbols` dazunehmen (0 = aus), wie trader-scanner
    scanner_top: int = Field(default=0, ge=0, le=30)
    # News-Sperre: kein Einstieg, wenn es in den letzten so vielen Minuten Schlagzeilen gab (0 = aus), wie trader-news
    news_block_minutes: int = Field(default=0, ge=0, le=1440)

    @field_validator("symbols")
    @classmethod
    def _tickers(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            t = s.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", t):
                raise ValueError(f"'{s}' ist kein US-Tickersymbol (z. B. AAPL)")
            if t not in out:
                out.append(t)
        if not out:
            raise ValueError("mindestens eine Aktie erforderlich")
        return out


class FuturesConfig(BaseModel):
    """Einstellungen fuer USDT-Perpetual-Futures (noetig fuer Short-Positionen)."""

    leverage: float = Field(default=1.0, ge=1.0, le=100.0)
    margin_mode: Literal["isolated", "cross"] = "isolated"


class DashboardConfig(BaseModel):
    enabled: bool = True
    port: int = Field(default=8765, ge=0, le=65535)
    # weitere Host-Namen hinter einem Reverse-Proxy mit Passwort, z. B. 203-0-113-10.sslip.io (auch per DASHBOARD_HOSTS in .env)
    allowed_hosts: list[str] = Field(default_factory=list)


class BotConfig(BaseModel):
    mode: Mode = "paper"
    exchange: str = "binance"
    market: Literal["spot", "futures", "stocks"] = "spot"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    quote_currency: str = "USDT"
    timeframe: str = "1h"
    poll_interval_seconds: int = 60
    keep_awake: bool = True  # Windows: Ruhemodus verhindern, solange der Bot laeuft
    state_dir: str = "state"
    log_dir: str = "logs"
    paper: PaperConfig = Field(default_factory=PaperConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    protections: ProtectionConfig = Field(default_factory=ProtectionConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    daytrading: DaytradingConfig = Field(default_factory=DaytradingConfig)
    futures: FuturesConfig = Field(default_factory=FuturesConfig)
    stocks: StocksConfig = Field(default_factory=StocksConfig)

    @property
    def is_futures(self) -> bool:
        return self.market == "futures"

    def market_symbol(self, symbol: str) -> str:
        """Schreibweise fuer ccxt: bei Futures BTC/USDT -> BTC/USDT:USDT."""
        if self.is_futures and ":" not in symbol:
            return f"{symbol}:{symbol.split('/')[1]}"
        return symbol

    @property
    def leverage(self) -> float:
        return self.futures.leverage if self.market in ("futures", "stocks") else 1.0

    @model_validator(mode="after")
    def _market_rules(self):
        if self.strategy.allow_short and self.market == "spot":
            raise ValueError("Short-Positionen (strategy.allow_short) gehen nur mit market: futures, nicht im Spot-Handel")
        if self.strategy.allow_short and self.strategy.name == "claude_advisor":
            raise ValueError("Die KI-Strategie claude_advisor kann noch keine Short-Positionen. Setze allow_short: false.")
        if self.is_futures:
            # ccxt-Schreibweise fuer USDT-Perpetuals: BTC/USDT -> BTC/USDT:USDT
            self.symbols = [s if ":" in s else f"{s}:{s.split('/')[1]}" for s in self.symbols]
        return self

    @property
    def session_cfg(self) -> DaytradingConfig | None:
        """Tageshandel-Regeln, falls eingeschaltet, sonst None."""
        return self.daytrading if self.daytrading.enabled else None

    @field_validator("symbols")
    @classmethod
    def _symbols_not_empty(cls, v: list[str]):
        if not v:
            raise ValueError("mindestens ein Symbol erforderlich")
        return v


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    trading_mode: Mode | None = None
    live_trading_confirmed: bool = False
    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    exchange_api_password: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dashboard_hosts: str = ""  # komma-getrennt, ergaenzt dashboard.allowed_hosts
    alpaca_api_key: str = ""  # Aktien: Schluessel eines Alpaca-Papierkontos (nur Kurse, keine Orders)
    alpaca_api_secret: str = ""

    @property
    def dashboard_host_list(self) -> list[str]:
        return [h.strip() for h in self.dashboard_hosts.split(",") if h.strip()]


class Settings(BaseModel):
    bot: BotConfig
    secrets: Secrets

    @property
    def mode(self) -> Mode:
        return self.secrets.trading_mode or self.bot.mode

    def validate_for_run(self) -> None:
        if self.mode == "live":
            if not self.secrets.live_trading_confirmed:
                raise RuntimeError(
                    "Live-Modus angefordert, aber LIVE_TRADING_CONFIRMED ist nicht true. "
                    "Setze LIVE_TRADING_CONFIRMED=true in .env, wenn du wirklich mit echtem Geld handeln willst."
                )
            if not self.secrets.exchange_api_key or not self.secrets.exchange_api_secret:
                raise RuntimeError("Live-Modus: EXCHANGE_API_KEY und EXCHANGE_API_SECRET fehlen in .env")
            if self.bot.exchange in PASSPHRASE_EXCHANGES and not self.secrets.exchange_api_password:
                raise RuntimeError(f"{self.bot.exchange} verlangt zusaetzlich eine Passphrase: EXCHANGE_API_PASSWORD fehlt in .env")
        if self.mode == "live" and self.bot.is_futures:
            raise RuntimeError(
                "Futures (und damit Short-Positionen) sind bisher nur im Papierhandel freigegeben. "
                "Die Live-Anbindung fuer Futures ist noch nicht gegen eine echte Boerse getestet."
            )
        if self.bot.strategy.name == "claude_advisor" and not self.secrets.anthropic_api_key:
            raise RuntimeError("Die Strategie claude_advisor braucht ANTHROPIC_API_KEY in .env (Zugang zur Anthropic-API).")
        for symbol in self.bot.symbols:
            if "/" not in symbol or symbol.split("/")[1].split(":")[0] != self.bot.quote_currency:
                raise RuntimeError(
                    f"Symbol {symbol} passt nicht zur quote_currency {self.bot.quote_currency}"
                )


def load_base_config(config_path: str | Path = "config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def load_settings(config_path: str | Path = "config.yaml", use_override: bool = True) -> Settings:
    """config.yaml plus die in der App gespeicherten Einstellungen (state/settings_override.yaml).
    Passt die App-Fassung nicht mehr zu config.yaml, gilt config.yaml und es gibt eine Warnung im Log."""
    data = load_base_config(config_path)
    if use_override:
        from .app_settings import load_override, merge

        override = load_override(data.get("state_dir", "state"))
        if override:
            try:
                return Settings(bot=BotConfig(**merge(data, override)), secrets=Secrets())
            except ValueError as exc:
                import logging

                logging.getLogger(__name__).warning("App-Einstellungen ungueltig, es gilt config.yaml: %s", exc)
    return Settings(bot=BotConfig(**data), secrets=Secrets())
