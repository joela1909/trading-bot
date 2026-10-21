from __future__ import annotations

import math

from .config import RiskConfig
from .models import MarketLimits


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def position_size(
        self,
        equity: float,
        free_quote: float,
        entry_price: float,
        stop_loss: float,
        limits: MarketLimits | None = None,
        side: str = "long",
        leverage: float = 1.0,
    ) -> float:
        """Positionsgroesse in Basiswaehrung. 0 bedeutet: keine Order.
        Bei Short liegt der Stop ueber dem Einstieg. Mit Hebel bindet die Position nur Nennwert / Hebel an Sicherheit."""
        limits = limits or MarketLimits()
        if entry_price <= 0 or equity <= 0 or free_quote <= 0:
            return 0.0
        per_unit_risk = stop_loss - entry_price if side == "short" else entry_price - stop_loss
        if per_unit_risk <= 0:
            return 0.0

        risk_amount = equity * self.cfg.risk_per_trade_pct / 100.0
        amount = risk_amount / per_unit_risk

        max_quote = min(equity * self.cfg.max_position_pct / 100.0, free_quote * 0.99 * max(leverage, 1.0))
        amount = min(amount, max_quote / entry_price)

        amount = self._round_down(amount, limits.amount_precision)

        cost = amount * entry_price
        if amount < limits.min_amount or cost < limits.min_cost or cost < self.cfg.min_order_quote:
            return 0.0
        return amount

    def can_open_new(self, open_positions: int) -> bool:
        return open_positions < self.cfg.max_open_positions

    def daily_loss_exceeded(self, day_start_equity: float, current_equity: float) -> bool:
        if day_start_equity <= 0:
            return False
        loss_pct = (day_start_equity - current_equity) / day_start_equity * 100.0
        return loss_pct >= self.cfg.max_daily_loss_pct

    @staticmethod
    def _round_down(value: float, precision: int) -> float:
        if precision < 0:
            return value
        factor = 10 ** precision
        return math.floor(value * factor) / factor
