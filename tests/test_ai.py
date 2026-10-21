import json
from types import SimpleNamespace

import anthropic
import pytest

from tradingbot.ai import ClaudeAdvisor, DECISION_SCHEMA, build_user_prompt
from tradingbot.bot import TradingBot
from tradingbot.cli import main
from tradingbot.config import AiConfig, BotConfig, Secrets, Settings, StrategyConfig
from tradingbot.exchange import PaperBroker
from tradingbot.models import Signal
from tradingbot.notify import Notifier
from tradingbot.risk import RiskManager
from tradingbot.state import BotState
from tradingbot.strategy import LIVE_ONLY, REGISTRY, build_strategy
from tests.conftest import FakeFeed, make_ohlcv, trend_closes


def reply(action="buy", confidence=0.8, reason="Trend intakt", stop_reason="end_turn", text=None):
    body = text if text is not None else json.dumps({"action": action, "confidence": confidence, "reason": reason})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)], stop_reason=stop_reason)


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, Exception):
            raise item
        return item


def _cfg(**kw):
    return StrategyConfig(name="claude_advisor", fast_ema=5, slow_ema=10, rsi_period=5, atr_period=5, **kw)


def _df(n=80):
    return make_ohlcv(trend_closes()[:n])


def _strategy(client, ai=None, **kw):
    return build_strategy(_cfg(**kw), ai=ai or AiConfig(), client=client)


def _ctx(position=None):
    return {"symbol": "BTC/USDT", "quote": "USDT", "timeframe": "1h", "equity": 1000.0, "free_quote": 900.0, "open_positions": 0, "position": position}


