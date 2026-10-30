from types import SimpleNamespace

from tradingbot.ai import AiDecision, ClaudeAdvisor, OpenRouterClient
from tradingbot.ai_bot import AiRunner, ai_settings, build_ai_bot
from tradingbot.config import AiConfig, StrategyConfig
from tradingbot.models import Signal
from tradingbot.strategy.claude_advisor import ClaudeAdvisorStrategy
from tests.conftest import FakeFeed, make_ohlcv, trend_closes


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, data):
        self.headers, self.data, self.bodies = {}, data, []

    def post(self, url, json=None, timeout=None):
        self.bodies.append((url, json))
        return FakeResp(200, self.data)


def test_openrouter_client_sends_fallbacks_and_reads_answer():
    sess = FakeSession({"choices": [{"message": {"content": 'Sicher: {"action": "short", "confidence": 0.7, "reason": "Abwaertstrend"}'}}]})
    cfg = AiConfig(provider="openrouter", model="a:free", fallback_models=["b:free"])
    client = OpenRouterClient(cfg, "key", session=sess)
    text = client.complete("sys", "user")
    url, body = sess.bodies[0]
    assert "openrouter.ai" in url and body["models"] == ["a:free", "b:free"] and sess.headers["Authorization"] == "Bearer key"
    d = ClaudeAdvisor.parse_text(text)
    assert (d.action, d.confidence, d.ok) == ("short", 0.7, True)
    assert ClaudeAdvisor.parse_text({"action": "hold", "confidence": 0.2, "reason": "x"}).action == "hold"
    assert not ClaudeAdvisor.parse_text("kein json").ok


def test_advisor_opens_and_closes_shorts():
    answers = iter([AiDecision("short", 0.8, "runter"), AiDecision("cover", 0.7, "dreht")])
    s = ClaudeAdvisorStrategy(StrategyConfig(name="claude_advisor", allow_short=True), AiConfig(), client=object())
    s.advisor.ask = lambda prompt: next(answers)
    df = s.prepare(make_ohlcv(trend_closes()))
    assert s.decide(df, context={"symbol": "BTC", "position": None}).signal == Signal.SHORT
    pos = {"side": "short", "amount": 1, "entry_price": 1, "stop_loss": 2, "pnl_pct": 0.1}
    assert s.decide(df, context={"symbol": "BTC", "position": pos}).signal == Signal.COVER


def test_ai_bot_has_own_account_and_settings(settings, tmp_path):
    settings.bot.state_dir = str(tmp_path)
    settings.bot.ai_bot = settings.bot.ai_bot.model_copy(update={"enabled": True, "provider": "workers_ai"})
    s = ai_settings(settings)
    assert s.bot.strategy.name == "claude_advisor" and s.bot.strategy.allow_short and s.bot.timeframe == "15m"
    assert s.bot.protections.track_record_trades == 0 and not s.bot.strategy.realtime_signals
    bot = build_ai_bot(settings, feed=FakeFeed(make_ohlcv(trend_closes())), client=SimpleNamespace())
    assert str(bot.state_path).endswith("state_paper_ai.json")
    runner = AiRunner(settings.model_copy(update={"secrets": settings.secrets.model_copy(update={"openrouter_api_key": ""})}))
    runner.cfg = runner.cfg.model_copy(update={"enabled": True, "provider": "openrouter"})
    runner.start()
    assert runner.problem == "OPENROUTER_API_KEY fehlt" and runner.snapshot()["ready"] is False


def test_ai_sets_stop_and_target_and_adjusts_open_position(settings, tmp_path):
    from tradingbot.models import Decision, Position
    from tests.test_bot import _make_bot

    s = ClaudeAdvisorStrategy(StrategyConfig(name="claude_advisor", allow_short=True, stop_atr_mult=2.0, take_profit_atr_mult=4.0),
                              AiConfig(min_stop_atr=0, min_stop_cost_mult=0, min_rr=0), client=object())
    df = s.prepare(make_ohlcv(trend_closes()))
    close = float(df["close"].iloc[-1])
    s.advisor.ask = lambda p: AiDecision("buy", 0.8, "Sweep", stop_loss=close * 0.99, take_profit=close * 1.03)
    d = s.decide(df, context={"symbol": "BTC", "position": None})
    assert d.signal == Signal.BUY and abs(d.stop_loss - close * 0.99) < 1e-9 and abs(d.take_profit - close * 1.03) < 1e-9
    assert s.entry_plan(df, len(df) - 1, "long", close, 1.0) == (d.stop_loss, d.take_profit)
    s.advisor.ask = lambda p: AiDecision("buy", 0.8, "x", stop_loss=close * 1.01, take_profit=close * 1.03)
    assert s.decide(df, context={"symbol": "BTC", "position": None}).stop_loss < close, "Stop falsch: eigener Plan"

    bot, _, _ = _make_bot(settings, FakeFeed(make_ohlcv(trend_closes())), tmp_path)
    pos = Position("BTC/USDT", 1.0, 100.0, 95.0, 110.0, "2026-10-26T00:00:00+00:00", 100.0, 0.0, 1.0)
    bot._adjust_levels("BTC/USDT", pos, 102.0, Decision(Signal.HOLD, "x", stop_loss=90.0, take_profit=115.0, adjust=True))
    assert pos.stop_loss == 95.0 and pos.take_profit == 115.0, "Stop nie weiter weg"
    bot._adjust_levels("BTC/USDT", pos, 102.0, Decision(Signal.HOLD, "x", stop_loss=99.0, take_profit=None, adjust=True))
    assert pos.stop_loss == 99.0 and pos.stop_kind == "strategy"


