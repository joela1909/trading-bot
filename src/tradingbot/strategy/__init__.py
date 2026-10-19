from ..config import StrategyConfig
from .base import EXIT_GRID, Strategy
from .bollinger import BollingerReversionStrategy
from .donchian import DonchianStrategy
from .ema_cross import EmaCrossStrategy
from .macd_trend import MacdTrendStrategy
from .rsi_reversion import RsiReversionStrategy

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (EmaCrossStrategy, DonchianStrategy, RsiReversionStrategy, BollingerReversionStrategy, MacdTrendStrategy)
}


def build_strategy(cfg: StrategyConfig) -> Strategy:
    try:
        cls = REGISTRY[cfg.name]
    except KeyError as exc:
        raise ValueError(f"Unbekannte Strategie '{cfg.name}'. Verfuegbar: {sorted(REGISTRY)}") from exc
    return cls(cfg)


__all__ = ["Strategy", "EXIT_GRID", "REGISTRY", "build_strategy"]
