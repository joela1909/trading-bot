from tradingbot.config import RiskConfig
from tradingbot.models import MarketLimits
from tradingbot.risk import RiskManager


def test_position_size_risk_based():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=100.0, min_order_quote=1.0))
    # Kapital 1000, Risiko 10, Abstand zum Stop 5 -> 2 Einheiten
    assert rm.position_size(1000, 1000, 100.0, 95.0) == 2.0


def test_position_size_capped_by_max_position_pct():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=5.0, max_position_pct=10.0, min_order_quote=1.0))
    # Risiko 50 / Abstand 1 = 50 Einheiten = 5000 -> gedeckelt auf 10 % von 1000 = 100 -> 1 Einheit
    assert rm.position_size(1000, 1000, 100.0, 99.0) == 1.0


def test_position_size_capped_by_free_quote():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=5.0, max_position_pct=100.0, min_order_quote=1.0))
    amt = rm.position_size(1000, 50, 100.0, 99.0)
    assert amt * 100.0 <= 50 * 0.99 + 1e-9


def test_position_size_zero_below_min_order():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=0.1, max_position_pct=100.0, min_order_quote=10.0))
    assert rm.position_size(90, 90, 100.0, 99.0) == 0.0


def test_position_size_respects_market_limits_and_precision():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=100.0, min_order_quote=1.0))
    limits = MarketLimits(min_amount=0.01, min_cost=5.0, amount_precision=2)
    assert rm.position_size(1000, 1000, 100.0, 97.0, limits) == 3.33
    assert rm.position_size(1000, 1000, 100.0, 95.0, MarketLimits(min_amount=5.0)) == 0.0
    assert rm.position_size(1000, 1000, 100.0, 95.0, limits) == 2.0


def test_invalid_stop_gives_zero():
    rm = RiskManager(RiskConfig())
    assert rm.position_size(1000, 1000, 100.0, 100.0) == 0.0
    assert rm.position_size(1000, 1000, 100.0, 105.0) == 0.0


def test_daily_loss_and_open_positions():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=3.0, max_open_positions=2))
    assert not rm.daily_loss_exceeded(1000, 975)
    assert rm.daily_loss_exceeded(1000, 970)
    assert rm.can_open_new(1)
    assert not rm.can_open_new(2)
