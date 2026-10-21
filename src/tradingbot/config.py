from __future__ import annotations

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
    trailing_stop: bool = True
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


class FuturesConfig(BaseModel):
    """Einstellungen fuer USDT-Perpetual-Futures (noetig fuer Short-Positionen)."""

    leverage: float = Field(default=1.0, ge=1.0, le=10.0)
    margin_mode: Literal["isolated", "cross"] = "isolated"


class DashboardConfig(BaseModel):
    enabled: bool = True
    port: int = Field(default=8765, ge=0, le=65535)


class BotConfig(BaseModel):
    mode: Mode = "paper"
    exchange: str = "binance"
    market: Literal["spot", "futures"] = "spot"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    quote_currency: str = "USDT"
    timeframe: str = "1h"
    poll_interval_seconds: int = 60
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
        return self.futures.leverage if self.is_futures else 1.0

    @model_validator(mode="after")
    def _market_rules(self):
        if self.strategy.allow_short and not self.is_futures:
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


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    path = Path(config_path)
    data = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    return Settings(bot=BotConfig(**data), secrets=Secrets())
