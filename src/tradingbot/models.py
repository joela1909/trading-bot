from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Signal(str, Enum):
    BUY = "buy"  # Long eroeffnen
    SELL = "sell"  # Long schliessen
    SHORT = "short"  # Short eroeffnen
    COVER = "cover"  # Short schliessen
    HOLD = "hold"


def side_sign(side: str) -> int:
    return -1 if side == "short" else 1


@dataclass
class Decision:
    signal: Signal
    reason: str
    stop_loss: float | None = None
    take_profit: float | None = None
    atr: float | None = None
    adjust: bool = False  # HOLD mit neuem Stop/Ziel fuer die offene Position (z. B. von der KI)


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
    highest_price: float  # bester Kurs seit Einstieg: bei Long das Hoch, bei Short das Tief (fuer den Trailing-Stop)
    entry_fee: float = 0.0
    atr_at_entry: float | None = None
    stop_order_id: str | None = None
    exchange_stop: float | None = None
    side: str = "long"  # long | short
    leverage: float = 1.0
    strategy: str = ""  # ausloesende Strategie(n), mehrere mit "+" verbunden
    initial_stop: float | None = None  # Stop beim Einstieg
    stop_kind: str = "initial"  # woher der aktuelle Stop kommt: initial | line | trailing

    def set_stop(self, value: float, kind: str) -> None:
        self.stop_loss = value
        self.stop_kind = kind

    @property
    def is_short(self) -> bool:
        return self.side == "short"

    def unrealized(self, price: float) -> float:
        """Kursgewinn ohne Gebuehren."""
        return side_sign(self.side) * (price - self.entry_price) * self.amount

    def value(self, price: float) -> float:
        """Wert der Position im Konto: gebundene Sicherheit plus Kursgewinn. Spot-Long: Menge * Kurs."""
        return self.entry_price * self.amount / self.leverage + self.unrealized(price)

    @property
    def liquidation_price(self) -> float | None:
        from .risk import liquidation_price

        return liquidation_price(self.entry_price, self.side, self.leverage)

    def stop_hit(self, price: float) -> bool:
        return price >= self.stop_loss if self.is_short else price <= self.stop_loss

    def take_profit_hit(self, price: float) -> bool:
        if self.take_profit is None:
            return False
        return price <= self.take_profit if self.is_short else price >= self.take_profit

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
    side: str = "long"
    strategy: str = ""  # ausloesende Strategie(n), mehrere mit "+" verbunden
    leverage: float | None = None  # Hebel des Trades (None = vor dieser Aufzeichnung)
    initial_stop: float | None = None  # Stop beim Einstieg
    stop_kind: str = ""  # bei Stop-Ausstieg: initial | line | trailing

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        return cls(**d)
