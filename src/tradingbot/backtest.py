from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .config import PaperConfig, StrategyConfig
from .models import MarketLimits, Position, Signal, Trade
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
    def __init__(self, strategy: Strategy, risk: RiskManager, strategy_cfg: StrategyConfig, paper_cfg: PaperConfig, limits: MarketLimits | None = None):
        self.strategy = strategy
        self.risk = risk
        self.scfg = strategy_cfg
        self.start_balance = paper_cfg.starting_balance
        self.fee_rate = paper_cfg.fee_pct / 100.0
        self.slip = paper_cfg.slippage_pct / 100.0
        self.limits = limits or MarketLimits()

    def run(self, df: pd.DataFrame, symbol: str) -> BacktestResult:
        if len(df) < self.strategy.warmup_bars:
            raise ValueError(f"Zu wenig Kerzen ({len(df)}), mindestens {self.strategy.warmup_bars} noetig")
        df = self.strategy.prepare(df)
        cash = self.start_balance
        pos: Position | None = None
        trades: list[Trade] = []
        curve_ts: list[str] = []
        curve_eq: list[float] = []
        total_fees = 0.0

        def close(p: Position, price: float, ts: int, reason: str) -> None:
            nonlocal cash, total_fees
            proceeds = p.amount * price
            fee = proceeds * self.fee_rate
            cash += proceeds - fee
            fees = fee + p.entry_fee
            total_fees += fee
            pnl = (price - p.entry_price) * p.amount - fees
            trades.append(
                Trade(symbol, p.amount, p.entry_price, price, pnl, pnl / (p.entry_price * p.amount) * 100.0, fees, p.opened_at, _ts(ts), reason)
            )

        for i in range(1, len(df)):
            row = df.iloc[i]
            ts = int(row["timestamp"])
            if pos is not None:
                exit_price = None
                reason = ""
                if row["low"] <= pos.stop_loss:
                    exit_price, reason = pos.stop_loss * (1 - self.slip), "stop_loss"
                elif pos.take_profit is not None and row["high"] >= pos.take_profit:
                    exit_price, reason = pos.take_profit * (1 - self.slip), "take_profit"
                else:
                    decision = self.strategy.decide(df, i)
                    if decision.signal == Signal.SELL:
                        exit_price, reason = float(row["close"]) * (1 - self.slip), "signal"
                if exit_price is not None:
                    close(pos, exit_price, ts, reason)
                    pos = None
                elif self.scfg.trailing_stop and row["high"] > pos.highest_price:
                    pos.highest_price = float(row["high"])
                    atr_now = float(row["atr"])
                    if atr_now == atr_now and atr_now > 0:
                        pos.stop_loss = max(pos.stop_loss, self.strategy.trailing_stop(pos.highest_price, atr_now))
            else:
                decision = self.strategy.decide(df, i)
                if decision.signal == Signal.BUY and decision.atr:
                    entry = float(row["close"]) * (1 + self.slip)
                    stop = entry - self.scfg.stop_atr_mult * decision.atr
                    amount = self.risk.position_size(cash, cash, entry, stop, self.limits)
                    if amount > 0:
                        cost = amount * entry
                        fee = cost * self.fee_rate
                        cash -= cost + fee
                        total_fees += fee
                        tp = entry + self.scfg.take_profit_atr_mult * decision.atr if self.scfg.take_profit_atr_mult > 0 else None
                        pos = Position(symbol, amount, entry, stop, tp, _ts(ts), entry, fee, decision.atr)

            equity = cash + (pos.amount * float(row["close"]) if pos else 0.0)
            curve_ts.append(_ts(ts))
            curve_eq.append(equity)

        if pos is not None:
            last = df.iloc[-1]
            close(pos, float(last["close"]) * (1 - self.slip), int(last["timestamp"]), "end_of_data")
            curve_eq[-1] = cash
            pos = None

        curve = pd.Series(curve_eq, index=curve_ts, name="equity")
        running_max = curve.cummax()
        drawdown = ((curve - running_max) / running_max).min() * 100.0 if len(curve) else 0.0
        wins = [t for t in trades if t.pnl_quote > 0]
        gross_profit = sum(t.pnl_quote for t in wins)
        gross_loss = -sum(t.pnl_quote for t in trades if t.pnl_quote <= 0)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if not trades else float("inf"))
        first_close, last_close = float(df["close"].iloc[0]), float(df["close"].iloc[-1])

        return BacktestResult(
            symbol=symbol,
            start_balance=self.start_balance,
            end_equity=cash,
            total_return_pct=(cash / self.start_balance - 1) * 100.0,
            buy_hold_return_pct=(last_close / first_close - 1) * 100.0,
            max_drawdown_pct=abs(float(drawdown)),
            n_trades=len(trades),
            win_rate_pct=(len(wins) / len(trades) * 100.0) if trades else 0.0,
            profit_factor=profit_factor,
            total_fees=total_fees,
            trades=trades,
            equity_curve=curve,
        )
