from __future__ import annotations

import math

from .config import RiskConfig
from .models import MarketLimits


MAINTENANCE_MARGIN = 0.005  # Binance USDT-Perpetuals, unterste Stufe: 0,5 % vom Nennwert
LIQ_SAFETY = 0.8  # Stop spaetestens bei 80 % des Wegs bis zur Liquidation


def liquidation_price(entry: float, side: str, leverage: float) -> float | None:
    """Ungefaehrer Liquidationskurs bei isolierter Sicherheit. Ohne Hebel keiner."""
    if leverage <= 1.0:
        return None
    dist = max(1.0 / leverage - MAINTENANCE_MARGIN, 0.0) * entry
    return entry + dist if side == "short" else entry - dist


def cap_stop(entry: float, stop: float, side: str, leverage: float) -> tuple[float, bool]:
    """Legt den Stop vor die Liquidation, falls er dahinter liegt. Gibt (Stop, wurde_verschoben) zurueck."""
    liq = liquidation_price(entry, side, leverage)
    if liq is None:
        return stop, False
    limit = entry + LIQ_SAFETY * (liq - entry)
    if (side == "short" and stop > limit) or (side != "short" and stop < limit):
        return limit, True
    return stop, False


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
        Bei Short liegt der Stop ueber dem Einstieg. Mit Hebel bindet die Position nur Nennwert / Hebel an Sicherheit.
        max_position_pct begrenzt diese Sicherheit (ohne Hebel = Nennwert)."""
        limits = limits or MarketLimits()
        if entry_price <= 0 or equity <= 0 or free_quote <= 0:
            return 0.0
        per_unit_risk = stop_loss - entry_price if side == "short" else entry_price - stop_loss
        if per_unit_risk <= 0:
            return 0.0

        risk_amount = equity * self.cfg.risk_per_trade_pct / 100.0
        amount = risk_amount / per_unit_risk

        lev = max(leverage, 1.0)
        max_quote = min(equity * self.cfg.max_position_pct / 100.0 * lev, free_quote * 0.99 * lev)
        amount = min(amount, max_quote / entry_price)

        amount = self._round_down(amount, limits.amount_precision)

        cost = amount * entry_price
        if amount < limits.min_amount or cost < limits.min_cost or cost < self.cfg.min_order_quote:
            return 0.0
        return amount

    def target_check(self, price: float, target_distance: float, fee_pct: float, slippage_pct: float) -> tuple[bool, float, float]:
        """Lohnt sich der Trade nach Kosten? target_distance: Abstand zum Ziel (ohne Ziel: zum Stop) in Kurs-Einheiten.
        Gibt (ok, Ziel in %, Kosten hin und zurueck in %) zurueck."""
        if price <= 0:
            return False, 0.0, 0.0
        target_pct = target_distance / price * 100.0
        cost_pct = 2.0 * (fee_pct + slippage_pct)
        ratio = self.cfg.min_target_cost_ratio
        return (ratio <= 0 or target_pct >= ratio * cost_pct), target_pct, cost_pct

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
