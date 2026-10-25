from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tradingbot.analysis import check_lookahead, check_recursive
from tradingbot.backtest import Backtester
from tradingbot.cli import main
from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
from tradingbot.research import expand_grid
from tradingbot.risk import RiskManager
from tradingbot.strategy import REGISTRY, build_strategy
from tradingbot.strategy.base import Strategy
from tradingbot.synthetic import make_series

ET = ZoneInfo("America/New_York")
T0 = 1_704_153_600_000  # 2024-01-02 00:00 UTC
BAR = 900_000


def _cfg(**kw):
    base = dict(name="anchored_vwap", allow_short=True, trailing_stop=False)
    base.update(kw)
    return StrategyConfig(**base)


def _intraday(seed=3, n=6000, tf_ms=300_000):
    df = make_series("random", n, seed, tf_ms=tf_ms)
    df["volume"] = np.random.default_rng(seed).uniform(20, 200, n)
    return df


def _reference(df, strat):
    """Zeile fuer Zeile aus dem Original uebertragen, ohne die gemeinsame Vektor-Hilfe zu nutzen."""
    ema_v, atr_v, base_v = df["ema_sig"].tolist(), df["atr"].tolist(), df["atr_base"].tolist()
    n = len(df)
    out = {k: [False] * n for k in ("gd", "rd", "gt", "rt")}
    day = cur = None
    pv = volume = 0.0
    regime, wr, wg, has_prev, prev_close, prev_vwap = 0, False, False, False, 0.0, 0.0
    for i in range(n):
        dt = datetime.fromtimestamp(int(df["timestamp"].iloc[i]) / 1000, tz=ET)
        d, hhmm = dt.year * 10000 + dt.month * 100 + dt.day, dt.hour * 100 + dt.minute
        if d != day:
            day, pv, volume, regime, wr, wg, has_prev = d, 0.0, 0.0, 0, False, False, False
        p = (df["high"].iloc[i] + df["low"].iloc[i] + df["close"].iloc[i]) / 3.0
        v = max(df["volume"].iloc[i], 0.0)
        pv += p * v
        volume += v
        e = ema_v[i]
        if e != e or volume <= 0.0:
            continue
        vwap = pv / volume
        a, b = atr_v[i], base_v[i]
        if a != a or b != b or a <= 0 or b <= 0:
            continue
        close = float(df["close"].iloc[i])
        if 930 <= hhmm <= 1600:
            if wr and close > e:
                out["rt"][i], wr, regime = True, False, -2
            elif wg and close < e:
                out["gt"][i], wg, regime = True, False, 2
            elif regime == -2 and e > vwap:
                regime = -3
            elif regime == 2 and e < vwap:
                regime = 3
            elif regime == -3 and has_prev and prev_close >= prev_vwap and close < vwap:
                out["gd"][i], regime = True, 0
            elif regime == 3 and has_prev and prev_close <= prev_vwap and close > vwap:
                out["rd"][i], regime = True, 0
            elif regime == 0:
                if e < vwap and close < e:
                    out["rd"][i], wr, regime = True, True, -1
                elif e > vwap and close > e:
                    out["gd"][i], wg, regime = True, True, 1
        prev_close, prev_vwap, has_prev = close, vwap, True
    return {k: np.array(v) for k, v in out.items()}


def test_state_machine_matches_a_line_by_line_reference():
    for seed in (1, 2, 3):
        strat = build_strategy(_cfg())
        df = strat.prepare(_intraday(seed))
        ref = _reference(df, strat)
        for col, key in (("green_dot", "gd"), ("red_dot", "rd"), ("green_tri", "gt"), ("red_tri", "rt")):
            assert np.array_equal(df[col].to_numpy(), ref[key]), f"{col} weicht ab (Seed {seed})"
        assert df["green_dot"].sum() > 5 and df["red_dot"].sum() > 5 and df["green_tri"].sum() > 3 and df["red_tri"].sum() > 3


def test_signals_are_inside_the_session_only():
    strat = build_strategy(_cfg(session_start_hhmm=1000, session_end_hhmm=1100))
    df = strat.prepare(_intraday(2))
    local = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(ET)
    hhmm = (local.dt.hour * 100 + local.dt.minute).to_numpy()
    marks = df[["green_dot", "red_dot", "green_tri", "red_tri"]].any(axis=1).to_numpy()
    assert marks.any() and ((hhmm[marks] >= 1000) & (hhmm[marks] <= 1100)).all()


