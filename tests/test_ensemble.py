import numpy as np
import pandas as pd
import pytest

from tradingbot.backtest import Backtester
from tradingbot.config import PaperConfig, RiskConfig, StrategyConfig
from tradingbot.models import Signal
from tradingbot.risk import RiskManager
from tradingbot.strategy import REGISTRY, build_strategy
from tradingbot.synthetic import make_series


def _series() -> pd.DataFrame:
    trend = make_series("trend", 3000, 5)
    rev = make_series("meanrev", 3000, 6, start_price=float(trend["close"].iloc[-1]), start_ts=int(trend["timestamp"].iloc[-1]) + 3_600_000)
    return pd.concat([trend, rev], ignore_index=True)


def test_default_members_are_all_strategies_except_itself():
    s = build_strategy(StrategyConfig(name="ensemble"))
    assert {m.name for m in s.members} == set(REGISTRY) - {"ensemble", "tsmom", "smc", "supertrend_ai", "reversal_zones"}, "tsmom nur auf Wunsch"
    assert s.warmup_bars == max(m.warmup_bars for m in s.members)


def test_unknown_member_is_rejected():
    with pytest.raises(ValueError):
        build_strategy(StrategyConfig(name="ensemble", members=["ema_cross", "gibt_es_nicht"]))
    with pytest.raises(ValueError):
        build_strategy(StrategyConfig(name="ensemble", members=["ensemble"]))


def test_single_member_with_one_vote_equals_the_member():
    df = _series()
    member = build_strategy(StrategyConfig(name="donchian", allow_short=True))
    ens = build_strategy(StrategyConfig(name="ensemble", members=["donchian"], min_votes=1, allow_short=True))
    mb, ms, msh, mco = member.all_signals(member.prepare(df))
    eb, es, esh, eco = ens.all_signals(ens.prepare(df))
    # Ein Mitglied: Einstieg identisch, Ausstieg nur beim Gegensignal (eigene Ausstiegs-Zustaende zaehlen nicht)
    assert (eb == (mb & ~msh)).all()
    assert (esh == (msh & ~mb)).all()
    assert (es == esh).all() and (eco == eb).all()


def test_majority_and_conflict_rules():
    ens = build_strategy(StrategyConfig(name="ensemble", members=["ema_cross", "donchian", "macd_trend"], min_votes=2, allow_short=True))
    df = ens.prepare(_series()).iloc[:5].copy()
    for m in ens.members:
        for kind in ("buy", "sell", "short", "cover"):
            df[f"v_{m.name}_{kind}"] = False
    df["atr"] = 1.0
    # Kerze 0: zwei Long-Stimmen -> Long. Kerze 1: zwei Long, eine Short -> Long. Kerze 2: zwei Long, zwei... (nur 3 Mitglieder)
    df.loc[0, ["v_ema_cross_buy", "v_donchian_buy"]] = True
    df.loc[1, ["v_ema_cross_buy", "v_donchian_buy", "v_macd_trend_short"]] = True
    df.loc[2, ["v_ema_cross_buy"]] = True  # nur eine Stimme
    df.loc[3, ["v_ema_cross_short", "v_donchian_short"]] = True
    df.loc[4, ["v_ema_cross_sell", "v_macd_trend_sell"]] = True
    buy, sell = ens.signals(df)
    short, cover = ens.short_signals(df)
    assert list(buy) == [True, True, False, False, False]
    assert list(short) == [False, False, False, True, False]
    assert sell[3], "Short-Mehrheit schliesst Long"
    assert not sell[4], "Ausstiegs-Zustaende einzelner Strategien schliessen nichts mehr"
    assert cover[0] and cover[1], "Long-Mehrheit schliesst Short"
    d = ens.decide(df.reset_index(drop=True), 1, context={"position": None})  # Kerze 0 gilt als Warmup
    assert d.signal == Signal.BUY and d.reason == "2 von 3 Strategien: ema_cross, donchian"
    votes = ens.member_votes(df, 3)
    assert [v["vote"] for v in votes] == ["short", "short", None]


def test_ensemble_backtests_and_explains():
    cfg = StrategyConfig(name="ensemble", min_votes=2, allow_short=True)
    s = build_strategy(cfg)
    df = _series()
    res = Backtester(s, RiskManager(RiskConfig()), cfg, PaperConfig()).run(df, "X/USDT")
    assert res.n_trades > 0
    assert res.end_equity - res.start_balance == pytest.approx(sum(t.pnl_quote for t in res.trades), abs=1e-6)
    text = s.explain(s.prepare(df))
    assert text[0].startswith("Schiedsrichter") and any(t.startswith("Short-Stimmen") for t in text)
    assert ("vwap", "Tages-VWAP", "main") in s.chart_lines


def test_member_params_override():
    s = build_strategy(StrategyConfig(name="ensemble", members=["vwap_reversion"], member_params={"vwap_reversion": {"vwap_dev_atr": 1.25}}))
    assert s.members[0].p("vwap_dev_atr", 0) == 1.25


def test_solo_member_alone_opens_and_nothing_stops_it():
    ens = build_strategy(StrategyConfig(name="ensemble", members=["ema_cross", "donchian", "macd_trend"], min_votes=3,
                                        allow_short=True, solo_member="donchian"))
    df = ens.prepare(_series()).iloc[:5].copy()
    for m in ens.members:
        for kind in ("buy", "sell", "short", "cover"):
            df[f"v_{m.name}_{kind}"] = False
    df["atr"] = 1.0
    df.loc[0, ["v_donchian_buy"]] = True                                         # nur die Solo-Strategie -> Long
    df.loc[1, ["v_donchian_buy", "v_ema_cross_short", "v_macd_trend_short"]] = True  # zwei dagegen -> trotzdem Long
    df.loc[2, ["v_donchian_short"]] = True                                       # Solo Short -> Short
    df.loc[3, ["v_ema_cross_buy"]] = True                                        # eine andere Stimme reicht nicht
    df.loc[4, ["v_ema_cross_buy", "v_macd_trend_buy", "v_donchian_short"]] = True  # Mehrheit kann Solo nicht aufhalten
    buy, sell = ens.signals(df)
    short, cover = ens.short_signals(df)
    assert list(buy) == [True, True, False, False, False]
    assert list(short) == [False, False, True, False, True]
    assert list(cover) == list(buy) and list(sell) == list(short)
    d = ens.decide(df, 1, context={"position": None})
    assert d.signal == Signal.BUY and "donchian reicht allein" in d.reason
    assert [v["solo"] for v in ens.member_votes(df, 0)] == [False, True, False]


def test_solo_member_must_be_a_member():
    with pytest.raises(ValueError):
        build_strategy(StrategyConfig(name="ensemble", members=["ema_cross"], solo_member="donchian"))
