from __future__ import annotations

import pandas as pd

from ..ai import AiDecision, ClaudeAdvisor, build_user_prompt
from ..config import AiConfig, StrategyConfig
from ..indicators import ema, rsi
from ..models import Decision, Signal
from .base import WARMUP_FACTOR, Strategy


class ClaudeAdvisorStrategy(Strategy):
    """KI-Strategie: Claude entscheidet bei jeder neuen Kerze ueber Einstieg und Ausstieg.

    Nicht backtestbar, weil das Modell die Vergangenheit teilweise aus dem Training kennt. Bewerten laesst
    sich die Strategie nur im Papierhandel. Stop-Loss, Take-Profit und Positionsgroesse bleiben beim Bot.
    """

    name = "claude_advisor"
    live_only = True
    chart_lines = [("ema_fast", "EMA schnell", "main"), ("ema_slow", "EMA langsam", "trend")]

    def __init__(self, cfg: StrategyConfig, ai: AiConfig, api_key: str = "", client=None):
        super().__init__(cfg)
        self.ai = ai
        self.advisor = ClaudeAdvisor(ai, api_key=api_key, client=client)

    @property
    def warmup_bars(self) -> int:
        return WARMUP_FACTOR * max(self.cfg.slow_ema, self.cfg.rsi_period, self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        df["ema_fast"] = ema(df["close"], self.cfg.fast_ema)
        df["ema_slow"] = ema(df["close"], self.cfg.slow_ema)
        df["rsi"] = rsi(df["close"], self.cfg.rsi_period)

    def signals(self, df):
        raise NotImplementedError("claude_advisor ist nicht backtestbar, nur Papier- und Live-Handel")

    def decide(self, df: pd.DataFrame, i: int = -1, context: dict | None = None) -> Decision:
        context = context or {}
        if len(df) < 2:
            return Decision(Signal.HOLD, "zu wenig Daten")
        atr_value = float(df["atr"].iloc[-1])
        if atr_value != atr_value or atr_value <= 0:
            return Decision(Signal.HOLD, "Indikatoren noch im Warmup")
        symbol = context.get("symbol", "?")
        position = context.get("position")
        prompt = build_user_prompt(symbol, context.get("timeframe", "?"), context.get("quote", ""), df, context, self.ai.candles_in_prompt)
        d: AiDecision = self.advisor.ask(prompt)

        if not d.ok:
            self.advisor.record(symbol, d, applied=False, note="Fehler, es wird gehalten")
            return Decision(Signal.HOLD, d.reason, atr=atr_value)

        close = float(df["close"].iloc[-1])
        if d.action == "buy" and position is None:
            if d.confidence < self.ai.min_confidence:
                self.advisor.record(symbol, d, applied=False, note=f"Konfidenz unter {self.ai.min_confidence:.2f}")
                return Decision(Signal.HOLD, f"KI-Kauf verworfen, Konfidenz {d.confidence:.2f}", atr=atr_value)
            self.advisor.record(symbol, d, applied=True)
            stop = close - self.stop_distance(close, atr_value)
            target = self.target_distance(close, atr_value)
            tp = close + target if target is not None else None
            return Decision(Signal.BUY, f"KI: {d.reason}", stop_loss=stop, take_profit=tp, atr=atr_value)
        if d.action == "sell" and position is not None:
            if d.confidence < self.ai.min_confidence_exit:
                self.advisor.record(symbol, d, applied=False, note=f"Konfidenz unter {self.ai.min_confidence_exit:.2f}")
                return Decision(Signal.HOLD, f"KI-Verkauf verworfen, Konfidenz {d.confidence:.2f}", atr=atr_value)
            self.advisor.record(symbol, d, applied=True)
            return Decision(Signal.SELL, f"KI: {d.reason}", atr=atr_value)
        applied = d.action == "hold"
        self.advisor.record(symbol, d, applied=applied, note="" if applied else "passt nicht zur Position")
        return Decision(Signal.HOLD, f"KI: {d.reason}", atr=atr_value)

    def export_state(self) -> dict:
        return {"ai": self.advisor.export_state()}

    def import_state(self, state: dict) -> None:
        if "ai" in state:
            self.advisor.import_state(state["ai"])
