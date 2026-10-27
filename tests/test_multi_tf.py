from tradingbot.multi_tf import MultiBot, _merge_history, ordered_timeframes, settings_for
from tests.conftest import FakeFeed, make_ohlcv, trend_closes
from tests.test_bot import _make_bot


def test_timeframes_sorted_and_settings_separate(settings):
    assert ordered_timeframes("15m", ["4h", "1m", "15m", "5m"]) == ["1m", "5m", "15m", "4h"]
    s = settings_for(settings, "4h")
    assert s.bot.timeframe == "4h" and settings.bot.timeframe != "4h"
    s.bot.risk.risk_per_trade_pct = 9.0
    assert settings.bot.risk.risk_per_trade_pct != 9.0, "eigene Kopie je Zeitrahmen"


def test_merge_history_sums_last_known_values():
    a = [["2026-10-27T10:00:00+00:00", 100.0], ["2026-10-27T12:00:00+00:00", 110.0]]
    b = [["2026-10-27T11:00:00+00:00", 50.0]]
    assert _merge_history([a, b]) == [["2026-10-27T10:00:00+00:00", 150.0], ["2026-10-27T11:00:00+00:00", 150.0],
                                      ["2026-10-27T12:00:00+00:00", 160.0]]


def test_multibot_sums_accounts_and_routes_chart(settings, tmp_path):
    bots = {}
    for tf in ("1h", "4h"):
        d = tmp_path / tf
        d.mkdir()
        bots[tf], _, _ = _make_bot(settings_for(settings, tf), FakeFeed(make_ohlcv(trend_closes())), d)
    m = MultiBot(bots, "1h")
    for b in bots.values():
        b.run_once()
    snap = m.get_snapshot()
    assert snap["timeframes"] == ["1h", "4h"] and len(snap["accounts"]) == 2
    assert abs(snap["equity"] - sum(b.get_snapshot()["equity"] for b in bots.values())) < 1e-6
    assert all(p["timeframe"] in ("1h", "4h") for p in snap["positions"])
    m.pause()
    assert all(b.state.entries_paused for b in bots.values())
    assert m.cfg is bots["1h"].cfg and m.timeframes == ["1h", "4h"]
    assert m.chart("BTC/USDT", "4h")["timeframe"] == "4h"
