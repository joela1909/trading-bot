import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.backtest import Backtester
from tradingbot.bot import TradingBot
from tradingbot.config import BotConfig, PaperConfig, RiskConfig, Secrets, Settings, StrategyConfig
from tradingbot.data import load_history
from tradingbot.exchange import InsufficientFunds, PaperFuturesBroker
from tradingbot.models import Position, Signal
from tradingbot.notify import Notifier
from tradingbot.research import format_report, run_comparison
from tradingbot.risk import RiskManager
from tradingbot.snapshot import make_snapshot
from tradingbot.state import BotState
from tradingbot.strategy import REGISTRY, build_strategy
from tradingbot.synthetic import make_series
from tests.conftest import FakeFeed, make_ohlcv, trend_closes

SYMBOL = "BTC/USDT:USDT"


def _inverted(df: pd.DataFrame) -> pd.DataFrame:
    """Spiegelt eine Kursreihe (1/Kurs): aus Anstiegen werden Rueckgaenge, damit Short-Signale entstehen."""
    out = df.copy()
    k = 10_000.0
    out["open"], out["close"] = k / df["open"], k / df["close"]
    out["high"], out["low"] = k / df["low"], k / df["high"]
    return out


def _mixed() -> pd.DataFrame:
    trend = make_series("trend", 3000, 5)
    rev = make_series("meanrev", 3000, 6, start_price=float(trend["close"].iloc[-1]), start_ts=int(trend["timestamp"].iloc[-1]) + 3_600_000)
    return _inverted(pd.concat([trend, rev], ignore_index=True))


# ------------------------------------------------------------------ Konfiguration
def test_short_requires_futures_and_symbols_are_mapped():
    with pytest.raises(ValidationError):
        BotConfig(strategy=StrategyConfig(allow_short=True))
    cfg = BotConfig(market="futures", symbols=["BTC/USDT", "SOL/USDT", "ETH/USDT:USDT"], strategy=StrategyConfig(allow_short=True))
    assert cfg.symbols == ["BTC/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT"]
    assert cfg.market_symbol("XRP/USDT") == "XRP/USDT:USDT"
    assert BotConfig().market_symbol("XRP/USDT") == "XRP/USDT"


def test_claude_can_short_but_live_futures_is_blocked():
    BotConfig(market="futures", strategy=StrategyConfig(name="claude_advisor", allow_short=True))  # KI-Konto handelt Long und Short
    s = Settings(bot=BotConfig(market="futures", mode="live"), secrets=Secrets(_env_file=None, live_trading_confirmed=True, exchange_api_key="k", exchange_api_secret="s"))
    with pytest.raises(RuntimeError, match="Papierhandel"):
        s.validate_for_run()
    Settings(bot=BotConfig(market="futures"), secrets=Secrets(_env_file=None)).validate_for_run()


# ------------------------------------------------------------------ Paper-Futures-Broker
class _PriceFeed:
    def __init__(self, price):
        self.price = price

    def fetch_price(self, symbol):
        return self.price

    def market_limits(self, symbol):
        raise RuntimeError("keine Limits")


def test_paper_futures_short_profit_and_fees(tmp_path):
    feed = _PriceFeed(100.0)
    b = PaperFuturesBroker(feed, "USDT", 1000.0, fee_pct=0.1, slippage_pct=0.0, state_path=tmp_path / "f.json")
    fill = b.open_position(SYMBOL, "short", 1.0)
    assert fill.price == 100.0 and fill.fee == pytest.approx(0.1)
    assert b.balance_quote() == pytest.approx(1000 - 100 - 0.1)
    assert b.balance_base(SYMBOL) == 1.0
    pos = Position(SYMBOL, 1.0, 100.0, 103.0, 90.0, "t", 100.0, 0.1, side="short")
    feed.price = 90.0
    assert b.balance_quote() + pos.value(90.0) == pytest.approx(1000 - 0.1 + 10.0)
    b.close_position(SYMBOL, "short", 1.0)
    assert b.balance_quote() == pytest.approx(1000 - 0.1 + 10.0 - 0.09)
    assert b.balance_base(SYMBOL) == 0.0
    # Zustand wird gespeichert
    assert PaperFuturesBroker(feed, "USDT", 1.0, 0.1, 0.0, state_path=tmp_path / "f.json").balance_quote() == pytest.approx(b.balance_quote())