POSITION = {"amount": 0.1, "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0, "pnl_pct": 2.0}


def test_request_uses_current_api_shape():
    client = FakeClient(reply("hold", 0.9))
    strat = _strategy(client)
    df = strat.prepare(_df())
    strat.decide(df, context=_ctx())
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": DECISION_SCHEMA}
    for removed in ("temperature", "top_p", "top_k", "thinking", "tool_choice", "budget_tokens"):
        assert removed not in call
    assert call["max_tokens"] >= 2000 and call["system"]
    prompt = call["messages"][0]["content"]
    assert "BTC/USDT" in prompt and "Keine Position offen" in prompt and "Letzte 48 Kerzen" in prompt


def test_prompt_describes_open_position():
    strat = _strategy(FakeClient(reply()))
    df = strat.prepare(_df())
    text = build_user_prompt("BTC/USDT", "1h", "USDT", df, _ctx(POSITION), 20)
    assert "LONG offen" in text and "Stop-Loss 95" in text and "Take-Profit 110" in text


@pytest.mark.parametrize(
    "response,ok,action",
    [
        (reply("buy", 0.7), True, "buy"),
        (reply("SELL", 0.5), True, "sell"),
        (reply("buy", 1.7), True, "buy"),
        (reply(text="kein json"), False, "hold"),
        (reply(text='{"action": "moon", "confidence": 1, "reason": "x"}'), False, "hold"),
        (reply(text='{"action": "buy"}'), False, "hold"),
        (reply(stop_reason="refusal"), False, "hold"),
        (reply(stop_reason="max_tokens"), False, "hold"),
        (SimpleNamespace(content=[], stop_reason="end_turn"), False, "hold"),
    ],
)
def test_parse_is_strict_and_fails_safe(response, ok, action):
    d = ClaudeAdvisor.parse(response)
    assert d.ok is ok and d.action == action
    assert 0.0 <= d.confidence <= 1.0


@pytest.mark.parametrize("exc", [anthropic.APIConnectionError(request=SimpleNamespace()), anthropic.APITimeoutError(request=SimpleNamespace())])
def test_api_errors_mean_hold(exc):
    strat = _strategy(FakeClient(exc))
    d = strat.decide(strat.prepare(_df()), context=_ctx())
    assert d.signal == Signal.HOLD
    assert strat.advisor.decisions[-1]["applied"] is False


def test_buy_needs_confidence_and_flat_position():
    df_strat = _strategy(FakeClient(reply("buy", 0.9)))
    df = df_strat.prepare(_df())
    d = df_strat.decide(df, context=_ctx())
    close = float(df["close"].iloc[-1])
    assert d.signal == Signal.BUY and d.stop_loss < close < d.take_profit and d.reason.startswith("KI:")

    low = _strategy(FakeClient(reply("buy", 0.4)))
    assert low.decide(low.prepare(_df()), context=_ctx()).signal == Signal.HOLD
    assert low.advisor.decisions[-1]["applied"] is False

    long_pos = _strategy(FakeClient(reply("buy", 0.99)))
    assert long_pos.decide(long_pos.prepare(_df()), context=_ctx(POSITION)).signal == Signal.HOLD


def test_sell_only_with_position_and_exit_threshold():
    s = _strategy(FakeClient(reply("sell", 0.55)))
    assert s.decide(s.prepare(_df()), context=_ctx(POSITION)).signal == Signal.SELL
    s = _strategy(FakeClient(reply("sell", 0.3)))
    assert s.decide(s.prepare(_df()), context=_ctx(POSITION)).signal == Signal.HOLD
    s = _strategy(FakeClient(reply("sell", 0.99)))
    assert s.decide(s.prepare(_df()), context=_ctx()).signal == Signal.HOLD


def test_daily_call_budget_blocks_further_calls():
    client = FakeClient(reply("hold", 0.9))
    strat = _strategy(client, ai=AiConfig(max_calls_per_day=2))
    df = strat.prepare(_df())
    for _ in range(4):
        d = strat.decide(df, context=_ctx())
    assert len(client.calls) == 2 and "Tageslimit" in d.reason


def test_state_roundtrip_keeps_counter_and_log():
    a = _strategy(FakeClient(reply("hold", 0.9)))
    a.decide(a.prepare(_df()), context=_ctx())
    b = _strategy(FakeClient(reply("hold", 0.9)))
    b.import_state(a.export_state())
    assert b.advisor.calls == 1 and len(b.advisor.decisions) == 1


def test_not_backtestable_and_not_in_compare_registry():
    assert "claude_advisor" in LIVE_ONLY and "claude_advisor" not in REGISTRY
    strat = _strategy(FakeClient(reply()))
    with pytest.raises(NotImplementedError):
        strat.signals(strat.prepare(_df()))


def test_cli_refuses_to_backtest_the_ai(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"log_dir: {tmp_path / 'logs'}\nstrategy:\n  name: claude_advisor\n")
    assert main(["--config", str(cfg), "backtest", "--csv", "x.csv"]) == 2
    assert main(["--config", str(cfg), "compare", "--demo", "trend", "--strategies", "claude_advisor"]) == 2
    assert "nicht backtestbar" in capsys.readouterr().err


def test_api_key_is_required_for_the_ai_strategy():
    s = Settings(bot=BotConfig(strategy=StrategyConfig(name="claude_advisor")), secrets=Secrets(_env_file=None))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        s.validate_for_run()
    s = Settings(bot=BotConfig(strategy=StrategyConfig(name="claude_advisor")), secrets=Secrets(_env_file=None, anthropic_api_key="k"))
    s.validate_for_run()


def test_bitget_live_requires_passphrase():
    live = dict(trading_mode="live", live_trading_confirmed=True, exchange_api_key="k", exchange_api_secret="s")
    with pytest.raises(RuntimeError, match="Passphrase"):
        Settings(bot=BotConfig(exchange="bitget"), secrets=Secrets(_env_file=None, **live)).validate_for_run()
    Settings(bot=BotConfig(exchange="bitget"), secrets=Secrets(_env_file=None, exchange_api_password="p", **live)).validate_for_run()
    Settings(bot=BotConfig(exchange="binance"), secrets=Secrets(_env_file=None, **live)).validate_for_run()


# ---------------------------------------------------------------- Bot-Einbindung
def _bot(settings, tmp_path, client, price_df):
    settings.bot.strategy = settings.bot.strategy.model_copy(update={"name": "claude_advisor"})
    strat = build_strategy(settings.bot.strategy, ai=settings.bot.ai, client=client)
    feed = FakeFeed(price_df)
    broker = PaperBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    bot = TradingBot(settings, feed, broker, strat, RiskManager(settings.bot.risk), BotState(), Notifier(), tmp_path / "state.json")
    return bot, feed


def test_bot_opens_position_on_ai_buy_and_shows_it_in_snapshot(settings, tmp_path):
    client = FakeClient(reply("buy", 0.8, "Aufwaertstrend"))
    bot, feed = _bot(settings, tmp_path, client, _df(70))
    bot.run_once()
    assert "BTC/USDT" in bot.state.positions
    snap = bot.get_snapshot()
    assert snap["ai"]["model"] == "claude-opus-5-5" and snap["ai"]["calls_today"] == 1
    assert snap["ai"]["decisions"][0]["action"] == "buy" and snap["ai"]["decisions"][0]["applied"] is True
    bot.run_once()
    assert len(client.calls) == 1, "gleiche Kerze, kein zweiter Aufruf"


def test_bot_asks_the_ai_about_exit_and_passes_position_context(settings, tmp_path):
    client = FakeClient(reply("buy", 0.8), reply("sell", 0.9, "Trend gedreht"))
    bot, feed = _bot(settings, tmp_path, client, _df(70))
    bot.run_once()
    longer = _df(71)
    feed.df = longer
    feed.price = float(longer["close"].iloc[-1])
    bot.run_once()
    assert not bot.state.positions and bot.state.trades[-1].reason.startswith("signal: KI:")
    assert "LONG offen" in client.calls[1]["messages"][0]["content"]


def test_cheap_gates_run_before_the_paid_ai_call(settings, tmp_path):
    client = FakeClient(reply("buy", 0.9))
    bot, _ = _bot(settings, tmp_path, client, _df(70))
    bot.pause()
    bot.run_once()
    assert client.calls == [] and not bot.state.positions
    bot.resume()
    bot.state.last_candle_ts.clear()
    bot.risk.cfg.max_open_positions = 0
    bot.run_once()
    assert client.calls == []


def test_ai_state_survives_restart(settings, tmp_path):
    client = FakeClient(reply("hold", 0.9))
    bot, _ = _bot(settings, tmp_path, client, _df(70))
    bot.run_once()
    saved = BotState.load(tmp_path / "state.json")
    assert saved.strategy_state["ai"]["calls"] == 1 and saved.strategy_state["ai"]["decisions"]


def test_normal_strategies_have_no_ai_block(settings, tmp_path):
    from tests.test_bot import _make_bot

    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    assert bot.get_snapshot()["ai"] is None
