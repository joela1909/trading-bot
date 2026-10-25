"""Auswertung der abgeschlossenen Trades fuer die Statistik-Seite der App."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DAILY_DAYS = 14


def _ts(iso: str) -> datetime:
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _group(trades: list) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_quote > 0)
    return {"n": n, "wins": wins, "win_rate_pct": wins / n * 100.0 if n else None, "pnl": sum(t.pnl_quote for t in trades)}


def _streaks(trades: list) -> tuple[int, int, int]:
    """Laengste Gewinn- und Verlustserie, dazu die aktuelle (positiv = Gewinne, negativ = Verluste)."""
    best = worst = cur = 0
    for t in trades:
        if t.pnl_quote > 0:
            cur = cur + 1 if cur > 0 else 1
        else:
            cur = cur - 1 if cur < 0 else -1
        best, worst = max(best, cur), min(worst, cur)
    return best, -worst, cur


def max_drawdown(history: list) -> tuple[float | None, float | None]:
    """Groesster Rueckgang vom Hoechststand im Kapitalverlauf: (Prozent, Betrag)."""
    peak = None
    dd_pct = dd_abs = 0.0
    for _, eq in history:
        eq = float(eq)
        peak = eq if peak is None else max(peak, eq)
        if peak > 0:
            dd_abs = max(dd_abs, peak - eq)
            dd_pct = max(dd_pct, (peak - eq) / peak * 100.0)
    return (dd_pct, dd_abs) if history else (None, None)


def _stop_groups(trades: list) -> dict:
    g: dict = defaultdict(list)
    for t in trades:
        if t.reason.startswith("stop_loss"):
            g[getattr(t, "stop_kind", "") or "unbekannt"].append(t)
    return g


def _lev_groups(trades: list) -> dict:
    g: dict = defaultdict(list)
    for t in trades:
        g[getattr(t, "leverage", None)].append(t)
    return g


def analyze(trades: list, equity_history: list | None = None, tz: str = "Europe/Berlin", now: datetime | None = None) -> dict:
    """Alles, was an den Trades interessant ist. trades: aelteste zuerst."""
    zone = ZoneInfo(tz)
    n = len(trades)
    wins = [t for t in trades if t.pnl_quote > 0]
    losses = [t for t in trades if t.pnl_quote <= 0]
    total = sum(t.pnl_quote for t in trades)
    fees = sum(t.fees for t in trades)
    gross_profit = sum(t.pnl_quote for t in wins)
    gross_loss = -sum(t.pnl_quote for t in losses)
    avg_win = gross_profit / len(wins) if wins else None
    avg_loss = -gross_loss / len(losses) if losses else None
    best_streak, worst_streak, current = _streaks(trades)

    holds = {"all": [], "win": [], "loss": []}
    for t in trades:
        minutes = (_ts(t.closed_at) - _ts(t.opened_at)).total_seconds() / 60.0
        holds["all"].append(minutes)
        holds["win" if t.pnl_quote > 0 else "loss"].append(minutes)

    def avg(xs: list) -> float | None:
        return sum(xs) / len(xs) if xs else None

    by_side = {side: _group([t for t in trades if t.side == side]) for side in ("long", "short")}
    sym_groups: dict = defaultdict(list)
    strat_groups: dict = defaultdict(list)
    reason_groups: dict = defaultdict(list)
    hours = [[] for _ in range(24)]
    weekdays = [[] for _ in range(7)]
    days: dict = defaultdict(list)
    for t in trades:
        sym_groups[t.symbol].append(t)
        strat_groups[getattr(t, "strategy", "") or "unbekannt"].append(t)
        reason = t.reason.split(":")[0] if t.reason.startswith("signal") else t.reason
        reason_groups[reason].append(t)
        opened = _ts(t.opened_at).astimezone(zone)
        hours[opened.hour].append(t)
        weekdays[opened.weekday()].append(t)
        days[_ts(t.closed_at).astimezone(zone).date()].append(t)

    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    daily = []
    for k in range(DAILY_DAYS - 1, -1, -1):
        d = today - timedelta(days=k)
        g = _group(days.get(d, []))
        daily.append({"date": d.isoformat(), "n": g["n"], "pnl": g["pnl"]})
    active_days = [sum(t.pnl_quote for t in ts) for ts in days.values()]
    first_day = min(days) if days else None
    span_days = (today - first_day).days + 1 if first_day else 0

    dd_pct, dd_abs = max_drawdown(equity_history or [])
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / n * 100.0 if n else None,
        "total_pnl": total,
        "pnl_before_fees": total + fees,
        "fees": fees,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "expectancy": total / n if n else None,
        "expectancy_pct": sum(t.pnl_pct for t in trades) / n if n else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": avg_win / -avg_loss if avg_win is not None and avg_loss else None,
        "best": max((t.pnl_quote for t in trades), default=None),
        "worst": min((t.pnl_quote for t in trades), default=None),
        "best_streak": best_streak,
        "worst_streak": worst_streak,
        "current_streak": current,
        "hold_avg_min": avg(holds["all"]),
        "hold_median_min": median(holds["all"]) if holds["all"] else None,
        "hold_win_min": avg(holds["win"]),
        "hold_loss_min": avg(holds["loss"]),
        "max_drawdown_pct": dd_pct,
        "max_drawdown_abs": dd_abs,
        "trades_per_day": n / span_days if span_days else None,
        "days_green": sum(1 for x in active_days if x > 0),
        "days_red": sum(1 for x in active_days if x <= 0),
        "by_side": by_side,
        "by_symbol": sorted(({"symbol": s, **_group(ts)} for s, ts in sym_groups.items()), key=lambda g: g["pnl"], reverse=True),
        "by_stop_kind": sorted(({"kind": k, **_group(ts)} for k, ts in _stop_groups(trades).items()), key=lambda g: g["n"], reverse=True),
        "by_leverage": sorted(({"leverage": k, **_group(ts)} for k, ts in _lev_groups(trades).items()), key=lambda g: (g["leverage"] is None, g["leverage"] or 0)),
        "by_strategy": sorted(({"strategy": s, **_group(ts)} for s, ts in strat_groups.items()), key=lambda g: g["pnl"], reverse=True),
        "by_reason": sorted(({"reason": r, **_group(ts)} for r, ts in reason_groups.items()), key=lambda g: g["n"], reverse=True),
        "by_hour": [{"hour": h, **_group(ts)} for h, ts in enumerate(hours)],
        "by_weekday": [{"day": WEEKDAYS[i], **_group(ts)} for i, ts in enumerate(weekdays)],
        "daily": daily,
        "timezone": tz,
    }