def test_paper_futures_leverage_slippage_and_errors():
    feed = _PriceFeed(100.0)
    b = PaperFuturesBroker(feed, "USDT", 100.0, fee_pct=0.0, slippage_pct=1.0, leverage=2.0)
    fill = b.open_position(SYMBOL, "short", 1.0)
    assert fill.price == pytest.approx(99.0), "Short verkauft mit Abschlag"
    assert b.balance_quote() == pytest.approx(100.0 - 99.0 / 2)
    with pytest.raises(InsufficientFunds):
        b.open_position(SYMBOL, "long", 1.0)
    with pytest.raises(ValueError):
        b.close_position(SYMBOL, "long", 1.0)
    assert b.close_position(SYMBOL, "short", 1.0).price == pytest.approx(101.0), "Short kauft mit Aufschlag zurueck"
    with pytest.raises(InsufficientFunds):
        PaperFuturesBroker(feed, "USDT", 10.0, 0.0, 0.0).open_position(SYMBOL, "short", 1.0)


def test_position_helpers_for_short():
    p = Position(SYMBOL, 2.0, 100.0, 105.0, 90.0, "t", 100.0, side="short", leverage=2.0)
    assert p.unrealized(95.0) == pytest.approx(10.0)
    assert p.value(95.0) == pytest.approx(100.0 + 10.0)
    assert p.stop_hit(105.0) and not p.stop_hit(104.9)
    assert p.take_profit_hit(90.0) and not p.take_profit_hit(90.1)
    assert Position.from_dict({k: v for k, v in p.to_dict().items() if k not in ("side", "leverage")}).side == "long"


def test_risk_sizes_short_with_stop_above_entry():
    risk = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=100.0, min_order_quote=0.0))
    assert risk.position_size(1000, 1000, 100.0, 102.0, side="short") == pytest.approx(5.0)
    assert risk.position_size(1000, 1000, 100.0, 98.0, side="short") == 0.0, "Stop unter dem Einstieg ist fuer Short falsch"


# ------------------------------------------------------------------ Strategien
@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_strategy_produces_valid_short_decision(name):
    strat = build_strategy(StrategyConfig(name=name, allow_short=True, take_profit_atr_mult=4.0))
    df = strat.prepare(_mixed())
    short, cover = strat.short_signals(df)
    assert short.dtype == bool and cover.dtype == bool and len(short) == len(df)
    assert short.any(), f"{name} shortet nie"
    if not strat.fixed_levels:  # Strategien mit festen Stop-/Zielkursen steigen nur ueber diese aus
        assert cover.any(), f"{name} schliesst Shorts nie"
    i = next(int(k) for k in np.flatnonzero(short) if not strat.signals(df)[0][k])
    d = strat.decide(df, i, context={"position": None})
    assert d.signal == Signal.SHORT
    close = float(df["close"].iloc[i])
    assert d.stop_loss > close and (d.take_profit < close if d.take_profit is not None else strat.fixed_levels)
    if not strat.fixed_levels:
        j = int(np.flatnonzero(cover)[-1])
        assert strat.decide(df, j, context={"position": {"side": "short"}}).signal == Signal.COVER


def test_no_short_decisions_when_disabled():
    strat = build_strategy(StrategyConfig(name="ema_cross"))
    df = strat.prepare(_mixed())
    assert all(strat.decide(df, i, context={"position": None}).signal != Signal.SHORT for i in range(0, len(df), 7))
    assert strat.all_signals(df)[2] is None


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_short_signals_have_no_lookahead(name):
    strat = build_strategy(StrategyConfig(name=name, allow_short=True))
    df = make_series("meanrev", 2500, 9)
    assert check_lookahead(strat, df, checks=15).ok
    assert check_recursive(strat, df, checks=20).ok


