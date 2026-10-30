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


def test_ai_stop_floor_from_atr_and_costs_and_min_rr():
    from tradingbot.ai import AiDecision
    from tradingbot.config import AiConfig, StrategyConfig
    from tradingbot.strategy.claude_advisor import ClaudeAdvisorStrategy
    from tests.conftest import make_ohlcv, trend_closes

    s = ClaudeAdvisorStrategy(StrategyConfig(name="claude_advisor", allow_short=True),
                              AiConfig(min_stop_atr=0.0, min_stop_cost_mult=3.0, min_rr=1.5), client=object())
    s.cost_pct = 0.22
    df = s.prepare(make_ohlcv(trend_closes()))
    close = float(df["close"].iloc[-1])
    prompts = []
    s.advisor.ask = lambda p: prompts.append(p) or AiDecision("short", 0.9, "x", stop_loss=close * 1.004, take_profit=close * 0.997)
    d = s.decide(df, context={"symbol": "BTC", "position": None})
    assert abs(d.stop_loss / close - 1.0066) < 1e-9, "0,40 % zu eng: auf 3 x 0,22 % gesetzt"
    assert abs(1 - d.take_profit / close - 1.5 * 0.0066) < 1e-9, "Ziel mindestens 1,5 x Stop"
    assert "Stop mindestens 0.66 %" in prompts[0]
    s.advisor.ask = lambda p: AiDecision("buy", 0.9, "x", stop_loss=close * 0.98, take_profit=close * 1.05)
    d = s.decide(df, context={"symbol": "BTC", "position": None})
    assert abs(d.stop_loss - close * 0.98) < 1e-9 and abs(d.take_profit - close * 1.05) < 1e-9, "weite Werte bleiben"


def test_performance_brake_per_timeframe_and_side_with_probe():
    now = datetime.now(timezone.utc)

    def t(side, pnl, h):
        return (trade("BTC/USDT", side, (now - timedelta(hours=h + 0.2)).isoformat(), (now - timedelta(hours=h)).isoformat(), pnl), "1m")

    hist = [t("short", -0.5, 1 + i) for i in range(10)] + [t("long", 0.4, 2), (trade("ETH/USDT", "short", iso(0), iso(5), 1.0), "1h")]
    bots = [bot()]
    g = PortfolioGuard(lambda: bots, 0, 0, trades=lambda: hist, perf_window=15, perf_min_trades=8, perf_probe_hours=12)
    blocked, why = g.performance("1m", "short")
    assert blocked and "1m-Shorts gebremst" in why and "0 Gewinner" in why
    assert not g.performance("1m", "long")[0] and not g.performance("1h", "short")[0], "zu wenig Trades / andere Richtung"
    assert not g("XRP/USDT", "short", "1m")[0] and g("XRP/USDT", "long", "1m")[0]
    assert g.precheck("XRP/USDT", "1m")[0], "Long noch moeglich: KI fragen"
    hist += [t("long", -0.6, 1.5 + i * 0.01) for i in range(9)]
    ok, why2 = g.precheck("XRP/USDT", "1m")
    assert not ok and "Longs" in why2 and "Shorts" in why2, "beide Richtungen gebremst: KI nicht fragen"
    old = [t("short", -0.5, 13 + i) for i in range(10)]
    g2 = PortfolioGuard(lambda: bots, 0, 0, trades=lambda: old, perf_window=15, perf_min_trades=8, perf_probe_hours=12)
    assert not g2.performance("1m", "short")[0], "nach 12 h ohne Trade ein Probe-Trade"


def test_stop_checked_between_slow_ai_symbols(settings, tmp_path):
    from tests.conftest import FakeFeed, make_ohlcv, trend_closes
    from tests.test_bot import _make_bot

    b, _, _ = _make_bot(settings, FakeFeed(make_ohlcv(trend_closes())), tmp_path)
    price = b.feed.fetch_price("BTC/USDT")
    b.broker.open_position("BTC/USDT", "long", 0.01)
    b.state.positions["BTC/USDT"] = Position("BTC/USDT", 0.01, price * 1.1, price * 1.05, None, iso(0), price * 1.1)
    b._check_exits({})
    assert "BTC/USDT" not in b.state.positions and b.state.trades[-1].reason == "stop_loss"
