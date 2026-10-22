from __future__ import annotations

from datetime import datetime, timezone

from .state import BotState

MAX_CHART_POINTS = 500
MAX_TRADES = 100


def compute_stats(trades: list) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.pnl_quote > 0]
    gross_profit = sum(t.pnl_quote for t in wins)
    gross_loss = -sum(t.pnl_quote for t in trades if t.pnl_quote <= 0)
    return {
        "n_trades": n,
        "wins": len(wins),
        "win_rate_pct": (len(wins) / n * 100.0) if n else None,
        "total_pnl": sum(t.pnl_quote for t in trades),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "best": max((t.pnl_quote for t in trades), default=None),
        "worst": min((t.pnl_quote for t in trades), default=None),
        "fees": sum(t.fees for t in trades),
    }


def downsample(points: list, limit: int = MAX_CHART_POINTS) -> list:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit)]
    picked[-1] = points[-1]
    return picked


def make_snapshot(
    *,
    state: BotState,
    mode: str,
    exchange: str,
    timeframe: str,
    quote: str,
    symbols: list[str],
    equity: float | None,
    free_quote: float | None,
    prices: dict[str, float],
    last_cycle: str | None,
    running: bool,
    controls: bool,
    ai_meta: dict | None = None,
) -> dict:
    history = list(state.equity_history)
    if equity is None and history:
        equity = float(history[-1][1])
    start_equity = float(history[0][1]) if history else None
    return_pct = None
    if equity is not None and start_equity:
        return_pct = (equity / start_equity - 1.0) * 100.0
    day_pnl_pct = None
    if equity is not None and state.day_start_equity:
        day_pnl_pct = (equity / state.day_start_equity - 1.0) * 100.0

    positions = []
    for symbol, p in list(state.positions.items()):
        price = prices.get(symbol)
        pnl = pnl_pct = None
        if price is not None:
            pnl = p.unrealized(price) - p.entry_fee
            invested = p.entry_price * p.amount
            pnl_pct = pnl / invested * 100.0 if invested else None
        # Groesse: Nennwert zum aktuellen Kurs, gebundene Sicherheit und Verlust bis zum Stop
        notional = p.amount * (price if price is not None else p.entry_price)
        risk_quote = abs(p.entry_price - p.stop_loss) * p.amount
        positions.append(
            {
                "symbol": symbol,
                "side": p.side,
                "amount": p.amount,
                "notional": notional,
                "size_pct": notional / equity * 100.0 if equity else None,
                "margin": p.entry_price * p.amount / p.leverage,
                "leverage": p.leverage,
                "risk_quote": risk_quote,
                "risk_pct": risk_quote / equity * 100.0 if equity else None,
                "entry_price": p.entry_price,
                "price": price,
                "stop_loss": p.stop_loss,
                "stop_on_exchange": bool(p.stop_order_id),
                "take_profit": p.take_profit,
                "pnl_quote": pnl,
                "pnl_pct": pnl_pct,
                "opened_at": p.opened_at,
            }
        )

    ai = None
    ai_state = (state.strategy_state or {}).get("ai")
    if ai_meta is not None:
        ai_state = ai_state or {}
        ai = {
            **ai_meta,
            "calls_today": ai_state.get("calls", 0),
            "decisions": list(reversed(ai_state.get("decisions", [])[-15:])),
        }

    trades = list(state.trades)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "exchange": exchange,
        "timeframe": timeframe,
        "quote": quote,
        "symbols": list(symbols),
        "running": running,
        "controls": controls,
        "paused": state.entries_paused,
        "halted": state.entries_halted_for_day,
        "last_cycle": last_cycle,
        "equity": equity,
        "free_quote": free_quote,
        "start_equity": start_equity,
        "return_pct": return_pct,
        "day_start_equity": state.day_start_equity,
        "day_pnl_pct": day_pnl_pct,
        "positions": positions,
        "trades": [t.to_dict() for t in reversed(trades[-MAX_TRADES:])],
        "stats": compute_stats(trades),
        "equity_history": downsample(history),
        "ai": ai,
    }