def test_intraday_warmup_covers_a_whole_day_at_1m():
    assert build_strategy(StrategyConfig(name="vwap_reversion"), timeframe="1m").warmup_bars >= 1440
    assert build_strategy(StrategyConfig(name="opening_range"), timeframe="1m").warmup_bars >= 1440
    assert build_strategy(StrategyConfig(name="vwap_reversion"), timeframe="15m").warmup_bars == 300


# ------------------------------------------------------------------ Backtest
def _flat_then_fall() -> pd.DataFrame:
    closes = np.r_[np.full(350, 100.0), np.linspace(100.0, 90.0, 51)[1:], np.full(50, 90.0)]
    return make_ohlcv(closes)


def test_backtest_short_pnl_is_exact():
    cfg = StrategyConfig(name="donchian", allow_short=True, stop_atr_mult=50.0, take_profit_atr_mult=0.0, trailing_stop=False)
    strat = build_strategy(cfg)
    df = strat.prepare(_flat_then_fall())
    n = len(df)
    z = np.zeros(n, dtype=bool)
    short, cover = z.copy(), z.copy()
    short[349], cover[380] = True, True
    risk = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=50.0, min_order_quote=0.0))
    paper = PaperConfig(starting_balance=1000.0, fee_pct=0.1, slippage_pct=0.0)
    res = Backtester(strat, risk, cfg, paper).run(df, SYMBOL, prepared=True, signals=(z, z, short, cover))
    assert res.n_trades == 1
    t = res.trades[0]
    entry, exit_ = float(df["close"].iloc[349]), float(df["close"].iloc[380])
    assert t.side == "short" and t.reason == "signal"
    assert t.entry_price == pytest.approx(entry) and t.exit_price == pytest.approx(exit_)
    expected = (entry - exit_) * t.amount - (entry + exit_) * t.amount * 0.001
    assert t.pnl_quote == pytest.approx(expected)
    assert t.pnl_quote > 0
    assert res.end_equity == pytest.approx(1000.0 + expected)
    assert res.end_equity == pytest.approx(float(res.equity_curve.iloc[-1]))


def test_backtest_ignores_short_signals_when_disabled():
    cfg = StrategyConfig(name="donchian", stop_atr_mult=50.0)
    strat = build_strategy(cfg)
    df = strat.prepare(_flat_then_fall())
    z = np.zeros(len(df), dtype=bool)
    short = z.copy()
    short[349] = True
    res = Backtester(strat, RiskManager(RiskConfig()), cfg, PaperConfig()).run(df, SYMBOL, prepared=True, signals=(z, z, short, z))
    assert res.n_trades == 0


def test_backtest_short_stop_loss_above_entry():
    cfg = StrategyConfig(name="donchian", allow_short=True, stop_atr_mult=2.0, take_profit_atr_mult=0.0, trailing_stop=False)
    strat = build_strategy(cfg)
    closes = np.r_[np.full(350, 100.0), np.linspace(100.0, 120.0, 30)]
    df = strat.prepare(make_ohlcv(closes))
    z = np.zeros(len(df), dtype=bool)
    short = z.copy()
    short[349] = True
    res = Backtester(strat, RiskManager(RiskConfig(min_order_quote=0.0)), cfg, PaperConfig(slippage_pct=0.0)).run(df, SYMBOL, prepared=True, signals=(z, z, short, z))
    assert res.trades[0].reason == "stop_loss" and res.trades[0].exit_price > res.trades[0].entry_price
    assert res.trades[0].pnl_quote < 0


@pytest.mark.parametrize("name", ["ema_cross", "vwap_reversion", "bollinger_reversion"])
def test_backtest_with_shorts_is_consistent(name):
    cfg = StrategyConfig(name=name, allow_short=True)
    strat = build_strategy(cfg)
    df = _mixed()
    res = Backtester(strat, RiskManager(RiskConfig()), cfg, PaperConfig()).run(df, SYMBOL)
    sides = {t.side for t in res.trades}
    assert "short" in sides
    assert res.end_equity - res.start_balance == pytest.approx(sum(t.pnl_quote for t in res.trades), abs=1e-6)