def _falling_day():
    rows = [(100.0, 100.5, 99.5, 100.0)] * 12
    for k in range(1, 15):  # Kurs faellt Kerze fuer Kerze
        c = 100.0 - 1.5 * k
        rows.append((c + 1.5, c + 2.0, c - 0.5, c))
    for c in (80.0, 86.0, 93.0):  # kraeftige Erholung
        rows.append((c - 4.0, c + 0.5, c - 4.5, c))
    ts = T0 + np.arange(len(rows)) * BAR
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100.0})


def test_falling_market_gives_red_dot_then_red_triangle_on_rebound():
    strat = build_strategy(_cfg(tz="UTC", session_start_hhmm=0, session_end_hhmm=2359, ema_length=3, baseline_atr_period=5, atr_period=3))
    df = strat.prepare(_falling_day())
    rd, rt = df["red_dot"].to_numpy(), df["red_tri"].to_numpy()
    assert not df["green_dot"].any() and not df["green_tri"].any()
    first_dot, first_tri = int(np.argmax(rd)), int(np.argmax(rt))
    assert rd.any() and rt.any() and first_dot < first_tri
    assert df["ema_sig"].iloc[first_dot] < df["vwap"].iloc[first_dot] and df["close"].iloc[first_dot] < df["ema_sig"].iloc[first_dot]
    assert df["close"].iloc[first_tri] > df["ema_sig"].iloc[first_tri]
    assert first_dot == int(np.argmax(strat.short_signals(df)[0])) or not strat._vol_ok(df)[first_dot]


def test_volatility_filter_blocks_entries_but_not_the_markers():
    on = build_strategy(_cfg(max_relative_atr=0.1))
    off = build_strategy(_cfg(use_relative_volatility_filter=False))
    d_on, d_off = on.prepare(_intraday(2)), off.prepare(_intraday(2))
    assert d_on["green_dot"].sum() == d_off["green_dot"].sum(), "die Marker bleiben gleich"
    assert on.signals(d_on)[0].sum() < off.signals(d_off)[0].sum()
    assert off.signals(d_off)[0].sum() == d_off["green_dot"].sum()


def test_stop_is_atr_multiple_of_the_signal_close_and_no_target():
    strat = build_strategy(_cfg(atr_stop_multiplier=2.5))
    df = strat.prepare(_intraday(1))
    ls, lt, ss, st = strat.entry_levels(df)
    i = 3000
    assert ls[i] == pytest.approx(df["close"].iloc[i] - 2.5 * df["atr"].iloc[i]) and ss[i] == pytest.approx(df["close"].iloc[i] + 2.5 * df["atr"].iloc[i])
    assert np.isnan(lt).all() and np.isnan(st).all()
    with pytest.raises(ValueError):
        build_strategy(_cfg(use_atr_stop=False))


def test_registered_causal_live_consistent_and_grid():
    strat = build_strategy(_cfg(), timeframe="5m")
    df = _intraday(4)
    assert check_lookahead(strat, df).ok
    assert check_recursive(strat, df).ok
    assert "anchored_vwap" in REGISTRY
    combos = expand_grid("anchored_vwap")
    assert len(combos) == len({tuple(sorted(c.items())) for c in combos}) and "stop_atr_mult" not in combos[0]


def test_live_run_is_refused(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"log_dir: {tmp_path / 'logs'}\nstate_dir: {tmp_path / 'state'}\nstrategy:\n  name: anchored_vwap\n")
    assert main(["--config", str(cfg), "run", "--once"]) == 2
    assert "nur im Backtest" in capsys.readouterr().err


