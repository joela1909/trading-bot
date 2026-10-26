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