def test_comparison_report_mentions_direction():
    data = {f"D{i}/USDT:USDT": _mixed().iloc[:3000].reset_index(drop=True) for i in range(2)}
    cmp = run_comparison(data, ["ema_cross"], StrategyConfig(allow_short=True), RiskConfig(), PaperConfig(), "1h", min_trades=1)
    assert "Richtung:** Long und Short" in format_report(cmp)


# ------------------------------------------------------------------ Bot
class _Notes(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _futures_bot(small_strategy_cfg, tmp_path):
    cfg = small_strategy_cfg.model_copy(update={"allow_short": True})
    bot_cfg = BotConfig(
        market="futures", symbols=["BTC/USDT"], timeframe="1h", strategy=cfg,
        state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"),
    )
    settings = Settings(bot=bot_cfg, secrets=Secrets(_env_file=None))
    settings.bot.protections.cooldown_candles = 0
    strat = build_strategy(settings.bot.strategy)
    full = make_ohlcv(trend_closes())
    prepared = strat.prepare(full)
    i = next(k for k in range(len(prepared)) if strat.decide(prepared, k, context={"position": None}).signal == Signal.SHORT)
    feed = FakeFeed(full.iloc[: i + 1].reset_index(drop=True))
    broker = PaperFuturesBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "pf.json")
    notes = _Notes()
    bot = TradingBot(settings, feed, broker, strat, RiskManager(settings.bot.risk), BotState(), notes, tmp_path / "state.json")
    return bot, feed, broker, notes


def test_bot_opens_short_and_stops_out_above(small_strategy_cfg, tmp_path):
    bot, feed, broker, notes = _futures_bot(small_strategy_cfg, tmp_path)
    equity = bot.run_once()
    pos = bot.state.positions[SYMBOL]
    assert pos.side == "short" and pos.take_profit < pos.entry_price < pos.stop_loss
    assert any(m.startswith("SHORT ") for m in notes.messages)
    assert equity == pytest.approx(1000.0, abs=2.0)
    feed.price = pos.stop_loss * 1.01
    bot.run_once()
    assert not bot.state.positions
    t = bot.state.trades[-1]
    assert t.side == "short" and t.reason == "stop_loss" and t.pnl_quote < 0
    assert broker.balance_base(SYMBOL) == 0.0
    assert broker.balance_quote() == pytest.approx(1000.0 + t.pnl_quote)


def test_bot_short_trailing_stop_and_take_profit(small_strategy_cfg, tmp_path):
    bot, feed, broker, _ = _futures_bot(small_strategy_cfg, tmp_path)
    bot.run_once()
    pos = bot.state.positions[SYMBOL]
    initial_stop = pos.stop_loss
    feed.price = pos.entry_price * 0.98
    bot.run_once()
    assert pos.stop_loss < initial_stop, "Trailing-Stop muss bei Short nach unten nachziehen"
    snap = bot.get_snapshot()
    assert snap["positions"][0]["side"] == "short" and snap["positions"][0]["pnl_quote"] > 0
    feed.price = pos.take_profit * 0.999
    bot.run_once()
    t = bot.state.trades[-1]
    assert t.reason == "take_profit" and t.pnl_quote > 0 and t.side == "short"


def test_snapshot_short_pnl_sign():
    state = BotState(positions={SYMBOL: Position(SYMBOL, 1.0, 100.0, 105.0, None, "t", 100.0, side="short")})
    snap = make_snapshot(state=state, mode="paper", exchange="binance", timeframe="1m", quote="USDT", symbols=[SYMBOL], equity=None,
                         free_quote=None, prices={SYMBOL: 95.0}, last_cycle=None, running=True, controls=False)
    assert snap["positions"][0]["pnl_quote"] == pytest.approx(5.0)


