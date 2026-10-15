import pytest

from tradingbot.config import BotConfig, Secrets, Settings, StrategyConfig
from tradingbot.data import drop_incomplete_candle, load_csv, timeframe_seconds
from tests.conftest import START_TS, TF_MS, make_ohlcv


def test_live_requires_confirmation_and_keys():
    s = Settings(bot=BotConfig(mode="live"), secrets=Secrets(_env_file=None))
    with pytest.raises(RuntimeError, match="LIVE_TRADING_CONFIRMED"):
        s.validate_for_run()
    s = Settings(bot=BotConfig(mode="live"), secrets=Secrets(_env_file=None, live_trading_confirmed=True))
    with pytest.raises(RuntimeError, match="API_KEY"):
        s.validate_for_run()
    s = Settings(
        bot=BotConfig(mode="live"),
        secrets=Secrets(_env_file=None, live_trading_confirmed=True, exchange_api_key="k", exchange_api_secret="s"),
    )
    s.validate_for_run()


def test_env_mode_overrides_yaml():
    s = Settings(bot=BotConfig(mode="live"), secrets=Secrets(_env_file=None, trading_mode="paper"))
    assert s.mode == "paper"
    s.validate_for_run()


def test_symbol_quote_mismatch():
    s = Settings(bot=BotConfig(symbols=["BTC/EUR"], quote_currency="USDT"), secrets=Secrets(_env_file=None))
    with pytest.raises(RuntimeError, match="quote_currency"):
        s.validate_for_run()


def test_strategy_config_validation():
    with pytest.raises(ValueError):
        StrategyConfig(fast_ema=50, slow_ema=20)


def test_timeframe_seconds():
    assert timeframe_seconds("1m") == 60
    assert timeframe_seconds("4h") == 14400
    assert timeframe_seconds("1d") == 86400
    with pytest.raises(ValueError):
        timeframe_seconds("1x")


def test_drop_incomplete_candle():
    df = make_ohlcv([1.0, 2.0, 3.0])
    last_open = START_TS + 2 * TF_MS
    assert len(drop_incomplete_candle(df, "1h", now_ms=last_open + TF_MS // 2)) == 2
    assert len(drop_incomplete_candle(df, "1h", now_ms=last_open + TF_MS)) == 3


def test_load_csv_with_iso_and_ms_timestamps(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("timestamp,open,high,low,close,volume\n2024-01-01T00:00:00Z,1,2,0.5,1.5,10\n2024-01-01T01:00:00Z,1.5,2,1,1.8,10\n")
    df = load_csv(p)
    assert list(df["close"]) == [1.5, 1.8]
    assert df["timestamp"].iloc[1] - df["timestamp"].iloc[0] == TF_MS
    p2 = tmp_path / "b.csv"
    p2.write_text(f"Timestamp,Open,High,Low,Close,Volume\n{START_TS},1,2,0.5,1.5,10\n")
    assert load_csv(p2)["timestamp"].iloc[0] == START_TS
