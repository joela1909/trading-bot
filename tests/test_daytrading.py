from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.backtest import Backtester
from tradingbot.config import DaytradingConfig, PaperConfig, ProtectionConfig, RiskConfig, StrategyConfig
from tradingbot.indicators import session_vwap
from tradingbot.research import format_report, run_comparison
from tradingbot.risk import RiskManager
from tradingbot.session import Session, session_day_key
from tradingbot.strategy import build_strategy
from tradingbot.synthetic import make_series
from tests.conftest import FakeFeed, make_ohlcv, trend_closes
from tests.test_bot import _first_buy_index, _make_bot

TF15 = 15 * 60_000
UTC = timezone.utc


def _dt(**kw) -> DaytradingConfig:
    base = dict(enabled=True, timezone="UTC", entry_start="08:00", entry_end="20:00", flat_time="21:00", max_trades_per_day=0)
    return DaytradingConfig(**{**base, **kw})


def _local_min(ts: pd.Timestamp) -> int:
    return ts.hour * 60 + ts.minute


# ------------------------------------------------------------------ Config und Uhrzeiten
def test_config_rejects_wrong_order_and_unknown_timezone():
    with pytest.raises(ValidationError):
        DaytradingConfig(entry_start="10:00", entry_end="09:00", flat_time="22:00")
    with pytest.raises(ValidationError):
        DaytradingConfig(entry_end="22:00", flat_time="21:00")
    with pytest.raises(ValidationError):
        DaytradingConfig(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        DaytradingConfig(entry_start="8 Uhr")


def test_session_uses_local_time_including_dst():
    s = Session(_dt(timezone="Europe/Berlin", entry_start="08:00", entry_end="21:30", flat_time="22:00"))
    summer = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 08:00 in Berlin (Sommerzeit)
    winter = datetime(2026, 12, 1, 6, 0, tzinfo=UTC)  # 07:00 in Berlin
    assert s.entry_allowed(summer)
    assert not s.entry_allowed(winter)
    opened = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    assert s.must_close(datetime(2026, 7, 1, 19, 59, tzinfo=UTC), opened) is None
    assert s.must_close(datetime(2026, 7, 1, 20, 0, tzinfo=UTC), opened) == "session_end"
    assert s.must_close(datetime(2026, 7, 2, 7, 0, tzinfo=UTC), opened) == "session_end"


def test_max_hold_minutes():
    s = Session(_dt(max_hold_minutes=90))
    opened = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    assert s.must_close(opened + timedelta(minutes=89), opened) is None
    assert s.must_close(opened + timedelta(minutes=90), opened) == "max_hold"


# ------------------------------------------------------------------ Indikatoren / Strategien
def test_session_vwap_resets_each_day():
    df = make_ohlcv(np.r_[np.full(96, 100.0), np.full(96, 200.0)], tf_ms=TF15, start_ts=1_767_225_600_000)  # 2026-01-01 00:00 UTC
    vwap = session_vwap(df, session_day_key(df["timestamp"], "UTC"))
    assert vwap.iloc[95] == pytest.approx(100.0, rel=0.02)
    # ohne Reset laege der VWAP nach der ersten Kerze von Tag 2 noch bei ~101
    assert vwap.iloc[96] > 150, "VWAP muss am neuen Tag neu beginnen"
    assert vwap.iloc[191] == pytest.approx(200.0, rel=0.01)


def test_opening_range_never_buys_before_range_is_complete():
    strat = build_strategy(StrategyConfig(name="opening_range", orb_minutes=60), session=_dt())
    df = strat.prepare(make_series("trend", 4000, 11, tf_ms=TF15))
    buy, _ = strat.signals(df)
    assert buy.any()
    open_ts = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    minute = (open_ts.dt.hour * 60 + open_ts.dt.minute).to_numpy()
    assert (minute[buy] >= 9 * 60).all(), "Kauf erst nach Ende der Eroeffnungsspanne (08:00 + 60 min)"


@pytest.mark.parametrize("name", ["vwap_reversion", "opening_range"])
def test_intraday_strategies_pass_lookahead_and_recursive_check(name):
    strat = build_strategy(StrategyConfig(name=name), session=_dt())
    df = make_series("meanrev", 3000, 5, tf_ms=TF15)
    assert check_lookahead(strat, df).ok
    assert check_recursive(strat, df).ok


# ------------------------------------------------------------------ Backtest
@pytest.mark.parametrize("name", ["vwap_reversion", "opening_range", "ema_cross"])
def test_backtest_respects_session(name):
    dt = _dt(max_trades_per_day=2)
    cfg = StrategyConfig(name=name, take_profit_atr_mult=0.0)
    strat = build_strategy(cfg, session=dt)
    df = make_series("meanrev", 8000, 21, tf_ms=TF15)
    res = Backtester(strat, RiskManager(RiskConfig()), cfg, PaperConfig(), protections_cfg=ProtectionConfig(), daytrading=dt).run(df, "X/USDT")
    assert res.n_trades > 0
    per_day: dict[str, int] = {}
    for t in res.trades:
        # Entscheidung faellt beim Kerzenschluss, deshalb + 15 min
        decided = pd.Timestamp(t.opened_at) + pd.Timedelta(minutes=15)
        closed = pd.Timestamp(t.closed_at) + pd.Timedelta(minutes=15)
        assert 8 * 60 <= _local_min(decided) < 20 * 60, t
        assert closed - decided < pd.Timedelta(hours=13), "keine Position ueber Nacht"
        if t.reason != "end_of_data":
            assert closed.date() == decided.date() and _local_min(closed) <= 21 * 60, t
        key = decided.strftime("%Y-%m-%d")
        per_day[key] = per_day.get(key, 0) + 1
    assert max(per_day.values()) <= 2
    assert any(t.reason == "session_end" for t in res.trades)


def test_backtest_without_daytrading_unchanged():
    cfg = StrategyConfig(name="donchian")
    df = make_series("trend", 4000, 7)
    a = Backtester(build_strategy(cfg), RiskManager(RiskConfig()), cfg, PaperConfig()).run(df, "X/USDT")
    b = Backtester(build_strategy(cfg), RiskManager(RiskConfig()), cfg, PaperConfig(), daytrading=DaytradingConfig(enabled=False)).run(df, "X/USDT")
    assert a.end_equity == b.end_equity and a.n_trades == b.n_trades


def test_comparison_reports_daytrading_rules():
    data = {f"D{i}/USDT": make_series("meanrev", 4000, 30 + i, tf_ms=TF15) for i in range(2)}
    cmp = run_comparison(data, ["vwap_reversion"], StrategyConfig(), RiskConfig(), PaperConfig(), "15m", min_trades=1, daytrading=_dt())
    report = format_report(cmp)
    assert "Tageshandel:** Einstiege 08:00-20:00" in report


# ------------------------------------------------------------------ Bot
def _bot_with_session(settings, tmp_path, dt: DaytradingConfig, now: datetime):
    settings.bot.daytrading = dt
    settings.bot.protections.cooldown_candles = 0  # Abkuehlphase laeuft auf echter Uhrzeit, hier stoert sie
    full = make_ohlcv(trend_closes())
    buy_i = _first_buy_index(build_strategy(settings.bot.strategy), full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, notifier = _make_bot(settings, feed, tmp_path)
    bot._now = lambda: now
    return bot, notifier


def test_bot_ignores_buy_outside_entry_window(settings, tmp_path):
    bot, _ = _bot_with_session(settings, tmp_path, _dt(), datetime(2026, 7, 1, 6, 0, tzinfo=UTC))
    bot.run_once()
    assert not bot.state.positions


def test_bot_closes_position_at_flat_time(settings, tmp_path):
    bot, _ = _bot_with_session(settings, tmp_path, _dt(), datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    bot.run_once()
    pos = bot.state.positions["BTC/USDT"]
    pos.opened_at = "2026-07-01T10:00:00+00:00"
    bot._now = lambda: datetime(2026, 7, 1, 21, 0, tzinfo=UTC)
    bot.run_once()
    assert not bot.state.positions
    assert bot.state.trades[-1].reason == "session_end"


def test_bot_closes_leftover_position_from_previous_day(settings, tmp_path):
    bot, _ = _bot_with_session(settings, tmp_path, _dt(), datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    bot.run_once()
    bot.state.positions["BTC/USDT"].opened_at = "2026-07-01T10:00:00+00:00"
    bot._now = lambda: datetime(2026, 7, 2, 7, 0, tzinfo=UTC)  # PC war zur flat_time aus
    bot.run_once()
    assert not bot.state.positions and bot.state.trades[-1].reason == "session_end"


def test_bot_respects_max_entries_per_day_and_resets_next_day(settings, tmp_path):
    bot, _ = _bot_with_session(settings, tmp_path, _dt(max_trades_per_day=1), datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    bot.run_once()
    assert bot.state.entries_today == 1
    bot.state.positions["BTC/USDT"].opened_at = "2026-07-01T10:00:00+00:00"
    bot._now = lambda: datetime(2026, 7, 1, 21, 0, tzinfo=UTC)
    bot.run_once()  # flat
    assert not bot.state.positions
    bot._now = lambda: datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    bot.state.last_candle_ts.clear()
    bot.run_once()
    assert not bot.state.positions, "Limit von 1 Einstieg pro Tag erreicht"
    bot._now = lambda: datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
    bot.state.last_candle_ts.clear()
    bot.run_once()
    assert "BTC/USDT" in bot.state.positions and bot.state.entries_today == 1


def test_startup_message_mentions_daytrading(settings, tmp_path):
    bot, notifier = _bot_with_session(settings, tmp_path, _dt(), datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    bot.stop()
    bot.run_forever()
    assert any("Tageshandel 08:00-20:00" in m for m in notifier.messages)
