import pytest

from tradingbot.bot import TradingBot
from tradingbot.exchange import PaperBroker
from tradingbot.models import Signal
from tradingbot.notify import Notifier
from tradingbot.risk import RiskManager
from tradingbot.state import BotState
from tradingbot.strategy import build_strategy
from tests.conftest import FakeFeed, make_ohlcv, trend_closes


class RecordingNotifier(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _first_buy_index(strategy, df):
    prepared = strategy.prepare(df)
    for i in range(len(prepared)):
        if strategy.decide(prepared, i).signal == Signal.BUY:
            return i
    raise AssertionError("kein Kaufsignal")


def _make_bot(settings, feed, tmp_path):
    strategy = build_strategy(settings.bot.strategy)
    broker = PaperBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    notifier = RecordingNotifier()
    bot = TradingBot(settings, feed, broker, strategy, RiskManager(settings.bot.risk), BotState(), notifier, tmp_path / "state.json")
    return bot, broker, notifier


def test_opens_position_on_buy_signal_and_closes_on_stop(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, broker, notifier = _make_bot(settings, feed, tmp_path)

    equity = bot.run_once()
    assert "BTC/USDT" in bot.state.positions
    pos = bot.state.positions["BTC/USDT"]
    assert pos.amount > 0 and pos.stop_loss < pos.entry_price < pos.take_profit
    assert any(m.startswith("KAUF") for m in notifier.messages)
    assert equity == pytest.approx(1000.0, abs=2.0)

    # gleiche Kerze erneut -> kein zweiter Einstieg, Position bleibt
    bot.run_once()
    assert len(bot.state.positions) == 1

    # Kurs faellt unter den Stop -> Position wird geschlossen
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert not bot.state.positions
    assert len(bot.state.trades) == 1
    assert bot.state.trades[0].reason == "stop_loss"
    assert bot.state.trades[0].pnl_quote < 0
    assert broker.balance_base("BTC/USDT") == 0.0


def test_take_profit_and_trailing_stop(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    initial_stop = pos.stop_loss

    feed.price = pos.entry_price * 1.02
    bot.run_once()
    assert pos.stop_loss > initial_stop, "Trailing-Stop sollte nachziehen"

    feed.price = pos.take_profit * 1.001
    bot.run_once()
    assert bot.state.trades[-1].reason == "take_profit"
    assert bot.state.trades[-1].pnl_quote > 0


def test_daily_loss_limit_halts_entries(settings, tmp_path):
    settings.bot.risk.max_daily_loss_pct = 0.01
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, notifier = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert bot.state.entries_halted_for_day
    assert any("Tagesverlustlimit" in m for m in notifier.messages)


def test_state_persists_and_reloads(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    reloaded = BotState.load(tmp_path / "state.json")
    assert "BTC/USDT" in reloaded.positions
    assert reloaded.last_candle_ts["BTC/USDT"] == int(feed.df["timestamp"].iloc[-1])


def test_reconcile_drops_position_sold_manually(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, broker, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    broker.balances["BTC"] = 0.0
    bot.reconcile()
    assert not bot.state.positions


def test_pause_blocks_new_entries_but_manages_positions(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.pause()
    bot.run_once()
    assert not bot.state.positions
    assert BotState.load(tmp_path / "state.json").entries_paused

    bot.resume()
    bot.state.last_candle_ts.clear()
    bot.run_once()
    assert "BTC/USDT" in bot.state.positions

    bot.pause()
    pos = bot.state.positions["BTC/USDT"]
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    assert not bot.state.positions, "Stop-Loss muss trotz Pause greifen"


def test_cycle_builds_snapshot_and_records_equity(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    snap = bot.get_snapshot()
    assert snap["mode"] == "paper" and snap["equity"] == pytest.approx(1000.0, abs=2.0)
    assert snap["positions"] and snap["positions"][0]["price"] == feed.price
    assert len(bot.state.equity_history) == 1
    bot.run_once()
    assert len(bot.state.equity_history) == 1, "Punkte nur alle 5 Minuten"


def test_equity_history_is_thinned_when_too_long(settings, tmp_path):
    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.state.equity_history = [[f"2020-01-01T00:00:{i % 60:02d}+00:00", float(i)] for i in range(bot.EQUITY_MAX_POINTS)]
    bot._record_equity(1234.0)
    assert len(bot.state.equity_history) < bot.EQUITY_MAX_POINTS
    assert bot.state.equity_history[-1][1] == 1234.0


def test_candles_are_cached_and_only_topped_up(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    feed = FakeFeed(full.iloc[:-3].reset_index(drop=True))
    limits = []
    orig = feed.fetch_ohlcv
    feed.fetch_ohlcv = lambda s, tf, limit: (limits.append(limit), orig(s, tf, limit))[1]
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    prepares = []
    real_prepare = bot.strategy.prepare
    bot.strategy.prepare = lambda df: (prepares.append(len(df)), real_prepare(df))[1]
    bot.process_symbol("BTC/USDT", feed.price)
    bot.process_symbol("BTC/USDT", feed.price)  # keine neue Kerze: nicht neu rechnen
    feed.df = full.iloc[:-1].reset_index(drop=True)  # zwei neue Kerzen
    bot.process_symbol("BTC/USDT", feed.price)
    need = bot.strategy.warmup_bars + 10
    assert limits == [need, TradingBot.CANDLE_TOPUP, TradingBot.CANDLE_TOPUP]
    assert len(prepares) == 2
    cached = bot._candles["BTC/USDT"]
    assert int(cached["timestamp"].iloc[-1]) == int(full["timestamp"].iloc[-2]) and len(cached) == min(need, len(full) - 1)
    feed.df = full.iloc[:10].reset_index(drop=True).assign(timestamp=full["timestamp"].iloc[:10] + 10**12)  # Luecke -> alles neu
    bot.process_symbol("BTC/USDT", feed.price)
    assert limits[-2:] == [TradingBot.CANDLE_TOPUP, need]


def test_delete_oldest_trades_keeps_newest_and_capital(settings, tmp_path):
    from tradingbot.models import Trade

    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot, broker, _ = _make_bot(settings, feed, tmp_path)
    for i in range(5):
        bot.state.trades.append(Trade("BTC/USDT", 1.0, 100.0, 101.0, 1.0, 1.0, 0.1, f"2026-10-0{i + 1}T00:00:00+00:00", f"2026-10-0{i + 1}T01:00:00+00:00", "stop_loss"))
    cash = broker.balance_quote()
    out = bot.delete_oldest_trades(3)
    assert out["deleted"] == 3 and out["remaining"] == 2 and out["from"].startswith("2026-10-01") and out["to"].startswith("2026-10-03")
    assert [t.opened_at[:10] for t in bot.state.trades] == ["2026-10-04", "2026-10-05"]
    assert broker.balance_quote() == cash
    assert bot.delete_oldest_trades(99)["deleted"] == 2


def test_chart_appends_running_candle(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    feed = FakeFeed(full.iloc[:-1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.process_symbol("BTC/USDT", feed.price)
    view_last = bot.views["BTC/USDT"]["candles"][-1][0]

    class LiveFeed:
        def fetch_chart(self, symbol, tf, limit):
            return full.tail(limit).reset_index(drop=True)

    bot.chart_feed = LiveFeed()
    d = bot.chart("BTC/USDT")
    assert d["candles"][-1][0] == int(full["timestamp"].iloc[-1]) > view_last
    assert all(len(l["values"]) == len(d["candles"]) for l in d["lines"])
    assert d["lines"][0]["values"][-1] is None
    # nur das Neue: ab der vorletzten Kerze, Linien gleich lang, ohne Trades
    t = d["candles"][-2][0]
    tail = bot.chart("BTC/USDT", since=t)
    assert tail["delta"] and [c[0] for c in tail["candles"]] == [c[0] for c in d["candles"][-2:]]
    assert all(len(l["values"]) == 2 for l in tail["lines"]) and "trades" not in tail
    assert "delta" not in bot.chart("BTC/USDT", since=1), "since vor der ersten Kerze: alles"


def test_account_reset_restores_starting_capital(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, broker, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    assert bot.state.positions, "Testvoraussetzung: eine Position ist offen"
    bot.state.entries_halted_for_day = True
    bot.state.equity_history.append(["x", 900.0])
    bot.protections._lock(10**15, "Stop-Loss-Serie")
    assert bot.request_account_reset(clear_trades=True)["pending"]
    bot.run_once()
    assert not bot.state.positions and bot.state.trades == []
    assert broker.balance_quote() == 1000.0 and not bot.state.entries_halted_for_day
    assert bot.protections.entry_allowed("BTC/USDT", 10**12)[0]
    assert len(bot.state.equity_history) <= 1


def test_account_reset_keeps_trades_if_wanted(settings, tmp_path):
    from tradingbot.models import Trade

    bot, broker, _ = _make_bot(settings, FakeFeed(make_ohlcv(trend_closes())), tmp_path)
    bot.state.trades.append(Trade("BTC/USDT", 1, 1, 1, -5, -5, 0, "2026-10-01T00:00:00+00:00", "2026-10-01T01:00:00+00:00", "stop_loss"))
    bot.request_account_reset(clear_trades=False)
    bot.run_once()
    assert len(bot.state.trades) == 1 and broker.balance_quote() == 1000.0


def test_trade_records_leverage_initial_stop_and_stop_kind(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    pos = next(iter(bot.state.positions.values()))
    assert pos.initial_stop == pos.stop_loss and pos.stop_kind == "initial"
    feed.price = pos.stop_loss * 0.99
    bot.run_once()
    t = bot.state.trades[-1]
    assert t.reason.startswith("stop_loss") and t.stop_kind == "initial" and t.leverage == pos.leverage and t.initial_stop == pos.initial_stop


def test_backtest_marks_trailing_stop_kind():
    from tradingbot.backtest import Backtester
    from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
    from tradingbot.risk import RiskManager
    from tradingbot.synthetic import make_series

    cfg = StrategyConfig(name="donchian", allow_short=True, take_profit_atr_mult=0)
    s = build_strategy(cfg)
    r = Backtester(s, RiskManager(RiskConfig(min_order_quote=0)), cfg, PaperConfig()).run(make_series("trend", 3000, 5), "X")
    kinds = {t.stop_kind for t in r.trades if t.reason == "stop_loss"}
    assert kinds and kinds <= {"initial", "trailing"} and all(t.initial_stop and t.leverage for t in r.trades)


def test_realtime_mode_trades_on_running_candle_once_per_candle(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)
    closed = full.iloc[:buy_i].reset_index(drop=True)          # Signal-Kerze ist noch nicht geschlossen
    settings.bot.strategy = settings.bot.strategy.model_copy(update={"realtime_signals": True})
    settings.bot.protections = settings.bot.protections.model_copy(update={"cooldown_candles": 0})

    class LiveFeed(FakeFeed):
        def fetch_chart(self, symbol, tf, limit):
            return full.iloc[: buy_i + 1].tail(limit).reset_index(drop=True)

    feed = LiveFeed(closed)
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    assert "BTC/USDT" in bot.state.positions, "Einstieg schon auf der laufenden Kerze"
    pos = bot.state.positions["BTC/USDT"]
    bot._close_position("BTC/USDT", pos, "test")
    bot._acted_candle["BTC/USDT"] = (int(full["timestamp"].iloc[buy_i]), "any")
    bot.run_once()
    assert "BTC/USDT" not in bot.state.positions, "kein zweiter Einstieg in derselben Kerze"
    bot._acted_candle["BTC/USDT"] = (int(full["timestamp"].iloc[buy_i]), "long")
    bot.run_once()
    assert "BTC/USDT" not in bot.state.positions, "nach Long-Ausstieg kein neuer Long in derselben Kerze"
    bot._acted_candle["BTC/USDT"] = (int(full["timestamp"].iloc[buy_i]), "short")
    bot.run_once()
    assert "BTC/USDT" in bot.state.positions, "nach Short-Ausstieg darf der Long (Umkehr) in derselben Kerze kommen"


def test_closed_candle_mode_waits_for_close(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    strategy = build_strategy(settings.bot.strategy)
    buy_i = _first_buy_index(strategy, full)

    class LiveFeed(FakeFeed):
        def fetch_chart(self, symbol, tf, limit):
            return full.iloc[: buy_i + 1].tail(limit).reset_index(drop=True)

    feed = LiveFeed(full.iloc[:buy_i].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.run_once()
    assert not bot.state.positions


def test_signals_from_separate_spot_feed(settings, tmp_path):
    full = make_ohlcv(trend_closes())
    trade_feed = FakeFeed(full)
    bot, _, _ = _make_bot(settings, trade_feed, tmp_path)
    asked = []

    class SpotFeed(FakeFeed):
        def fetch_ohlcv(self, symbol, timeframe, limit):
            asked.append(symbol)
            return super().fetch_ohlcv(symbol, timeframe, limit)

    bot.signal_feed = SpotFeed(full)
    bot.process_symbol("BTC/USDT:USDT", trade_feed.price)
    assert asked == ["BTC/USDT"], "Kerzen fuer die Signale vom Spot-Paar"
    assert trade_feed.calls == 0, "der Handels-Feed liefert keine Signal-Kerzen"
    assert bot._chart_symbol("BTC/USDT:USDT") == "BTC/USDT"


def test_reprice_fees_recomputes_trades_capital_and_history(settings, tmp_path):
    from tradingbot.models import Trade

    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot, broker, _ = _make_bot(settings, feed, tmp_path)
    old = (100 + 110) * 2 * 0.0005
    bot.state.trades.append(Trade("BTC/USDT", 2, 100, 110, 20 - old, 0, old, "2026-10-01T00:00:00+00:00", "2026-10-01T01:00:00+00:00", "x"))
    bot.state.equity_history = [["2026-10-01T00:30:00+00:00", 1000.0], ["2026-10-01T02:00:00+00:00", 1019.79]]
    bot.state.day_start_equity = 1019.79
    cash = broker.balance_quote()
    bot.cfg.paper.fee_pct = 0.06
    out = bot.reprice_fees()
    t = bot.state.trades[-1]
    new = (100 + 110) * 2 * 0.0006
    assert abs(t.fees - new) < 1e-12 and abs(t.pnl_quote - (20 - new)) < 1e-12
    assert abs(out["delta"] - (old - new)) < 1e-12 and abs(broker.balance_quote() - (cash + old - new)) < 1e-9
    assert bot.state.equity_history[0][1] == 1000.0, "Punkt vor dem Ausstieg bleibt"
    assert abs(bot.state.equity_history[1][1] - round(1019.79 + old - new, 4)) < 1e-9
    assert abs(bot.state.day_start_equity - (1019.79 + old - new)) < 1e-9
    assert bot.reprice_fees()["changed"] == 0, "zweimal ausfuehren aendert nichts"
