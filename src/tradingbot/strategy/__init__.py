from .base import Strategy
from .ema_cross import EmaCrossStrategy

from ..config import StrategyConfig

_REGISTRY: dict[str, type[Strategy]] = {
    EmaCrossStrategy.name: EmaCrossStrategy,
}


def build_strategy(cfg: StrategyConfig) -> Strategy:
    try:
        cls = _REGISTRY[cfg.name]
    except KeyError as exc:
        raise ValueError(f"Unbekannte Strategie '{cfg.name}'. Verfuegbar: {sorted(_REGISTRY)}") from exc
    return cls(cfg)


__all__ = ["Strategy", "EmaCrossStrategy", "build_strategy"]