def test_liquidity_zones_orderbook_and_positioning():
    from tradingbot.insights import LiquidityContext, liquidity_zones, orderbook_liquidity

    df = make_ohlcv(trend_closes())
    price = float(df["close"].iloc[-1])
    assert liquidity_zones(df, price)
    ex = SimpleNamespace(
        fetch_order_book=lambda s, limit: {"bids": [[price * 0.998, 10.0], [price * 0.99, 50.0]], "asks": [[price * 1.002, 2.0], [price * 1.02, 1.0]]},
        fetch_funding_rate=lambda s: {"fundingRate": 0.0001},
        fetch_open_interest_history=lambda s, tf, limit: [{"openInterestValue": 1e9}, {"openInterestValue": 1.1e9}],
    )
    ob, mid = orderbook_liquidity(ex, "BTC/USDT:USDT", price)
    assert abs(mid - price) / price < 0.01 and "mehr Kaeufer" in ob[0] and any("Kauf-Waende" in x for x in ob)
    ctx = LiquidityContext(ex)("BTC/USDT:USDT", df)
    assert any("Funding" in x for x in ctx) and any("Open Interest" in x and "+10.00 %" in x for x in ctx)


def test_openrouter_limit_switches_to_backup_for_the_day(monkeypatch):
    monkeypatch.setenv("TRADINGBOT_AI_URL", "http://ai.tradingbot/")

    class LimitedSession(FakeSession):
        def post(self, url, json=None, timeout=None):
            self.bodies.append((url, json))
            return FakeResp(429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}})

    cfg = AiConfig(provider="openrouter", model="a:free", backup_model="@cf/google/gemma-4-26b-a4b-it")
    sess = LimitedSession({})
    adv = ClaudeAdvisor(cfg, client=OpenRouterClient(cfg, "key", session=sess))
    adv.backup.complete = lambda system, user: '{"action": "hold", "confidence": 0.5, "reason": "ruhig", "stop_loss": null, "take_profit": null}'
    d1, d2 = adv.ask("p"), adv.ask("p")
    assert d1.ok and d1.reason.startswith("[Ersatzmodell]") and d2.reason.startswith("[Ersatzmodell]")
    assert len(sess.bodies) == 1, "nach dem Limit nicht mehr bei OpenRouter fragen"


def test_workers_ai_reads_both_answer_formats():
    from tradingbot.ai import WorkersAiClient

    cfg = AiConfig(provider="workers_ai", model="@cf/x")
    for data in ({"result": {"response": '{"action":"hold"}'}},
                 {"result": {"choices": [{"message": {"content": '{"action":"hold"}'}}]}}):
        assert WorkersAiClient(cfg, url="http://ai/", session=FakeSession(data)).complete("s", "u") == '{"action":"hold"}'


def test_gate_asks_ai_only_on_setups_and_memory_in_prompt():
    from tradingbot.ai import build_user_prompt

    calls = []
    s = ClaudeAdvisorStrategy(StrategyConfig(name="claude_advisor", allow_short=True), AiConfig(gate_lookback=20, gate_volume_min=2.0),
                              client=object(), timeframe="1m")
    s.advisor.ask = lambda p: calls.append(p) or AiDecision("hold", 0.5, "ok")
    df = make_ohlcv(trend_closes())
    df["timestamp"] = [1_790_000_000_000 + i * 60_000 for i in range(len(df))]
    p = s.prepare(df)
    assert s.gated and s.warmup_bars >= 3300
    p.loc[p.index[-1], "volume"] = 0.0  # kein Volumen: kein Setup
    assert "Vorauswahl" in s.decide(p, context={"symbol": "BTC", "position": None}).reason and not calls
    q = p.copy()
    lo = float(q["low"].iloc[-21:-1].min())
    q.loc[q.index[-1], ["low", "close", "volume"]] = [lo * 0.99, lo * 1.001, float(q["volume"].iloc[-21:-1].mean()) * 5]
    assert s.setup(q)[1] == "long"
    s.decide(q, context={"symbol": "BTC", "position": None, "recent_trades": ["BTC long: -0,50 % Stop"]})
    assert calls and "Liquiditaets-Sweep" in calls[0] and "Deine letzten abgeschlossenen Trades" in calls[0]
    text = build_user_prompt("BTC", "1m", "USDT", p, {"cost_pct": 0.22}, 30)
    assert "Ziel mindestens 0.66 %" in text
