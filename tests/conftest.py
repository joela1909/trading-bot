from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import BotConfig, Secrets, Settings, StrategyConfig
from tradingbot.models import MarketLimits

TF_MS = 3_600_000
START_TS = 1_700_000_000_000


def make_ohlcv(closes, start_ts: int = START_TS, tf_ms: int = TF_MS) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    ts = start_ts + np.arange(len(closes)) * tf_ms
    return pd.DataFrame(
        {"timestamp": ts.astype("int64"), "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000.0}
    )


def trend_closes(n_down: int = 40, n_up: int = 40, n_down2: int = 40) -> np.ndarray:
    down = np.linspace(100, 80, n_down)
    up = np.linspace(80, 120, n_up)
    down2 = np.linspace(120, 90, n_down2)
    return np.concatenate([down, up, down2])


class FakeFeed:
    def __init__(self, df: pd.DataFrame, price: float | None = None):
        self.df = df
        self.price = price if price is not None else float(df["close"].iloc[-1])
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls += 1
        return self.df.tail(limit).reset_index(drop=True)

    def fetch_price(self, symbol):
        return self.price

    def market_limits(self, symbol):
        return MarketLimits(min_amount=0.0001, min_cost=5.0, amount_precision=6)


@pytest.fixture
def small_strategy_cfg() -> StrategyConfig:
    return StrategyConfig(fast_ema=5, slow_ema=10, rsi_period=5, rsi_max_entry=95, atr_period=5, stop_atr_mult=2.0, take_profit_atr_mult=4.0)


@pytest.fixture
def settings(small_strategy_cfg, tmp_path) -> Settings:
    bot = BotConfig(
        symbols=["BTC/USDT"],
        quote_currency="USDT",
        timeframe="1h",
        state_dir=str(tmp_path / "state"),
        log_dir=str(tmp_path / "logs"),
        strategy=small_strategy_cfg,
    )
    return Settings(bot=bot, secrets=Secrets(_env_file=None))
