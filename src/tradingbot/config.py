from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["paper", "live"]


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


class DashboardConfig(BaseModel):
    enabled: bool = True
    port: int = Field(default=8765, ge=0, le=65535)


class BotConfig(BaseModel):
    mode: Mode = "paper"
    exchange: str = "binance"
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
        for symbol in self.bot.symbols:
            if "/" not in symbol or symbol.split("/")[1] != self.bot.quote_currency:
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
