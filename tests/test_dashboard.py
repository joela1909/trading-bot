import json
import urllib.error
import urllib.request

import pytest

from tradingbot.dashboard import DashboardServer
from tradingbot.models import Position, Trade
from tradingbot.snapshot import compute_stats, downsample, make_snapshot
from tradingbot.state import BotState


def _trade(pnl):
    return Trade("BTC/USDT", 1.0, 100.0, 100.0 + pnl, pnl, pnl, 0.5, "2024-01-01T00:00:00+00:00", "2024-01-02T00:00:00+00:00", "signal")


def _snapshot(state=None, **kw):
    args = dict(state=state or BotState(), mode="paper", exchange="binance", timeframe="1h", quote="USDT", symbols=["BTC/USDT"],
                equity=1000.0, free_quote=900.0, prices={}, last_cycle=None, running=True, controls=True)
    args.update(kw)
    return make_snapshot(**args)


def test_stats():
    stats = compute_stats([_trade(10), _trade(-5), _trade(5)])
    assert stats["n_trades"] == 3 and stats["wins"] == 2
    assert stats["win_rate_pct"] == pytest.approx(66.666, abs=0.01)
    assert stats["total_pnl"] == 10 and stats["profit_factor"] == 3.0
    empty = compute_stats([])
    assert empty["win_rate_pct"] is None and empty["total_pnl"] == 0


def test_downsample_keeps_ends():
    pts = [[str(i), float(i)] for i in range(2000)]
    out = downsample(pts, 100)
    assert len(out) == 100 and out[0] == pts[0] and out[-1] == pts[-1]
    assert downsample(pts[:10], 100) == pts[:10]


def test_snapshot_positions_and_returns():
    state = BotState()
    state.positions["BTC/USDT"] = Position("BTC/USDT", 2.0, 100.0, 90.0, 120.0, "2024-01-01T00:00:00+00:00", 100.0, entry_fee=1.0)
    state.equity_history = [["2024-01-01T00:00:00+00:00", 1000.0], ["2024-01-02T00:00:00+00:00", 1100.0]]
    snap = _snapshot(state, equity=1100.0, prices={"BTC/USDT": 105.0})
    pos = snap["positions"][0]
    assert pos["pnl_quote"] == pytest.approx(9.0)
    assert snap["return_pct"] == pytest.approx(10.0)
    # ohne bekannten Kurs: keine erfundenen Werte
    assert _snapshot(state)["positions"][0]["pnl_quote"] is None
    # ohne Equity-Angabe faellt die Ansicht auf den letzten gespeicherten Wert zurueck
    assert _snapshot(state, equity=None)["equity"] == 1100.0


def test_snapshot_is_json_serialisable_and_newest_trades_first():
    state = BotState(trades=[_trade(1), _trade(2)])
    snap = _snapshot(state)
    json.dumps(snap)
    assert snap["trades"][0]["pnl_quote"] == 2


@pytest.fixture
def server():
    calls = []
    srv = DashboardServer(lambda: {"ok": True}, {"pause": lambda: calls.append("pause") or {"paused": True}}, port=0)
    srv.start()
    srv.calls = calls
    yield srv
    srv.stop()


def _request(srv, path, method="GET", headers=None):
    req = urllib.request.Request(srv.url + path, method=method, headers=headers or {}, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_serves_page_assets_and_snapshot(server):
    status, body, headers = _request(server, "/")
    assert status == 200 and b"Trading Bot" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert _request(server, "/app.js")[0] == 200
    assert _request(server, "/style.css")[0] == 200
    status, body, _ = _request(server, "/api/snapshot")
    assert status == 200 and json.loads(body) == {"ok": True}
    assert _request(server, "/nope")[0] == 404


def test_refuses_non_loopback_bind():
    with pytest.raises(ValueError):
        DashboardServer(lambda: {}, host="0.0.0.0", port=0)


def test_rejects_foreign_host_header(server):
    status, _, _ = _request(server, "/api/snapshot", headers={"Host": "evil.example"})
    assert status == 403


def test_post_requires_custom_header_and_local_origin(server):
    assert _request(server, "/api/pause", "POST")[0] == 403
    assert _request(server, "/api/pause", "POST", {"X-Dashboard": "1", "Origin": "https://evil.example"})[0] == 403
    assert server.calls == []
    status, body, _ = _request(server, "/api/pause", "POST", {"X-Dashboard": "1", "Origin": server.url})
    assert status == 200 and json.loads(body) == {"paused": True}
    assert server.calls == ["pause"]
    assert _request(server, "/api/unknown", "POST", {"X-Dashboard": "1"})[0] == 404


def test_pause_button_flow_end_to_end(settings, tmp_path):
    from tradingbot.bot import TradingBot
    from tradingbot.exchange import PaperBroker
    from tradingbot.notify import Notifier
    from tradingbot.risk import RiskManager
    from tradingbot.strategy import build_strategy
    from tests.conftest import FakeFeed, make_ohlcv, trend_closes

    feed = FakeFeed(make_ohlcv(trend_closes()))
    broker = PaperBroker(feed, "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "bal.json")
    bot = TradingBot(settings, feed, broker, build_strategy(settings.bot.strategy), RiskManager(settings.bot.risk), BotState(), Notifier(), tmp_path / "state.json")
    srv = DashboardServer(bot.get_snapshot, {"pause": bot.pause, "resume": bot.resume}, port=0)
    srv.start()
    try:
        assert json.loads(_request(srv, "/api/snapshot")[1])["paused"] is False
        headers = {"X-Dashboard": "1"}
        assert json.loads(_request(srv, "/api/pause", "POST", headers)[1])["paused"] is True
        assert bot.state.entries_paused
        assert json.loads(_request(srv, "/api/snapshot")[1])["paused"] is True
        assert json.loads(_request(srv, "/api/resume", "POST", headers)[1])["paused"] is False
    finally:
        srv.stop()
