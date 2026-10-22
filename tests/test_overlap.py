import numpy as np
import pytest

from tradingbot.cli import main
from tradingbot.config import PaperConfig, ProtectionConfig, RiskConfig, StrategyConfig
from tradingbot.overlap import _recent, analyze_symbol, combine, format_overlap
from tradingbot.strategy import REGISTRY
from tradingbot.synthetic import make_series


def _run(names, kind="trend", seed=1, n=6000, protections=None, window=6):
    df = make_series(kind, n, seed)
    return analyze_symbol(df, "X/USDT", list(names), StrategyConfig(), RiskConfig(), PaperConfig(), protections, window=window)


def test_recent_window_only_looks_backwards():
    ev = np.array([0, 0, 1, 0, 0, 0, 0, 0], dtype=bool)
    assert list(_recent(ev, 3)) == [False, False, True, True, True, False, False, False]


def test_distributions_are_complete_and_consistent():
    r = _run(REGISTRY)
    n = len(REGISTRY)
    for counts in (r.buy_k, r.sell_k, r.buy_window_k, r.long_k, r.entry_k, r.entry_window_k):
        assert len(counts) == n + 1 and counts.sum() == r.bars
    assert sum(r.buy_events.values()) == int(sum(k * c for k, c in enumerate(r.buy_k)))
    for nm in r.names:
        assert 0.0 <= r.time_long[nm] <= 100.0
    assert r.long_k[n] / r.bars * 100 <= min(r.time_long.values()) + 1e-9
    assert all(0.0 <= v <= 100.0 for v in r.state_agreement.values())


def test_identical_strategies_always_agree():
    r = _run(["ema_cross", "ema_cross"])
    assert list(r.state_agreement.values()) == [100.0]
    assert r.long_k[2] == pytest.approx(r.bars * r.time_long["ema_cross"] / 100.0, abs=1)
    assert r.buy_k[2] == r.buy_events["ema_cross"]


def test_trend_and_reversion_rarely_agree():
    r = _run(["ema_cross", "rsi_reversion"], kind="meanrev", seed=3)
    assert r.state_agreement[("ema_cross", "rsi_reversion")] < 90.0


def test_combine_sums_bars_and_counts():
    a, b = _run(["ema_cross", "donchian"], seed=1), _run(["ema_cross", "donchian"], seed=2)
    c = combine([a, b])
    assert c.bars == a.bars + b.bars and c.long_k.sum() == c.bars
    assert c.buy_events["donchian"] == a.buy_events["donchian"] + b.buy_events["donchian"]
    assert c.symbols == ["X/USDT", "X/USDT"]


def test_report_text_and_protections_flag():
    r = _run(REGISTRY, protections=ProtectionConfig())
    text = format_overlap(r, synthetic=True)
    assert "SYNTHETISCHE DATEN" in text and "Alle 5" in text and "Paarvergleich" in text
    assert "SYNTHETISCHE DATEN" not in format_overlap(r)


def test_too_little_data_raises():
    with pytest.raises(ValueError):
        analyze_symbol(make_series("trend", 300, 1), "X/USDT", ["ema_cross", "donchian"], StrategyConfig(), RiskConfig(), PaperConfig(), None)


def test_cli_overlap_demo_and_validation(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"log_dir: {tmp_path / 'logs'}\n")
    out = tmp_path / "reports"
    assert main(["--config", str(cfg), "overlap", "--demo", "trend", "--demo-bars", "5000", "--out", str(out)]) == 0
    assert len(list(out.glob("uebereinstimmung_*.md"))) == 1
    assert "DEMO-MODUS" in capsys.readouterr().out
    assert main(["--config", str(cfg), "overlap", "--demo", "trend", "--strategies", "ema_cross"]) == 2
    assert main(["--config", str(cfg), "overlap", "--demo", "trend", "--strategies", "ema_cross,claude_advisor"]) == 2
