import pytest

from tradingbot.config import RiskConfig
from tradingbot.risk import RiskManager, cap_stop, liquidation_price


def test_liquidation_price_and_stop_cap():
    assert liquidation_price(100.0, "long", 1.0) is None
    assert liquidation_price(100.0, "long", 100.0) == pytest.approx(99.5)
    assert liquidation_price(100.0, "short", 20.0) == pytest.approx(104.5)
    stop, moved = cap_stop(100.0, 99.0, "long", 100.0)  # 1 % Stop, Liquidation bei 0,5 %
    assert moved and stop == pytest.approx(99.6)
    stop, moved = cap_stop(100.0, 101.0, "short", 100.0)
    assert moved and stop == pytest.approx(100.4)
    assert cap_stop(100.0, 99.0, "long", 10.0) == (99.0, False), "bei 10x liegt die Liquidation 9,5 % entfernt"


def test_max_position_limits_margin_not_notional():
    risk = RiskManager(RiskConfig(risk_per_trade_pct=5.0, max_position_pct=25.0, min_order_quote=0.0))
    # 0,4 % Stop: Risiko 50 USDT -> 125 Einheiten Wunsch, Grenze ohne Hebel 250 USDT Nennwert
    assert risk.position_size(1000, 1000, 100.0, 99.6, side="long", leverage=1.0) == pytest.approx(2.5)
    # mit 100x: Grenze 250 USDT Sicherheit = 25.000 Nennwert, also gilt das Risiko: 50 / 0,4 = 125 Einheiten
    assert risk.position_size(1000, 1000, 100.0, 99.6, side="long", leverage=100.0) == pytest.approx(125.0)
