from ..config import AiConfig, DaytradingConfig, StrategyConfig
from .base import EXIT_GRID, Strategy
from .bollinger import BollingerReversionStrategy
from .claude_advisor import ClaudeAdvisorStrategy
from .donchian import DonchianStrategy
from .ensemble import EnsembleStrategy
from .ema_cross import EmaCrossStrategy
from .lux import ReversalZonesStrategy, SuperTrendAIStrategy
from .macd_trend import MacdTrendStrategy
from .opening_range import OpeningRangeBreakoutStrategy
from .rsi_reversion import RsiReversionStrategy
from .smc import SmartMoneyStrategy
from .tsmom import TimeSeriesMomentumStrategy
from .vwap_reversion import VwapReversionStrategy

# Backtestbare Strategien: nehmen am Vergleich teil.
REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (EmaCrossStrategy, DonchianStrategy, RsiReversionStrategy, BollingerReversionStrategy, MacdTrendStrategy,
                VwapReversionStrategy, OpeningRangeBreakoutStrategy, TimeSeriesMomentumStrategy, SmartMoneyStrategy,
                SuperTrendAIStrategy, ReversalZonesStrategy, EnsembleStrategy)
}

# Nur fuer Papier- und Live-Handel, nicht backtestbar.
LIVE_ONLY: dict[str, type[Strategy]] = {ClaudeAdvisorStrategy.name: ClaudeAdvisorStrategy}


def build_strategy(
    cfg: StrategyConfig,
    ai: AiConfig | None = None,
    api_key: str = "",
    client=None,
    session: DaytradingConfig | None = None,
    timeframe: str | None = None,
) -> Strategy:
    if cfg.name in LIVE_ONLY:
        strategy = ClaudeAdvisorStrategy(cfg, ai or AiConfig(), api_key=api_key, client=client)
        if session is not None:
            strategy.session = session
        return strategy
    try:
        cls = REGISTRY[cfg.name]
    except KeyError as exc:
        raise ValueError(f"Unbekannte Strategie '{cfg.name}'. Verfuegbar: {sorted(REGISTRY) + sorted(LIVE_ONLY)}") from exc
    return cls(cfg, session, timeframe)


__all__ = ["Strategy", "EXIT_GRID", "REGISTRY", "LIVE_ONLY", "build_strategy"]
