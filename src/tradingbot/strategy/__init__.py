from ..config import AiConfig, StrategyConfig
from .base import EXIT_GRID, Strategy
from .bollinger import BollingerReversionStrategy
from .claude_advisor import ClaudeAdvisorStrategy
from .donchian import DonchianStrategy
from .ema_cross import EmaCrossStrategy
from .macd_trend import MacdTrendStrategy
from .rsi_reversion import RsiReversionStrategy

# Backtestbare Strategien: nehmen am Vergleich teil.
REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (EmaCrossStrategy, DonchianStrategy, RsiReversionStrategy, BollingerReversionStrategy, MacdTrendStrategy)
}

# Nur fuer Papier- und Live-Handel, nicht backtestbar.
LIVE_ONLY: dict[str, type[Strategy]] = {ClaudeAdvisorStrategy.name: ClaudeAdvisorStrategy}


def build_strategy(cfg: StrategyConfig, ai: AiConfig | None = None, api_key: str = "", client=None) -> Strategy:
    if cfg.name in LIVE_ONLY:
        return ClaudeAdvisorStrategy(cfg, ai or AiConfig(), api_key=api_key, client=client)
    try:
        cls = REGISTRY[cfg.name]
    except KeyError as exc:
        raise ValueError(f"Unbekannte Strategie '{cfg.name}'. Verfuegbar: {sorted(REGISTRY) + sorted(LIVE_ONLY)}") from exc
    return cls(cfg)


__all__ = ["Strategy", "EXIT_GRID", "REGISTRY", "LIVE_ONLY", "build_strategy"]