# ------------------------------------------------ Backtester: Tagesversuche und Ausstieg zur naechsten Eroeffnung
class Scripted(Strategy):
    name = "scripted"
    entry_on_next_open = True
    fixed_levels = True

    def __init__(self, cfg, buys, rule=None, sells=(), stop=1.0, tp=1.0, exit_next=False):
        super().__init__(cfg)
        self.buys, self.sells, self.rule, self.stop, self.tp = list(buys), list(sells), rule, stop, tp
        self.exit_on_next_open = exit_next

    @property
    def warmup_bars(self):
        return 5

    def _add_indicators(self, df):
        df["day_key"] = (df["timestamp"] // 86_400_000).astype(int)

    def signals(self, df):
        buy, sell = np.zeros(len(df), dtype=bool), np.zeros(len(df), dtype=bool)
        buy[self.buys] = True
        sell[self.sells] = True
        return buy, sell

    def entry_levels(self, df):
        c = df["close"].to_numpy()
        nan = np.full(len(df), np.nan)
        return c - self.stop, (c + self.tp if self.tp else nan), nan, nan.copy()

    def attempt_rule(self):
        return self.rule

    def day_keys(self, df):
        return df["day_key"].to_numpy()


def _path(n, results):
    """Flache Kurse. results: {Signalindex: 'win'|'loss'} - die Kerze danach loest Ziel (101) oder Stop (99) aus."""
    rows = [[100.0, 100.2, 99.8, 100.0] for _ in range(n)]
    for j, res in results.items():
        if res == "win":
            rows[j + 1][1] = 101.5
        else:
            rows[j + 1][2] = 98.5
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"timestamp": T0 + np.arange(n) * BAR, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _run(df, buys, rule, **kw):
    cfg = StrategyConfig(name="anchored_vwap", trailing_stop=False)
    strat = Scripted(cfg, buys, rule, **kw)
    bt = Backtester(strat, RiskManager(RiskConfig(max_position_pct=100.0)), cfg, PaperConfig(starting_balance=10000, fee_pct=0.0, slippage_pct=0.0))
    return bt.run(df, "X/USDT")


RULE = {"stop_after_first_win": True, "additional_attempts": 2}


def test_first_win_ends_the_day():
    r = _run(_path(80, {20: "win", 30: "win", 40: "win"}), [20, 30, 40, 50], RULE)
    assert r.n_trades == 1 and r.trades[0].pnl_quote > 0


def test_after_a_loss_two_more_attempts_are_allowed_whatever_their_result():
    r = _run(_path(100, {20: "loss", 30: "loss", 40: "loss", 50: "win"}), [20, 30, 40, 50], RULE)
    assert r.n_trades == 3 and [t.pnl_quote < 0 for t in r.trades] == [True, True, True]
    r2 = _run(_path(100, {20: "loss", 30: "win", 40: "loss", 50: "loss"}), [20, 30, 40, 50], RULE)
    assert r2.n_trades == 3, "ein spaeterer Gewinn beendet den Tag nicht"


def test_zero_additional_attempts_means_one_trade_after_a_loss():
    r = _run(_path(80, {20: "loss", 30: "loss"}), [20, 30], {"stop_after_first_win": True, "additional_attempts": 0})
    assert r.n_trades == 1


def test_attempts_reset_on_the_next_day_and_no_rule_means_no_limit():
    per_day = 96
    results = {20: "loss", 30: "loss", 40: "loss", 50: "loss", per_day + 20: "loss", per_day + 30: "loss"}
    buys = [20, 30, 40, 50, per_day + 20, per_day + 30]
    r = _run(_path(2 * per_day, results), buys, RULE)
    assert r.n_trades == 5, "Tag 1: drei Trades, Tag 2: zwei Trades"
    assert _run(_path(2 * per_day, results), buys, None).n_trades == 6


def test_signals_before_the_atr_is_ready_are_skipped():
    assert _run(_path(60, {10: "loss", 20: "loss"}), [10, 20], None).n_trades == 1


def test_exit_signal_executes_at_the_next_open():
    df = _path(80, {})
    df.loc[36, "open"] = 103.0  # Eroeffnung nach dem Ausstiegssignal
    for col in ("high", "low", "close"):
        df.loc[36, col] = 103.0
    r = _run(df, [20], None, sells=[35], stop=50.0, tp=0.0, exit_next=True)
    t = r.trades[0]
    assert t.reason == "signal" and t.exit_price == pytest.approx(103.0) and t.entry_price == pytest.approx(100.0)
    r_now = _run(df, [20], None, sells=[35], stop=50.0, tp=0.0, exit_next=False)
    assert r_now.trades[0].exit_price == pytest.approx(100.0), "ohne Schalter zum Schlusskurs der Signalkerze"
