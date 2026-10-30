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

    MIN_STOP_PCT, MAX_STOP_PCT = 0.15, 10.0  # Stop der KI nur in diesem Abstand vom Kurs, sonst gilt der eigene Plan

    def __init__(self, cfg: StrategyConfig, ai: AiConfig, api_key: str = "", client=None, timeframe: str | None = None):
        super().__init__(cfg, timeframe=timeframe)
        self.ai = ai
        self.timeframe = timeframe or ""
        self.gated = self.timeframe in ai.gate_timeframes  # Vorauswahl: KI nur bei Setups fragen
        self._levels: tuple[str, float | None, float | None] | None = None  # (Richtung, Stop, Ziel) der letzten KI-Antwort
        self.cost_pct = 0.0  # Kosten je Trade hin und zurueck in %, setzt der Bot
        self.advisor = ClaudeAdvisor(ai, api_key=api_key, client=client, futures=cfg.allow_short)

    @property
    def warmup_bars(self) -> int:
        base = WARMUP_FACTOR * max(self.cfg.slow_ema, self.cfg.rsi_period, self.cfg.atr_period)
        # fuer den Trend auf 15m und 1h: etwa 55 Stunden Vorgeschichte (1m: 3.300 Kerzen)
        if self.timeframe_ms:
            return max(base, 55 * 3_600_000 // self.timeframe_ms)
        return base

    def setup(self, df: pd.DataFrame) -> tuple[str, str] | None:
        """Vorauswahl auf der letzten Kerze: (Beschreibung, Richtung) oder None.
        Sweep: Docht holt das Hoch/Tief der letzten Kerzen ab, Schluss wieder innerhalb (Umkehr).
        Ausbruch: Schluss jenseits des Hochs/Tiefs, in Richtung des 15m-Trends. Beides nur mit gate_volume_min-fachem Volumen."""
        n = self.ai.gate_lookback
        if len(df) < n + 2:
            return None
        last = df.iloc[-1]
        look = df.iloc[-n - 1:-1]
        hi, lo = float(look["high"].max()), float(look["low"].min())
        vol = float(look["volume"].mean()) or 1.0
        vr = float(last["volume"]) / vol
        c = float(last["close"])
        if vr < self.ai.gate_volume_min:
            return None
        trend = df["htf_trend"].iloc[-1] if "htf_trend" in df.columns else 0
        if float(last["low"]) < lo and c > lo:
            return f"Liquiditaets-Sweep unter dem {n}-Kerzen-Tief {lo:.6g} mit Schluss darueber (Volumen {vr:.1f}x)", "long"
        if float(last["high"]) > hi and c < hi:
            return f"Liquiditaets-Sweep ueber dem {n}-Kerzen-Hoch {hi:.6g} mit Schluss darunter (Volumen {vr:.1f}x)", "short"
        if c > hi and trend >= 0:
            return f"Ausbruch ueber das {n}-Kerzen-Hoch {hi:.6g} mit {vr:.1f}x Volumen, 15m-Trend aufwaerts", "long"
        if c < lo and trend <= 0:
            return f"Ausbruch unter das {n}-Kerzen-Tief {lo:.6g} mit {vr:.1f}x Volumen, 15m-Trend abwaerts", "short"
        return None

    def _add_indicators(self, df: pd.DataFrame) -> None:
        df["ema_fast"] = ema(df["close"], self.cfg.fast_ema)
        df["ema_slow"] = ema(df["close"], self.cfg.slow_ema)
        df["rsi"] = rsi(df["close"], self.cfg.rsi_period)
        # Trend des naechstgroesseren Zeitrahmens (15m-EMA 20 gegen 50) auf die Bot-Kerzen uebertragen: 1 auf, -1 ab
        if self.timeframe_ms and self.timeframe_ms < 900_000 and "timestamp" in df.columns:
            k = 900_000 // self.timeframe_ms
            e20, e50 = ema(df["close"], 20 * k), ema(df["close"], 50 * k)
            df["htf_trend"] = (e20 > e50).astype(int) - (e20 < e50).astype(int)

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
        if self.gated:
            # Vorauswahl: ohne Setup keine Abfrage; offene Positionen nur jede n-te Kerze pruefen (Stop/Ziel laufen ja)
            if position is None:
                found = self.setup(df)
                if found is None:
                    return Decision(Signal.HOLD, "kein Setup (Vorauswahl), KI nicht gefragt", atr=atr_value)
                context = {**context, "setup": found[0]}
            else:
                ts = int(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else 0
                if self.timeframe_ms and (ts // self.timeframe_ms) % self.ai.position_check_every:
                    return Decision(Signal.HOLD, "Position laeuft, KI prueft jede "
                                    f"{self.ai.position_check_every}. Kerze", atr=atr_value)
        if position is None:
            close_now = float(df["close"].iloc[-1])
            context = {**context, "min_stop_pct": self.min_stop_pct(close_now, atr_value), "min_rr": self.ai.min_rr}
        prompt = build_user_prompt(symbol, context.get("timeframe", "?"), context.get("quote", ""), df, context, self.ai.candles_in_prompt)
        d: AiDecision = self.advisor.ask(prompt)
        self._levels = None

        if not d.ok:
            self.advisor.record(symbol, d, applied=False, note="Fehler, es wird gehalten")
            return Decision(Signal.HOLD, d.reason, atr=atr_value)

        close = float(df["close"].iloc[-1])
        side = (position or {}).get("side", "long") if position else None
        if d.action == "short" and position is None and self.cfg.allow_short:
            if d.confidence < self.ai.min_confidence:
                self.advisor.record(symbol, d, applied=False, note=f"Konfidenz unter {self.ai.min_confidence:.2f}")
                return Decision(Signal.HOLD, f"KI-Short verworfen, Konfidenz {d.confidence:.2f}", atr=atr_value)
            self.advisor.record(symbol, d, applied=True)
            self._levels = ("short", d.stop_loss, d.take_profit)
            stop, tp = self.entry_plan(df, len(df) - 1, "short", close, atr_value)
            return Decision(Signal.SHORT, f"KI: {d.reason}", stop_loss=stop, take_profit=tp, atr=atr_value)
        if d.action == "cover" and side == "short":
            if d.confidence < self.ai.min_confidence_exit:
                self.advisor.record(symbol, d, applied=False, note=f"Konfidenz unter {self.ai.min_confidence_exit:.2f}")
                return Decision(Signal.HOLD, f"KI-Ausstieg verworfen, Konfidenz {d.confidence:.2f}", atr=atr_value)
            self.advisor.record(symbol, d, applied=True)
            return Decision(Signal.COVER, f"KI: {d.reason}", atr=atr_value)
        if d.action == "buy" and position is None:
            if d.confidence < self.ai.min_confidence:
                self.advisor.record(symbol, d, applied=False, note=f"Konfidenz unter {self.ai.min_confidence:.2f}")
                return Decision(Signal.HOLD, f"KI-Kauf verworfen, Konfidenz {d.confidence:.2f}", atr=atr_value)
            self.advisor.record(symbol, d, applied=True)
            self._levels = ("long", d.stop_loss, d.take_profit)
            stop, tp = self.entry_plan(df, len(df) - 1, "long", close, atr_value)
            return Decision(Signal.BUY, f"KI: {d.reason}", stop_loss=stop, take_profit=tp, atr=atr_value)
        if d.action == "sell" and position is not None and side != "short":
            if d.confidence < self.ai.min_confidence_exit:
                self.advisor.record(symbol, d, applied=False, note=f"Konfidenz unter {self.ai.min_confidence_exit:.2f}")
                return Decision(Signal.HOLD, f"KI-Verkauf verworfen, Konfidenz {d.confidence:.2f}", atr=atr_value)
            self.advisor.record(symbol, d, applied=True)
            return Decision(Signal.SELL, f"KI: {d.reason}", atr=atr_value)
        applied = d.action == "hold"
        self.advisor.record(symbol, d, applied=applied, note="" if applied else "passt nicht zur Position")
        if applied and position is not None and (d.stop_loss or d.take_profit):
            # offene Position: neue Werte der KI, der Bot prueft sie (Stop nur enger)
            return Decision(Signal.HOLD, f"KI: {d.reason}", stop_loss=d.stop_loss, take_profit=d.take_profit, atr=atr_value, adjust=True)
        return Decision(Signal.HOLD, f"KI: {d.reason}", atr=atr_value)

    def min_stop_pct(self, entry: float, atr_value: float) -> float:
        """Kleinster erlaubter Stop-Abstand in %: aus ATR (Rauschen) und Kosten je Trade."""
        atr_pct = atr_value / entry * 100.0 if entry and atr_value == atr_value else 0.0
        return max(self.ai.min_stop_atr * atr_pct, self.ai.min_stop_cost_mult * self.cost_pct, self.MIN_STOP_PCT)

    def entry_plan(self, df, i, side, entry, atr_value):
        """Stop und Ziel der KI, wenn sie zur Richtung passen und vernuenftig weit weg sind; sonst der eigene Plan.
        Danach Untergrenze: Stop mindestens min_stop_pct weit, Ziel mindestens min_rr x Stop-Abstand."""
        sign = -1 if side == "short" else 1
        stop, tp = None, None
        lv = self._levels
        if lv is not None and lv[0] == side and lv[1]:
            dist = sign * (entry - lv[1]) / entry * 100.0
            tp_ok = lv[2] is None or sign * (lv[2] - entry) > 0
            if self.MIN_STOP_PCT <= dist <= self.MAX_STOP_PCT and tp_ok:
                stop, tp = lv[1], lv[2]
        if stop is None:
            stop, tp = super().entry_plan(df, i, side, entry, atr_value)
        dist = max(sign * (entry - stop), entry * self.min_stop_pct(entry, atr_value) / 100.0)
        stop = entry - sign * dist
        if self.ai.min_rr > 0 and (tp is None or sign * (tp - entry) < self.ai.min_rr * dist):
            tp = entry + sign * self.ai.min_rr * dist
        return stop, tp

    def export_state(self) -> dict:
        return {"ai": self.advisor.export_state()}

    def import_state(self, state: dict) -> None:
        if "ai" in state:
            self.advisor.import_state(state["ai"])
