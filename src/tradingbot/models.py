from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Decision:
    signal: Signal
    reason: str
    stop_loss: float | None = None
    take_profit: float | None = None
    atr: float | None = None


@dataclass
class MarketLimits:
    min_amount: float = 0.0
    min_cost: float = 0.0
    amount_precision: int = 8


@dataclass
class Fill:
    symbol: str
    side: str
    amount: float
    price: float
    cost: float
    fee: float
    timestamp: str = field(default_factory=utcnow_iso)
    order_id: str | None = None


@dataclass
class Position:
    symbol: str
    amount: float
    entry_price: float
    stop_loss: float
    take_profit: float | None
    opened_at: str
    highest_price: float
    entry_fee: float = 0.0
    atr_at_entry: float | None = None
    stop_order_id: str | None = None
    exchange_stop: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)


@dataclass
class Trade:
    symbol: str
    amount: float
    entry_price: float
    exit_price: float
    pnl_quote: float
    pnl_pct: float
    fees: float
    opened_at: str
    closed_at: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        return cls(**d)
