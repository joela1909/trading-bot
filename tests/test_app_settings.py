import json
import urllib.error
import urllib.request

import pytest
import yaml

from tradingbot import app_settings
from tradingbot.config import load_settings
from tradingbot.dashboard import ApiError, DashboardServer

BASE = {
    "mode": "paper",
    "exchange": "binance",
    "market": "futures",
    "symbols": ["BTC/USDT", "ETH/USDT"],
    "timeframe": "1m",
    "strategy": {"name": "vwap_reversion", "allow_short": True, "vwap_dev_atr": 3.0},
    "daytrading": {"enabled": True, "timezone": "Europe/Berlin", "entry_start": "00:00", "entry_end": "23:50", "flat_time": "24:00"},
}


def _payload(**over):
    p = {
        "symbols": ["BTC/USDT", "SOL/USDT"],
        "timeframe": "5m",
        "strategy": {"name": "ensemble", "min_votes": 3, "members": None, "allow_short": True},
        "risk": {"risk_per_trade_pct": 0.5, "max_position_pct": 20, "max_open_positions": 2, "max_daily_loss_pct": 2},
        "daytrading": {"enabled": True, "entry_start": "08:00", "entry_end": "20:00", "flat_time": "21:00", "max_trades_per_day": 4},
    }
    p.update(over)
    return p


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRADINGBOT_STATE_URL", raising=False)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    settings = load_settings(tmp_path / "config.yaml")
    return tmp_path, settings


def test_save_and_reload_with_override(setup):
    tmp, settings = setup
    app_settings.validate_and_save(settings, BASE, _payload())
    reloaded = load_settings(tmp / "config.yaml")
    b = reloaded.bot
    assert b.symbols == ["BTC/USDT:USDT", "SOL/USDT:USDT"] and b.timeframe == "5m"
    assert b.strategy.name == "ensemble" and b.strategy.min_votes == 3 and b.strategy.vwap_dev_atr == 3.0, "Rest bleibt aus config.yaml"
    assert b.risk.max_open_positions == 2 and b.daytrading.flat_time == "21:00"
    assert b.mode == "paper" and b.market == "futures"
    cur = app_settings.current(reloaded, True)
    assert cur["values"]["strategy"]["min_votes"] == 3 and cur["locked"]["market"] == "futures"
    assert "ensemble" in cur["options"]["strategies"] and "ensemble" not in cur["options"]["members"]


@pytest.mark.parametrize("change, message", [
    ({"symbols": ["BTC-USDT"]}, "kein gueltiges Paar"),
    ({"symbols": []}, "symbols"),
    ({"timeframe": "3m"}, "timeframe"),
    ({"strategy": {"name": "gibt_es_nicht", "min_votes": 2, "members": None, "allow_short": False}}, "Unbekannte Strategie"),
    ({"strategy": {"name": "ensemble", "min_votes": 2, "members": ["ensemble"], "allow_short": False}}, "Unbekannte Strategie"),
    ({"risk": {"risk_per_trade_pct": 50, "max_position_pct": 20, "max_open_positions": 2, "max_daily_loss_pct": 2}}, "risk_per_trade_pct"),
    ({"daytrading": {"enabled": True, "entry_start": "20:00", "entry_end": "08:00", "flat_time": "21:00", "max_trades_per_day": 4}}, "entry_start"),
])
def test_invalid_input_is_rejected_and_nothing_saved(setup, change, message):
    tmp, settings = setup
    with pytest.raises(ValueError, match=message):
        app_settings.validate_and_save(settings, BASE, _payload(**change))
    assert not app_settings.override_path(settings.bot.state_dir).exists()


def test_mode_and_keys_cannot_be_changed(setup):
    tmp, settings = setup
    for field in ({"mode": "live"}, {"exchange": "bitget"}, {"market": "spot"}):
        with pytest.raises(ValueError):
            app_settings.validate_and_save(settings, BASE, _payload(**field))


def test_short_on_spot_is_rejected(setup):
    tmp, settings = setup
    spot = {**BASE, "market": "spot", "strategy": {"name": "vwap_reversion"}}
    with pytest.raises(ValueError, match="futures"):
        app_settings.validate_and_save(settings, spot, _payload())


def test_broken_override_falls_back_to_config(setup):
    tmp, settings = setup
    path = app_settings.override_path(settings.bot.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"strategy": {"name": "ema_cross", "allow_short": True}, "market": "spot"}), encoding="utf-8")
    assert load_settings(tmp / "config.yaml").bot.strategy.name == "vwap_reversion", "Bot startet trotzdem"


