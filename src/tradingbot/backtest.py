from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import PaperConfig, ProtectionConfig, StrategyConfig
from .models import MarketLimits, Position, Trade
from .protections import Protections
from .risk import RiskManager
from .strategy import Strategy


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="minutes")


@dataclass
class BacktestResult:
    symbol: str
    start_balance: float
    end_equity: float
    total_return_pct: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    n_trades: int
    win_rate_pct: float
    profit_factor: float | None
    total_fees: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    in_position: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool), repr=False)

    @property
    def calmar(self) -> float | None:
        return self.total_return_pct / self.max_drawdown_pct if self.max_drawdown_pct > 0 else None

    def summary(self) -> str:
        pf = f"{self.profit_factor:.2f}" if self.profit_factor is not None else "n/a"
        lines = [
            f"Backtest {self.symbol}",
            f"  Zeitraum:        {self.equity_curve.index[0]} bis {self.equity_curve.index[-1]}" if len(self.equity_curve) else "",
            f"  Startkapital:    {self.start_balance:.2f}",
            f"  Endkapital:      {self.end_equity:.2f}",
            f"  Rendite:         {self.total_return_pct:+.2f} %",
            f"  Buy & Hold:      {self.buy_hold_return_pct:+.2f} %",
            f"  Max. Drawdown:   {self.max_drawdown_pct:.2f} %",
            f"  Trades:          {self.n_trades}",
            f"  Trefferquote:    {self.win_rate_pct:.1f} %",
            f"  Profitfaktor:    {pf}",
            f"  Gebuehren:       {self.total_fees:.2f}",
        ]
        return "\n".join(l for l in lines if l)


class Backtester:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskManager,
        strategy_cfg: StrategyConfig,
        paper_cfg: PaperConfig,
        limits: MarketLimits | None = None,
        protections_cfg: ProtectionConfig | None = None,
    ):
        self.protections_cfg = protections_cfg
        self.strategy = strategy
        self.risk = risk
        self.scfg = strategy_cfg
        self.start_balance = paper_cfg.starting_balance
        self.fee_rate = paper_cfg.fee_pct / 100.0
        self.slip = paper_cfg.slippage_pct / 100.0
        self.limits = limits or MarketLimits()

    def run(
        self,
        df: pd.DataFrame,
        symbol: str,
        start: int = 1,
        end: int | None = None,
        prepared: bool = False,
        signals: tuple[np.ndarray, np.ndarray] | None = None,
    ) -> BacktestResult:
        """Simuliert die Strategie auf den Kerzen [start, end). Indikatoren werden auf der ganzen Reihe
        berechnet (sie sind kausal), der Handel beginnt aber erst bei `start` mit frischem Startkapital."""
        if len(df) < self.strategy.warmup_bars:
            raise ValueError(f"Zu wenig Kerzen ({len(df)}), mindestens {self.strategy.warmup_bars} noetig")
        if not prepared:
            df = self.strategy.prepare(df)
        buy_sig, sell_sig = signals if signals is not None else self.strategy.signals(df)
        n = len(df)
        end = n if end is None else min(end, n)
        start = max(start, 1)
        if end - start < 1:
            raise ValueError("Leerer Zeitraum fuer den Backtest")

        ts = df["timestamp"].to_numpy()
        high = df["high"].tolist()
        low = df["low"].tolist()
        close = df["close"].tolist()
        atr_v = df["atr"].tolist()
        buy = buy_sig.tolist()
        sell = sell_sig.tolist()

        fee_rate, slip = self.fee_rate, self.slip
        stop_mult = self.scfg.stop_atr_mult
        tp_mult = self.scfg.take_profit_atr_mult
        trailing = self.scfg.trailing_stop

        tf_ms = int(np.median(np.diff(ts[: min(len(ts), 500)]))) if len(ts) > 1 else 3_600_000
        prot = Protections(self.protections_cfg, tf_ms) if self.protections_cfg is not None else None

        cash = self.start_balance
        pos: Position | None = None
        trades: list[Trade] = []
        equity = np.empty(end - start)
        in_pos = np.zeros(end - start, dtype=bool)
        total_fees = 0.0

        def close_position(p: Position, price: float, ms: int, reason: str) -> None:
            nonlocal cash, total_fees
            proceeds = p.amount * price
            fee = proceeds * fee_rate
            cash += proceeds - fee
            fees = fee + p.entry_fee
            total_fees += fee
            pnl = (price - p.entry_price) * p.amount - fees
            trades.append(Trade(symbol, p.amount, p.entry_price, price, pnl, pnl / (p.entry_price * p.amount) * 100.0, fees, p.opened_at, _ts(ms), reason))
            if prot is not None:
                prot.on_exit(symbol, reason, ms)

        for k in range(end - start):
            i = start + k
            c = close[i]
            a = atr_v[i]
            if pos is not None:
                exit_price = None
                reason = ""
                if low[i] <= pos.stop_loss:
                    exit_price, reason = pos.stop_loss * (1 - slip), "stop_loss"
                elif pos.take_profit is not None and high[i] >= pos.take_profit:
                    exit_price, reason = pos.take_profit * (1 - slip), "take_profit"
                elif sell[i]:
                    exit_price, reason = c * (1 - slip), "signal"
                if exit_price is not None:
                    close_position(pos, exit_price, int(ts[i]), reason)
                    pos = None
                elif trailing and high[i] > pos.highest_price:
                    pos.highest_price = high[i]
                    if a == a and a > 0:
                        pos.stop_loss = max(pos.stop_loss, self.strategy.trailing_stop(pos.highest_price, a))
            elif buy[i] and a == a and a > 0 and (prot is None or prot.entry_allowed(symbol, int(ts[i]))[0]):
                entry = c * (1 + slip)
                stop = entry - stop_mult * a
                amount = self.risk.position_size(cash, cash, entry, stop, self.limits)
                if amount > 0:
                    cost = amount * entry
                    fee = cost * fee_rate
                    cash -= cost + fee
                    total_fees += fee
                    tp = entry + tp_mult * a if tp_mult > 0 else None
                    pos = Position(symbol, amount, entry, stop, tp, _ts(int(ts[i])), entry, fee, a)
            equity[k] = cash + (pos.amount * c if pos is not None else 0.0)
            in_pos[k] = pos is not None
            if prot is not None:
                prot.update_equity(equity[k], int(ts[i]))

        if pos is not None:
            close_position(pos, close[end - 1] * (1 - slip), int(ts[end - 1]), "end_of_data")
            equity[-1] = cash

        curve = pd.Series(equity, index=pd.to_datetime(ts[start:end], unit="ms", utc=True), name="equity")
        running_max = np.maximum.accumulate(equity)
        drawdown = float(((equity - running_max) / running_max).min() * 100.0)
        wins = [t for t in trades if t.pnl_quote > 0]
        gross_profit = sum(t.pnl_quote for t in wins)
        gross_loss = -sum(t.pnl_quote for t in trades if t.pnl_quote <= 0)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if not trades else float("inf"))

        return BacktestResult(
            symbol=symbol,
            start_balance=self.start_balance,
            end_equity=cash,
            total_return_pct=(cash / self.start_balance - 1) * 100.0,
            buy_hold_return_pct=(close[end - 1] / close[start - 1] - 1) * 100.0,
            max_drawdown_pct=abs(drawdown),
            n_trades=len(trades),
            win_rate_pct=(len(wins) / len(trades) * 100.0) if trades else 0.0,
            profit_factor=profit_factor,
            total_fees=total_fees,
            trades=trades,
            equity_curve=curve,
            in_position=in_pos,
        )
