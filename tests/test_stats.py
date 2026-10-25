from datetime import datetime, timezone

import pytest

from tradingbot.models import Trade
from tradingbot.stats import analyze, max_drawdown


def _t(sym, side, pnl, opened, minutes, reason="stop_loss"):
    o = datetime.fromisoformat(opened).replace(tzinfo=timezone.utc)
    c = o.replace(minute=o.minute) if minutes == 0 else datetime.fromtimestamp(o.timestamp() + minutes * 60, timezone.utc)
    return Trade(sym, 1.0, 100.0, 100.0 + pnl, pnl, pnl, 0.1, o.isoformat(), c.isoformat(), reason, side)


def test_analyze_core_figures():
    trades = [
        _t("BTC", "long", 3.0, "2026-10-19T08:00:00", 10, "take_profit"),   # Mo 10:00 Berlin
        _t("BTC", "short", -1.0, "2026-10-19T09:00:00", 20),
        _t("ETH", "long", -2.0, "2026-10-20T13:30:00", 30),
        _t("ETH", "long", 4.0, "2026-10-21T13:30:00", 40, "signal:short"),
    ]
    a = analyze(trades, [["x", 1000], ["x", 1010], ["x", 990], ["x", 1004]], now=datetime(2026, 10, 21, 20, tzinfo=timezone.utc))
    assert a["n"] == 4 and a["wins"] == 2 and a["win_rate_pct"] == 50
    assert a["total_pnl"] == pytest.approx(4.0) and a["pnl_before_fees"] == pytest.approx(4.4)
    assert a["profit_factor"] == pytest.approx(7 / 3) and a["payoff"] == pytest.approx(3.5 / 1.5)
    assert a["expectancy"] == pytest.approx(1.0)
    assert (a["best_streak"], a["worst_streak"], a["current_streak"]) == (1, 2, 1)
    assert a["hold_avg_min"] == pytest.approx(25) and a["hold_win_min"] == pytest.approx(25) and a["hold_loss_min"] == pytest.approx(25)
    assert a["by_side"]["short"]["n"] == 1 and a["by_side"]["long"]["pnl"] == pytest.approx(5.0)
    assert [g["symbol"] for g in a["by_symbol"]] == ["BTC", "ETH"]
    assert {g["reason"] for g in a["by_reason"]} == {"stop_loss", "take_profit", "signal"}
    assert a["by_hour"][10]["n"] == 1 and a["by_hour"][15]["n"] == 2, "Stunden in Berliner Zeit"
    assert a["by_weekday"][0]["n"] == 2
    assert len(a["daily"]) == 14 and a["daily"][-1]["pnl"] == pytest.approx(4.0)
    assert a["max_drawdown_pct"] == pytest.approx(20 / 1010 * 100)
    assert a["days_green"] == 2 and a["days_red"] == 1


def test_empty_and_drawdown():
    a = analyze([], [])
    assert a["n"] == 0 and a["win_rate_pct"] is None and a["max_drawdown_pct"] is None
    assert max_drawdown([["a", 100], ["b", 120], ["c", 90]]) == (pytest.approx(25.0), pytest.approx(30.0))
