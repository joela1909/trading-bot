from datetime import datetime, timezone

import pytest

from tradingbot.alpaca import AlpacaDataFeed, AlpacaError, us_market_open
from tradingbot.config import Secrets, Settings, BotConfig
from tradingbot.stocks import StockRunner, build_stock_bot, stock_settings
from tests.conftest import FakeFeed, make_ohlcv, trend_closes


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, pages, status=200):
        self.headers, self.calls, self.pages, self.status = {}, [], list(pages), status

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if "trades/latest" in url:
            return FakeResponse(self.status, {"trade": {"p": 187.5}})
        return FakeResponse(self.status, self.pages.pop(0))


def _bar(minute):
    return {"t": f"2026-10-20T14:{minute:02d}:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}


def test_alpaca_bars_paginate_and_price():
    sess = FakeSession([{"bars": [_bar(0), _bar(5)], "next_page_token": "abc"}, {"bars": [_bar(10)], "next_page_token": None}])
    feed = AlpacaDataFeed("k", "s", session=sess)
    df = feed.fetch_chart("AAPL", "5m", 10)
    assert list(df["close"]) == [1.5, 1.5, 1.5] and len(sess.calls) == 2
    assert sess.calls[0][1]["timeframe"] == "5Min" and sess.calls[0][1]["feed"] == "iex"
    assert sess.calls[1][1]["page_token"] == "abc"
    assert sess.headers["APCA-API-KEY-ID"] == "k"
    assert feed.fetch_price("AAPL") == 187.5
    assert feed.market_limits("AAPL").amount_precision == 0


def test_alpaca_rejects_bad_keys_and_missing_keys():
    with pytest.raises(AlpacaError, match="Schluessel"):
        AlpacaDataFeed("", "")
    feed = AlpacaDataFeed("k", "s", session=FakeSession([], status=403))
    with pytest.raises(AlpacaError, match="lehnt"):
        feed.check()


def test_market_hours():
    assert us_market_open(datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc))      # Di 10:00 New York
    assert not us_market_open(datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))  # 9:00
    assert not us_market_open(datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc))  # Samstag


def _settings(tmp_path, **stocks):
    bot = BotConfig(market="futures", symbols=["BTC/USDT"], timeframe="1m", state_dir=str(tmp_path / "state"),
                    log_dir=str(tmp_path / "logs"), strategy={"name": "ensemble", "allow_short": True, "min_votes": 3},
                    stocks={"enabled": True, **stocks})
    return Settings(bot=bot, secrets=Secrets(_env_file=None))


def test_stock_settings_are_paper_usd_newyork(tmp_path):
    s = stock_settings(_settings(tmp_path, symbols=["aapl", "MSFT"]))
    b = s.bot
    assert s.mode == "paper" and b.market == "stocks" and b.quote_currency == "USD" and b.leverage == 1.0
    assert b.symbols == ["AAPL", "MSFT"] and b.timeframe == "5m"
    assert b.daytrading.timezone == "America/New_York" and b.daytrading.flat_time == "15:55"
    assert b.strategy.orb_sessions == ["09:30 America/New_York"] and b.strategy.allow_short
    assert b.strategy.min_votes == 3, "Rest der Strategie wie beim Krypto-Bot"
    assert not b.stocks.enabled


def test_stock_bot_runs_with_own_state(tmp_path):
    settings = _settings(tmp_path, symbols=["AAPL"])
    feed = FakeFeed(make_ohlcv(trend_closes()))
    bot = build_stock_bot(settings, feed=feed, chart_feed=feed)
    bot.run_once()
    snap = bot.build_snapshot()
    assert snap["quote"] == "USD" and snap["symbols"] == ["AAPL"]
    assert (tmp_path / "state" / "state_paper_stocks.json").exists()
    assert bot.broker.balance_quote() == pytest.approx(10000.0)


def test_runner_without_keys_reports_problem(tmp_path):
    r = StockRunner(_settings(tmp_path))
    r.start()
    snap = r.snapshot()
    assert not snap["ready"] and "Alpaca" in snap["problem"] and snap["enabled"]


def test_scanner_and_news_from_alpaca():
    sess = FakeSession([{"most_actives": [{"symbol": "NVDA"}, {"symbol": "BRK.B"}, {"symbol": "ABR$D"}, {"symbol": "TSLA"}]},
                        {"news": [{"headline": "Tesla beats earnings"}]}])
    feed = AlpacaDataFeed("k", "s", session=sess)
    assert feed.most_actives(2) == ["NVDA", "BRK.B"]
    assert "screener/stocks/most-actives" in sess.calls[0][0]
    assert feed.recent_news("TSLA", 30) == ["Tesla beats earnings"]
    assert sess.calls[-1][1]["symbols"] == "TSLA" and "/v1beta1/news" in sess.calls[-1][0]


def test_entry_guard_blocks_entry(settings, tmp_path):
    from tests.test_bot import _first_buy_index, _make_bot

    full = make_ohlcv(trend_closes())
    from tradingbot.strategy import build_strategy

    buy_i = _first_buy_index(build_strategy(settings.bot.strategy), full)
    feed = FakeFeed(full.iloc[: buy_i + 1].reset_index(drop=True))
    bot, _, _ = _make_bot(settings, feed, tmp_path)
    bot.entry_guard = lambda s, side: (False, "News sprechen fuer Short (-2): x")
    bot.run_once()
    assert not bot.state.positions and "News sprechen" in bot._status["BTC/USDT"]