def test_reset_removes_override(setup):
    tmp, settings = setup
    app_settings.validate_and_save(settings, BASE, _payload())
    app_settings.reset(settings)
    assert load_settings(tmp / "config.yaml").bot.timeframe == "1m"


def test_settings_endpoint_over_http(setup):
    tmp, settings = setup
    saved = []

    def save(body):
        try:
            app_settings.validate_and_save(settings, BASE, body)
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        saved.append(True)
        return {"ok": True, "restart": True}

    srv = DashboardServer(lambda: {}, port=0, queries={"settings": lambda _p: app_settings.current(settings, False)}, commands={"settings": save})
    srv.start()
    try:
        def post(body, headers=None):
            req = urllib.request.Request(srv.url + "/api/settings", method="POST", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        assert post(_payload())[0] == 403, "ohne X-Dashboard-Header kein Speichern"
        assert post(_payload(), {"X-Dashboard": "1", "Origin": "https://evil.example"})[0] == 403
        status, body = post(_payload(timeframe="7m"), {"X-Dashboard": "1"})
        assert status == 400 and "timeframe" in body["error"] and not saved
        status, body = post(_payload(), {"X-Dashboard": "1"})
        assert status == 200 and body == {"ok": True, "restart": True} and saved
        with urllib.request.urlopen(srv.url + "/api/settings", timeout=5) as r:
            assert json.loads(r.read())["values"]["symbols"] == ["BTC/USDT", "ETH/USDT"]
    finally:
        srv.stop()


def test_bot_restart_request_sets_exit_code(settings, tmp_path):
    from tests.test_bot import _make_bot
    from tests.conftest import FakeFeed, make_ohlcv, trend_closes

    bot, _, _ = _make_bot(settings, FakeFeed(make_ohlcv(trend_closes())), tmp_path)
    bot.request_restart()
    assert bot.exit_code == 3 and bot._stop


def test_solo_member_and_trailing_pct_are_saved(setup):
    tmp, settings = setup
    strat = {"name": "ensemble", "min_votes": 3, "members": None, "allow_short": True, "solo_member": "opening_range", "trailing_pct": 1.0}
    app_settings.validate_and_save(settings, BASE, _payload(strategy=strat))
    b = load_settings(tmp / "config.yaml").bot
    assert b.strategy.solo_member == "opening_range" and b.strategy.trailing_pct == 1.0
    cur = app_settings.current(load_settings(tmp / "config.yaml"), True)["values"]["strategy"]
    assert cur["solo_member"] == "opening_range" and cur["trailing_pct"] == 1.0
    bad = dict(strat, members=["ema_cross", "donchian"])
    with pytest.raises(ValueError, match="kein Mitglied"):
        app_settings.validate_and_save(settings, BASE, _payload(strategy=bad))


def test_solo_member_can_be_switched_off_against_config(setup):
    tmp, settings = setup
    base = {**BASE, "strategy": {**BASE["strategy"], "solo_member": "opening_range"}}
    strat = {"name": "ensemble", "min_votes": 3, "members": None, "allow_short": True, "solo_member": None}
    app_settings.validate_and_save(settings, base, _payload(strategy=strat))
    (tmp / "config.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    assert load_settings(tmp / "config.yaml").bot.strategy.solo_member is None


def test_leverage_up_to_100(setup):
    tmp, settings = setup
    app_settings.validate_and_save(settings, BASE, _payload(futures={"leverage": 100}))
    reloaded = load_settings(tmp / "config.yaml")
    assert reloaded.bot.leverage == 100
    assert app_settings.current(reloaded, True)["values"]["futures"]["leverage"] == 100
    with pytest.raises(ValueError, match="leverage"):
        app_settings.validate_and_save(settings, BASE, _payload(futures={"leverage": 101}))


def test_stock_settings_are_separate_from_crypto(setup):
    tmp, settings = setup
    app_settings.validate_and_save(settings, BASE, _payload())  # Krypto
    stock = _payload(symbols=["aapl", "NVDA"], timeframe="15m", futures={"leverage": 5},
                     strategy={"name": "ensemble", "min_votes": 2, "members": None, "allow_short": True, "solo_member": None, "trailing_pct": 0.5})
    app_settings.validate_and_save(settings, BASE, stock, market="stocks")
    reloaded = load_settings(tmp / "config.yaml")
    assert reloaded.bot.symbols == ["BTC/USDT:USDT", "SOL/USDT:USDT"] and reloaded.bot.timeframe == "5m", "Krypto unveraendert"
    cur = app_settings.current(reloaded, market="stocks")
    v = cur["values"]
    assert v["symbols"] == ["AAPL", "NVDA"] and v["timeframe"] == "15m" and v["futures"]["leverage"] == 5
    assert v["strategy"]["min_votes"] == 2 and v["strategy"]["trailing_pct"] == 0.5
    assert cur["options"]["symbol_kind"] == "ticker" and cur["locked"]["market"] == "stocks" and cur["has_override"]
    assert app_settings.current(reloaded)["values"]["strategy"]["min_votes"] == 3
    # Krypto erneut speichern laesst Aktien stehen
    app_settings.validate_and_save(settings, BASE, _payload(timeframe="1m"))
    assert app_settings.current(load_settings(tmp / "config.yaml"), market="stocks")["values"]["symbols"] == ["AAPL", "NVDA"]
    with pytest.raises(ValueError, match="Tickersymbol"):
        app_settings.validate_and_save(settings, BASE, _payload(symbols=["BTC/USDT"]), market="stocks")
    # Zuruecksetzen nur fuer Aktien
    app_settings.reset(settings, market="stocks")
    r = load_settings(tmp / "config.yaml")
    assert r.bot.timeframe == "1m" and app_settings.current(r, market="stocks")["values"]["symbols"] != ["AAPL", "NVDA"]
    assert not app_settings.current(r, market="stocks")["has_override"]


def test_stoploss_guard_can_be_switched_off_per_market(setup):
    tmp, settings = setup
    app_settings.validate_and_save(settings, BASE, _payload(protections={"stoploss_guard_stops": 0}))
    r = load_settings(tmp / "config.yaml")
    assert r.bot.protections.stoploss_guard_stops == 0
    assert app_settings.current(r)["values"]["protections"]["stoploss_guard_stops"] == 0
    assert app_settings.current(r, market="stocks")["values"]["protections"]["stoploss_guard_stops"] == 0, "Aktien erben ohne eigene Wahl"
    app_settings.validate_and_save(settings, BASE, _payload(symbols=["AAPL"], protections={"stoploss_guard_stops": 3}), market="stocks")
    r = load_settings(tmp / "config.yaml")
    assert r.bot.protections.stoploss_guard_stops == 0
    assert app_settings.current(r, market="stocks")["values"]["protections"]["stoploss_guard_stops"] == 3


def test_disabled_guard_lifts_running_lock():
    from tradingbot.config import ProtectionConfig
    from tradingbot.protections import Protections

    p = Protections(ProtectionConfig(stoploss_guard_stops=0), 60_000)
    p.load({"locked_until": 10**15, "lock_reason": "Stop-Loss-Serie"})
    assert p.entry_allowed("X", 10**12)[0]


def test_cooldown_can_be_switched_off(setup):
    tmp, settings = setup
    app_settings.validate_and_save(settings, BASE, _payload(protections={"stoploss_guard_stops": 3, "cooldown_candles": 0}))
    r = load_settings(tmp / "config.yaml")
    assert r.bot.protections.cooldown_candles == 0
    assert app_settings.current(r)["values"]["protections"]["cooldown_candles"] == 0
    # ohne Angabe bleibt der Wert aus config.yaml
    app_settings.validate_and_save(settings, BASE, _payload(protections={"stoploss_guard_stops": 3}))
    assert load_settings(tmp / "config.yaml").bot.protections.cooldown_candles == 2


def test_exits_per_strategy_are_saved_and_cleared(setup):
    tmp, settings = setup
    strat = {"name": "ensemble", "min_votes": 2, "members": None, "allow_short": True,
             "exits": {"rsi_reversion": {"stop_atr_mult": 1.5, "take_profit_atr_mult": 2.0, "min_stop_pct": None}}}
    app_settings.validate_and_save(settings, BASE, _payload(strategy=strat))
    b = load_settings(tmp / "config.yaml").bot
    assert b.strategy.exits["rsi_reversion"].stop_atr_mult == 1.5 and b.strategy.exits["rsi_reversion"].min_stop_pct is None
    cur = app_settings.current(load_settings(tmp / "config.yaml"), True)
    assert cur["values"]["strategy"]["exits"]["rsi_reversion"]["take_profit_atr_mult"] == 2.0
    assert "supertrend_ai" in cur["options"]["own_exits"]
    bad = dict(strat, exits={"gibt_es_nicht": {"stop_atr_mult": 1.0}})
    with pytest.raises(ValueError, match="unbekannte Strategie"):
        app_settings.validate_and_save(settings, BASE, _payload(strategy=bad))