def test_snapshot_shows_position_size_and_risk():
    pos = Position(SYMBOL, 2.0, 100.0, 103.0, None, "t", 100.0, side="short", leverage=2.0)
    state = BotState(positions={SYMBOL: pos})
    snap = make_snapshot(state=state, mode="paper", exchange="binance", timeframe="1m", quote="USDT", symbols=[SYMBOL], equity=1000.0,
                         free_quote=None, prices={SYMBOL: 99.0}, last_cycle=None, running=True, controls=False)
    p = snap["positions"][0]
    assert p["notional"] == pytest.approx(198.0) and p["size_pct"] == pytest.approx(19.8)
    assert p["margin"] == pytest.approx(100.0) and p["leverage"] == 2.0
    assert p["risk_quote"] == pytest.approx(6.0) and p["risk_pct"] == pytest.approx(0.6)


def test_bot_messages_contain_position_size(small_strategy_cfg, tmp_path):
    bot, feed, _, notes = _futures_bot(small_strategy_cfg, tmp_path)
    bot.run_once()
    opened = next(m for m in notes.messages if m.startswith("SHORT "))
    assert "Groesse" in opened and "Risiko bis Stop" in opened
    feed.price = bot.state.positions[SYMBOL].stop_loss * 1.01
    bot.run_once()
    assert "Groesse" in next(m for m in notes.messages if m.startswith("SHORT ZU"))


def test_history_cache_filename_has_no_colon(tmp_path):
    class Feed:
        def fetch_ohlcv_range(self, symbol, timeframe, since, until=None):
            return make_ohlcv([100.0] * 10)

    load_history(Feed(), SYMBOL, "1m", "2026-01-01", "2026-01-02", tmp_path)
    names = [p.name for p in tmp_path.iterdir()]
    assert names and all(":" not in n for n in names)


# ------------------------------------------------------------------ Marktansicht ("Was der Bot sieht")
def test_bot_builds_market_view_with_status(small_strategy_cfg, tmp_path):
    import json

    bot, feed, _, _ = _futures_bot(small_strategy_cfg, tmp_path)
    bot.run_once()
    snap = bot.get_snapshot()
    json.dumps(snap, allow_nan=False)  # gueltiges JSON fuers Dashboard, kein NaN
    view = snap["market"][0]
    assert view["symbol"] == SYMBOL and "candles" not in view, "Uebersicht ohne Kerzen (klein halten)"
    chart = bot.chart(SYMBOL)
    json.dumps(chart, allow_nan=False)
    assert len(chart["candles"]) > 10 and chart["trades"] == []
    assert {line["label"] for line in chart["lines"]} == {"EMA schnell", "EMA langsam"}
    assert all(line["visible"] for line in chart["lines"])
    assert bot.chart("XRP/USDT:USDT")["candles"] == []
    assert view["position"]["side"] == "short" and view["position"]["opened_at"]
    assert view["status"].startswith("Short eroeffnet")
    assert view["explain"]
    bot.run_once()
    assert bot.get_snapshot()["market"][0]["status"].startswith("Short offen")


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_strategy_explains_itself(name):
    strat = build_strategy(StrategyConfig(name=name, allow_short=True))
    df = strat.prepare(_mixed())
    lines = strat.explain(df)
    assert lines and all(isinstance(x, str) and x for x in lines)
    assert all(col in df.columns for col, _, _ in strat.chart_lines if col != "ema_trend")


# ------------------------------------------------------------------ Kostenfilter
def test_target_check_math():
    risk = RiskManager(RiskConfig(min_target_cost_ratio=2.0))
    # Kosten hin und zurueck: 2 x (0,05 + 0,05) = 0,2 %. Ziel 4 ATR bei ATR 0,05 % = 0,2 % -> unter 2 x 0,2 %
    ok, target, cost = risk.target_check(100.0, 0.2, 0.05, 0.05)
    assert not ok and target == pytest.approx(0.2) and cost == pytest.approx(0.2)
    ok, target, _ = risk.target_check(100.0, 0.8, 0.05, 0.05)
    assert ok and target == pytest.approx(0.8)
    assert RiskManager(RiskConfig()).target_check(100.0, 0.004, 0.05, 0.05)[0], "0 = Filter aus"


