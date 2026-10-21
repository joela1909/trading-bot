import numpy as np
import pytest

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.cli import main
from tradingbot.config import StrategyConfig
from tradingbot.strategy import REGISTRY, build_strategy
from tradingbot.strategy.base import Strategy
from tradingbot.synthetic import make_series


class CheatingStrategy(Strategy):
    """Kauft, wenn die NAECHSTE Kerze hoeher schliesst. Das ist Blick in die Zukunft."""

    name = "cheat"

    @property
    def warmup_bars(self):
        return 30

    def _add_indicators(self, df):
        pass

    def signals(self, df):
        buy = (df["close"].shift(-1) > df["close"]).fillna(False).to_numpy() & (df["atr"] > 0).to_numpy()
        return buy, ~buy


class UnstableStrategy(Strategy):
    """Haengt vom Startpunkt der Reihe ab: Signal, wenn der Kurs ueber dem Durchschnitt ALLER bisherigen Kerzen liegt."""

    name = "unstable"

    @property
    def warmup_bars(self):
        return 30

    def _add_indicators(self, df):
        df["expanding_mean"] = df["close"].expanding().mean()

    def signals(self, df):
        above = (df["close"] > df["expanding_mean"]).to_numpy()
        cross = above & ~np.roll(above, 1)
        return cross & (df["atr"] > 0).to_numpy(), ~above & np.roll(above, 1)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_registered_strategies_are_causal_and_live_consistent(name):
    strat = build_strategy(StrategyConfig(name=name))
    df = make_series("trend", 4000, 4)
    look = check_lookahead(strat, df)
    assert look.ok and look.checked > 10, look
    rec = check_recursive(strat, df)
    assert rec.ok, f"{name}: {rec.mismatch_pct:.1f}% Abweichung"


def test_lookahead_is_detected():
    look = check_lookahead(CheatingStrategy(StrategyConfig()), make_series("random", 1500, 2))
    assert not look.ok and look.mismatches


def test_startup_dependence_is_detected():
    rec = check_recursive(UnstableStrategy(StrategyConfig()), make_series("trend", 3000, 3))
    assert not rec.ok and rec.mismatch_pct > 2.0


def test_cli_check_demo_ok_and_unknown(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"log_dir: {tmp_path / 'logs'}\n")
    assert main(["--config", str(cfg), "check", "--demo", "trend", "--demo-bars", "3000", "--strategies", "ema_cross,donchian"]) == 0
    out = capsys.readouterr().out
    assert "ema_cross" in out and "ok" in out
    assert main(["--config", str(cfg), "check", "--demo", "trend", "--strategies", "nope"]) == 2


def test_feed_paginates_when_more_than_one_request_is_needed():
    import time

    from tests.test_strategies_and_research import FakeExchange
    from tradingbot.data import CcxtDataFeed, timeframe_seconds

    tf_ms = timeframe_seconds("1h") * 1000
    now = int(time.time() * 1000)
    df = make_series("trend", 2600, 5, start_ts=(now // tf_ms - 2600) * tf_ms)
    feed = CcxtDataFeed("binance", ccxt_exchange=FakeExchange(df))
    out = feed.fetch_ohlcv("BTC/USDT", "1h", limit=1600)
    assert len(out) == 1600 and out["timestamp"].is_monotonic_increasing
