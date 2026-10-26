from types import SimpleNamespace

from tradingbot.insights import (
    CryptoScanner,
    NewsDirectionGuard,
    alpaca_crypto_symbol,
    crypto_most_active,
    headline_sentiment,
    news_direction,
)


def test_headline_sentiment_and_direction():
    assert headline_sentiment("Bitcoin surges to record high as ETF inflows jump") > 0
    assert headline_sentiment("Exchange hacked, Solana plunges amid liquidations") < 0
    assert headline_sentiment("Fed meeting scheduled for Wednesday") == 0
    assert news_direction(["Bitcoin surges", "ETH rallies"]) == ("long", 2)
    assert news_direction(["SEC lawsuit hits Coinbase, shares tumble"])[0] == "short"
    assert news_direction(["Bitcoin surges", "Bitcoin drops"])[0] is None


def test_news_guard_allows_only_news_direction():
    calls = []

    def fetch(symbol, minutes):
        calls.append((symbol, minutes))
        return ["Tesla beats earnings, stock jumps"] if symbol == "TSLA" else []

    guard = NewsDirectionGuard(fetch, 30)
    assert guard("TSLA", "long") == (True, "")
    ok, why = guard("TSLA", "short")
    assert not ok and "News sprechen fuer Long" in why
    assert guard("AAPL", "short") == (True, ""), "ohne News: alles erlaubt"
    assert len(calls) == 2, "Ergebnis je Paar zwischengespeichert"
    assert alpaca_crypto_symbol("BTC/USDT:USDT") == "BTCUSD"


def test_crypto_scanner_picks_most_active_perps():
    fut = SimpleNamespace(fetch_tickers=lambda: {
        "BTC/USDT:USDT": {"quoteVolume": 9e9}, "SUI/USDT:USDT": {"quoteVolume": 5e9}, "USDC/USDT:USDT": {"quoteVolume": 8e9},
        "NOSPOT/USDT:USDT": {"quoteVolume": 7e9}, "ETH/USDT:USDT": {"quoteVolume": 4e9}, "BTC/USDC:USDC": {"quoteVolume": 1e10},
    })
    spot = SimpleNamespace(load_markets=lambda: {"BTC/USDT": {}, "SUI/USDT": {}, "ETH/USDT": {}})
    assert crypto_most_active(fut, spot, 2) == ["BTC/USDT:USDT", "SUI/USDT:USDT"]
    bot = SimpleNamespace(cfg=SimpleNamespace(symbols=["ETH/USDT:USDT"], quote_currency="USDT"),
                          state=SimpleNamespace(positions={"DOGE/USDT:USDT": object()}))
    scan = CryptoScanner(bot, fut, spot, 2)
    scan()
    assert bot.cfg.symbols == ["ETH/USDT:USDT", "BTC/USDT:USDT", "SUI/USDT:USDT", "DOGE/USDT:USDT"]
    bot.cfg.symbols = ["X"]
    scan()
    assert bot.cfg.symbols == ["X"], "erst nach 6 Stunden wieder"


def test_crypto_scanner_keeps_saved_settings_unchanged(settings):
    from tradingbot.config import BotConfig

    cfg = BotConfig(market="futures", symbols=["ETH/USDT"])
    bot = SimpleNamespace(cfg=cfg, state=SimpleNamespace(positions={}))
    fut = SimpleNamespace(fetch_tickers=lambda: {"SUI/USDT:USDT": {"quoteVolume": 5e9}})
    CryptoScanner(bot, fut, None, 1)()
    assert bot.cfg.symbols == ["ETH/USDT:USDT", "SUI/USDT:USDT"] and cfg.symbols == ["ETH/USDT:USDT"]
