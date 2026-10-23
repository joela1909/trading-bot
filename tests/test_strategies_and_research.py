import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest import Backtester
from tradingbot.cli import main
from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
from tradingbot.models import Signal
from tradingbot.research import expand_grid, format_report, grid_dataframe, run_comparison
from tradingbot.risk import RiskManager
from tradingbot.strategy import EXIT_GRID, REGISTRY, build_strategy
from tradingbot.synthetic import make_series

ALL = sorted(REGISTRY)


def _mixed_series() -> pd.DataFrame:
    trend = make_series("trend", 3000, 5)
    rev = make_series("meanrev", 3000, 6, start_price=float(trend["close"].iloc[-1]), start_ts=int(trend["timestamp"].iloc[-1]) + 3_600_000)
    return pd.concat([trend, rev], ignore_index=True)


@pytest.mark.parametrize("name", ALL)
def test_signals_shape_dtype_and_warmup(name):
    strat = build_strategy(StrategyConfig(name=name))
    df = strat.prepare(make_series("trend", 1500, 3))
    buy, sell = strat.signals(df)
    assert len(buy) == len(sell) == len(df)
    assert buy.dtype == bool and sell.dtype == bool
    assert not buy[:10].any() and not sell[:10].any()


@pytest.mark.parametrize("name", ALL)
def test_each_strategy_produces_valid_buy_decision(name):
    strat = build_strategy(StrategyConfig(name=name, take_profit_atr_mult=4.0))
    df = strat.prepare(_mixed_series())
    buy, _ = strat.signals(df)
    assert buy.any(), f"{name} kauft nie"
    i = int(np.argmax(buy))
    d = strat.decide(df, i)
    assert d.signal == Signal.BUY
    close = float(df["close"].iloc[i])
    assert d.stop_loss < close < d.take_profit and d.atr > 0


def test_expand_grid_valid_and_complete():
    for name in ALL:
        combos = expand_grid(name)
        assert combos, name
        needed = set(EXIT_GRID) if REGISTRY[name].use_exit_grid else set(REGISTRY[name].exit_param_keys)
        assert all(needed <= set(c) for c in combos)
        assert all(REGISTRY[name].valid_params(c) for c in combos)
        assert len({tuple(sorted(c.items())) for c in combos}) == len(combos)


def test_backtest_segment_starts_with_fresh_capital():
    cfg = StrategyConfig(name="donchian")
    strat = build_strategy(cfg)
    df = make_series("trend", 4000, 7)
    bt = Backtester(strat, RiskManager(RiskConfig()), cfg, PaperConfig())
    res = bt.run(df, "X/USDT", start=2000)
    assert len(res.equity_curve) == 2000
    assert res.equity_curve.index[0] == pd.to_datetime(int(df["timestamp"].iloc[2000]), unit="ms", utc=True)
    assert res.start_balance == 1000.0
    with pytest.raises(ValueError):
        bt.run(df, "X/USDT", start=4000, end=4000)


def _compare(data, names=("ema_cross", "donchian")):
    return run_comparison(data, list(names), StrategyConfig(), RiskConfig(), PaperConfig(), "1h", split=0.6, min_trades=5)


def test_selection_uses_only_in_sample_data():
    """Aenderungen an Kerzen NACH dem Split duerfen die Auswahl und die Auswahlzeitraum-Zahlen nicht beeinflussen."""
    data = {"A/USDT": make_series("trend", 6000, 1), "B/USDT": make_series("trend", 6000, 2)}
    base = _compare(data)
    tampered = {}
    for s, df in data.items():
        df = df.copy()
        cut = int(len(df) * 0.6)
        for col in ("open", "high", "low", "close"):
            df.loc[cut:, col] = df.loc[cut:, col] * 3.0
        tampered[s] = df
    other = _compare(tampered)
    for a, b in zip(base.outcomes, other.outcomes):
        assert a.best.params == b.best.params
        assert a.best.is_agg.ret == pytest.approx(b.best.is_agg.ret)
        assert a.best.is_agg.trades == b.best.is_agg.trades
    assert [o.best.oos_agg.ret for o in base.outcomes] != [o.best.oos_agg.ret for o in other.outcomes]


