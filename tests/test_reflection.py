from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tradingbot.models import Position, Trade
from tradingbot.reflection import PortfolioGuard, Reflector, chain_guards, describe, trade_facts


def trade(symbol, side, opened, closed, pnl, reason="stop_loss"):
    return Trade(symbol=symbol, amount=1, entry_price=100, exit_price=100 + pnl, pnl_quote=pnl, pnl_pct=pnl, fees=0.1,
                 opened_at=opened, closed_at=closed, reason=reason, side=side, initial_stop=99.5)


def iso(minutes):
    return (datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()


def test_facts_find_bundles_reentries_and_duplicates():
    ts = [
        (trade("BTC/USDT", "long", iso(0), iso(30), -0.5), "1h"),
        (trade("ETH/USDT", "long", iso(2), iso(31), -0.5), "1h"),
        (trade("SOL/USDT", "long", iso(5), iso(32), -0.6), "4h"),
        (trade("BTC/USDT", "long", iso(10), iso(40), -0.4), "4h"),   # BTC war im 1h schon offen
        (trade("SOL/USDT", "long", iso(50), iso(70), -0.5), "1m"),   # 18 min nach dem SOL-Stop
        (trade("XRP/USDT", "short", iso(200), iso(260), 1.2, "take_profit"), "15m"),
    ]
    facts = "\n".join(trade_facts(ts))
    assert "Gesamt: 6 Trades, 1 Gewinner" in facts
    assert "Short: 1 Trades, 1 Gewinner" in facts
    assert "Buendel" in facts and "4 Trades" in facts
    assert "Wiedereinstieg" in facts
    assert "anderen Zeitrahmen schon offen" in facts
    assert trade_facts([]) == []


def test_describe_adds_entry_reason_of_ai():
    t = trade("BTC/USDT", "long", iso(0), iso(30), -0.5)
    dec = [{"ts": iso(-0.5), "symbol": "BTC/USDT", "action": "buy", "applied": True, "reason": "Sweep unter Tief"},
           {"ts": iso(-30), "symbol": "BTC/USDT", "action": "buy", "applied": True, "reason": "alt"}]
    text = describe(t, "1m", dec)
    assert text.startswith("1m BTC long") and "Einstiegsgrund: Sweep unter Tief" in text and "Stop-Abstand 0.50 %" in text


def test_reflector_runs_after_enough_trades_and_keeps_lessons(tmp_path):
    trades = [(trade("BTC/USDT", "long", iso(i), iso(i + 5), -0.5), "1m") for i in range(4)]
    asked = []

    def ask(system, user):
        asked.append((system, user))
        return 'Gedanken ... {"summary": "Zu viele Longs gegen den Trend.", "lessons": ["Kein Long gegen den 1h-Trend", " "]}'

    r = Reflector(ask, lambda: trades, lambda: [], tmp_path / "ai_lessons.json", every=5)
    assert not r.due() and not r.maybe_start()
    trades.append((trade("ETH/USDT", "long", iso(9), iso(12), -0.5), "1m"))
    assert r.due() and r.run()
    assert r.lessons == ["Kein Long gegen den 1h-Trend"]
    assert "hoechstens 6 Regeln" in asked[0][0] and "Buendel" not in asked[0][0] and "Kennzahlen" in asked[0][1]
    assert not r.due() and r.snapshot()["next_in"] == 5
    again = Reflector(ask, lambda: trades, lambda: [], tmp_path / "ai_lessons.json", every=5)
    assert again.lessons == ["Kein Long gegen den 1h-Trend"], "Lehren bleiben ueber einen Neustart erhalten"
    trades += [(trade("SOL/USDT", "long", iso(20 + i), iso(25 + i), -0.5), "1m") for i in range(5)]
    again.run()
    assert "Deine bisherigen Regeln" in asked[-1][1]


def test_reflector_ignores_unreadable_answer(tmp_path):
    trades = [(trade("BTC/USDT", "long", iso(i), iso(i + 5), -0.5), "1m") for i in range(5)]
    r = Reflector(lambda s, u: "keine Ahnung", lambda: trades, lambda: [], tmp_path / "l.json", every=5)
    assert not r.run() and r.lessons == [] and not r.due(), "nicht in jeder Runde neu fragen"
    assert "ohne Regeln" in r.snapshot()["error"]
    r._retry_at = 0
    assert r.due(), "spaeter erneut versuchen"


def test_workers_ai_uses_lessons_schema():
    from tradingbot.ai import LESSONS_SCHEMA, WorkersAiClient
    from tradingbot.config import AiConfig

    sent = []
    http = SimpleNamespace(post=lambda url, json, timeout: sent.append(json) or SimpleNamespace(
        status_code=200, json=lambda: {"result": {"response": {"summary": "s", "lessons": ["a"]}}}))
    c = WorkersAiClient(AiConfig(), url="http://ai", session=http)
    assert c.complete("sys", "u", LESSONS_SCHEMA) == {"summary": "s", "lessons": ["a"]}
    assert sent[0]["response_format"]["json_schema"] is LESSONS_SCHEMA
    c.complete("sys", "u")
    assert "action" in sent[1]["response_format"]["json_schema"]["properties"]


def pos(symbol, side):
    return Position(symbol=symbol, amount=1, entry_price=100, stop_loss=99, take_profit=None, opened_at=iso(0),
                    highest_price=100, side=side)


def bot(positions=(), trades=()):
    return SimpleNamespace(state=SimpleNamespace(positions={p.symbol: p for p in positions}, trades=list(trades)))


def test_portfolio_guard_blocks_duplicates_bundles_and_quick_reentry():
    bots = [bot([pos("BTC/USDT", "long"), pos("ETH/USDT", "long")]), bot([pos("SOL/USDT", "long")])]
    g = PortfolioGuard(lambda: bots, max_same_direction=3, reentry_minutes=60)
    ok, why = g("BTC/USDT", "short")
    assert not ok and "schon offen" in why
    ok, why = g("XRP/USDT", "long")
    assert not ok and "Grenze 3" in why
    assert g("XRP/USDT", "short")[0]
    now = datetime.now(timezone.utc)
    recent = trade("ADA/USDT", "short", (now - timedelta(minutes=40)).isoformat(), (now - timedelta(minutes=10)).isoformat(), -0.5)
    bots.append(bot(trades=[recent]))
    ok, why = g("ADA/USDT", "short")
    assert not ok and "Wiedereinstieg" in why
    assert PortfolioGuard(lambda: bots, 0, 60)("ADA/USDT", "long")[0], "Gegenrichtung nach Stop erlaubt"
    assert PortfolioGuard(lambda: bots, 0, 0)("XRP/USDT", "long")[0], "0 = aus"


def test_chain_guards_first_refusal_wins():
    g = chain_guards(None, lambda s, d: (True, ""), lambda s, d: (False, "News"), lambda s, d: (False, "zweite"))
    assert g("BTC/USDT", "long") == (False, "News")
    assert chain_guards()("BTC/USDT", "long") == (True, "")


def test_multibot_learning_shares_memory_lessons_and_guard(settings, tmp_path):
    from tradingbot.ai import AiDecision
    from tradingbot.config import AiConfig, StrategyConfig
    from tradingbot.multi_tf import MultiBot, settings_for
    from tradingbot.strategy.claude_advisor import ClaudeAdvisorStrategy
    from tests.conftest import FakeFeed, make_ohlcv, trend_closes
    from tests.test_bot import _make_bot

    prompts, bots = [], {}
    for tf in ("1h", "4h"):
        d = tmp_path / tf
        d.mkdir()
        b, _, _ = _make_bot(settings_for(settings, tf), FakeFeed(make_ohlcv(trend_closes())), d)
        b.strategy = ClaudeAdvisorStrategy(StrategyConfig(name="claude_advisor", allow_short=True), AiConfig(gate_timeframes=[]),
                                           client=object(), timeframe=tf)
        b.strategy.advisor.ask = lambda p: prompts.append(p) or AiDecision("buy", 0.9, "Trend")
        b.strategy.advisor.complete_text = lambda s, u, schema=None: '{"summary": "x", "lessons": ["Keine Buendel"]}'
        bots[tf] = b
    m = MultiBot(bots, "1h")
    m.enable_learning()
    bots["4h"].state.trades.append(trade("ETH/USDT", "long", iso(0), iso(30), -0.5))
    assert m.reflector.run() and m.reflector.lessons == ["Keine Buendel"]
    bots["1h"].run_once()
    assert "BTC/USDT" in bots["1h"].state.positions
    assert "Keine Buendel" in prompts[0] and "4h ETH long" in prompts[0], "Lehren und Trades aller Zeitrahmen im Prompt"
    bots["4h"].run_once()
    assert "BTC/USDT" not in bots["4h"].state.positions, "dasselbe Paar nicht in zwei Zeitrahmen"
    assert m.get_snapshot()["lessons"]["lessons"] == ["Keine Buendel"]