def test_backtest_skips_trades_whose_target_does_not_cover_costs():
    cfg = StrategyConfig(name="ensemble", allow_short=True)
    s = build_strategy(cfg)
    df = _mixed()
    paper = PaperConfig(fee_pct=0.5, slippage_pct=0.5)  # Kosten 2 % je Trade, Ziel muss 20 x so gross sein
    free = Backtester(s, RiskManager(RiskConfig()), cfg, paper).run(df, SYMBOL)
    strict = Backtester(s, RiskManager(RiskConfig(min_target_cost_ratio=20.0)), cfg, paper).run(df, SYMBOL)
    assert free.n_trades > 0 and strict.n_trades == 0


def test_bot_rejects_signal_when_target_too_small(small_strategy_cfg, tmp_path):
    bot, feed, broker, _ = _futures_bot(small_strategy_cfg, tmp_path)
    bot.cfg.risk.min_target_cost_ratio = 20.0
    bot.cfg.paper.fee_pct = 1.0  # Kosten 2,1 % je Trade -> Ziel muesste 42 % sein
    bot.run_once()
    assert not bot.state.positions
    assert "lohnt nicht" in bot.get_snapshot()["market"][0]["status"]
    assert broker.balance_quote() == pytest.approx(1000.0)


def test_risk_shown_includes_costs(small_strategy_cfg, tmp_path):
    bot, feed, _, notes = _futures_bot(small_strategy_cfg, tmp_path)
    bot.run_once()
    pos = bot.state.positions[SYMBOL]
    shown = bot.get_snapshot()["positions"][0]["risk_quote"]
    bare = abs(pos.entry_price - pos.stop_loss) * pos.amount
    assert shown > bare and shown == pytest.approx(bare + pos.entry_fee + pos.amount * pos.stop_loss * bot._exit_cost_rate())
    feed.price = pos.stop_loss * 1.0001
    bot.run_once()
    assert -bot.state.trades[-1].pnl_quote <= shown * 1.05, "tatsaechlicher Verlust passt zur angezeigten Zahl"


# ------------------------------------------------------------------ Mindestabstand des Stops
def test_min_stop_pct_widens_stop_and_target_in_ratio():
    s = build_strategy(StrategyConfig(name="ema_cross", stop_atr_mult=2.0, take_profit_atr_mult=4.0, min_stop_pct=0.5))
    assert s.stop_distance(100.0, 0.05) == pytest.approx(0.5), "2 x 0,05 = 0,1 < 0,5 % -> Mindestabstand"
    assert s.target_distance(100.0, 0.05) == pytest.approx(1.0), "Ziel bleibt doppelt so weit wie der Stop"
    assert s.stop_distance(100.0, 1.0) == pytest.approx(2.0), "groesser als der Mindestabstand -> ATR zaehlt"
    assert s.trailing_stop(100.0, 0.05) == pytest.approx(99.5)
    assert s.trailing_stop_short(100.0, 0.05) == pytest.approx(100.5)
    plain = build_strategy(StrategyConfig(name="ema_cross", stop_atr_mult=2.0, take_profit_atr_mult=4.0))
    assert plain.stop_distance(100.0, 0.05) == pytest.approx(0.1) and plain.target_distance(100.0, 0.05) == pytest.approx(0.2)
    assert build_strategy(StrategyConfig(name="ema_cross", take_profit_atr_mult=0.0)).target_distance(100.0, 1.0) is None


def test_bot_uses_min_stop(small_strategy_cfg, tmp_path):
    cfg = small_strategy_cfg.model_copy(update={"min_stop_pct": 15.0, "stop_atr_mult": 0.5, "take_profit_atr_mult": 1.0})
    bot, feed, _, _ = _futures_bot(cfg, tmp_path)
    bot.run_once()
    pos = bot.state.positions[SYMBOL]
    assert pos.stop_loss / pos.entry_price - 1 == pytest.approx(0.15, rel=1e-6), "Short-Stop 15 % ueber dem Einstieg"
    assert 1 - pos.take_profit / pos.entry_price == pytest.approx(0.30, rel=1e-6), "Ziel doppelt so weit"