def test_comparison_structure_and_report():
    data = {"A/USDT": make_series("trend", 6000, 1), "B/USDT": make_series("trend", 6000, 2)}
    cmp = _compare(data)
    ranking = cmp.ranking
    assert [o.best.oos_agg.ret for o in ranking] == sorted((o.best.oos_agg.ret for o in ranking), reverse=True)
    for o in cmp.outcomes:
        cut = int(6000 * 0.6)
        assert o.best.oos_results[0].equity_curve.index[0] == pd.to_datetime(int(data["A/USDT"]["timestamp"].iloc[cut]), unit="ms", utc=True)
    text = format_report(cmp, synthetic=True)
    assert "SYNTHETISCHE DATEN" in text and "Rangliste" in text and "Buy & Hold" in text
    assert "SYNTHETISCHE DATEN" not in format_report(cmp, synthetic=False)
    grid = grid_dataframe(cmp)
    assert grid["selected"].sum() == len(cmp.outcomes)
    assert set(grid["strategy"]) == {"ema_cross", "donchian"}


def test_no_robust_winner_on_pure_random_walk():
    data = {f"R{i}/USDT": make_series("random", 8000, 21 + i) for i in range(3)}
    assert _compare(data).winner is None


def test_trend_strategy_wins_on_trending_data():
    data = {f"T{i}/USDT": make_series("trend", 8000, 11 + i) for i in range(3)}
    winner = _compare(data, ("ema_cross", "donchian", "rsi_reversion")).winner
    assert winner is not None and winner.strategy in {"ema_cross", "donchian"}


def test_cli_compare_demo_writes_report(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"log_dir: {tmp_path / 'logs'}\nstate_dir: {tmp_path / 'state'}\nsymbols: [BTC/USDT]\n")
    out = tmp_path / "reports"
    code = main(["--config", str(cfg), "compare", "--demo", "trend", "--demo-bars", "5000", "--strategies", "ema_cross,donchian", "--out", str(out)])
    assert code == 0
    files = {p.suffix for p in out.iterdir()}
    assert files == {".md", ".csv"}
    assert "DEMO-MODUS" in capsys.readouterr().out


def test_cli_compare_requires_since_and_known_strategies(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"log_dir: {tmp_path / 'logs'}\n")
    assert main(["--config", str(cfg), "compare"]) == 2
    assert main(["--config", str(cfg), "compare", "--demo", "trend", "--strategies", "nope"]) == 2


class FakeExchange:
    """Liefert Kerzen seitenweise wie eine echte Boerse (max. 1000 pro Abruf)."""

    def __init__(self, df):
        self.rows = df.values.tolist()
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=1000):
        self.calls += 1
        rows = [r for r in self.rows if since is None or r[0] >= since]
        return rows[:limit]


def test_load_history_paginates_and_caches(tmp_path):
    from tradingbot.data import CcxtDataFeed, load_history

    df = make_series("trend", 2500, 9, start_ts=1_704_067_200_000)
    ex = FakeExchange(df)
    feed = CcxtDataFeed("binance", ccxt_exchange=ex)
    since = "2024-01-01"
    out = load_history(feed, "BTC/USDT", "1h", since, "2025-01-01", tmp_path)
    assert len(out) == 2500 and out["timestamp"].is_monotonic_increasing
    assert ex.calls >= 3
    calls = ex.calls
    again = load_history(feed, "BTC/USDT", "1h", since, "2025-01-01", tmp_path)
    assert ex.calls == calls and len(again) == 2500
    assert (tmp_path / "BTC_USDT_1h_2024-01-01_2025-01-01.csv").exists()


def test_metrics_rank_and_validate():
    from tradingbot.research import METRICS, metric_value, selection_score

    data = {"A/USDT": make_series("trend", 6000, 1)}
    cmp = run_comparison(data, ["ema_cross"], StrategyConfig(), RiskConfig(), PaperConfig(), "1h", split=0.6, min_trades=5)
    best = cmp.outcomes[0].best
    for m in METRICS:
        v = metric_value(best.is_results[0], m)
        assert v == v and abs(v) < 1e6
    assert metric_value(best.is_results[0], "profit") == best.is_results[0].total_return_pct
    assert selection_score(best.is_results, 10**9) == -1e9
    with pytest.raises(ValueError):
        metric_value(best.is_results[0], "nope")
    with pytest.raises(ValueError):
        run_comparison(data, ["ema_cross"], StrategyConfig(), RiskConfig(), PaperConfig(), "1h", metric="nope")


@pytest.mark.parametrize("metric", ["sharpe", "sortino", "profit"])
def test_comparison_runs_with_every_metric(metric):
    data = {"A/USDT": make_series("trend", 6000, 1), "B/USDT": make_series("trend", 6000, 2)}
    cmp = run_comparison(data, ["ema_cross", "donchian"], StrategyConfig(), RiskConfig(), PaperConfig(), "1h", metric=metric, min_trades=5)
    assert cmp.metric == metric and len(cmp.outcomes) == 2
    assert metric in {"sharpe", "sortino", "profit"} and "Auswahlzeitraum" in format_report(cmp)
