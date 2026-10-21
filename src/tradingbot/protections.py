from __future__ import annotations

from .config import ProtectionConfig


class Protections:
    """Schutzmechanismen gegen Verlustserien. Bot und Backtester nutzen dieselbe Klasse mit Kerzenzeit in
    Millisekunden, damit Tests und Live-Betrieb gleich handeln.

    - Abkuehlphase: nach jedem Ausstieg wird dasselbe Paar kurz nicht gekauft.
    - Stop-Loss-Serie: bei mehreren Stop-Losses in kurzer Zeit werden alle Einstiege gesperrt.
    - Drawdown-Grenze: faellt das Kapital zu weit unter sein Hoch, werden alle Einstiege gesperrt.
    """

    def __init__(self, cfg: ProtectionConfig, timeframe_ms: int):
        self.cfg = cfg
        self.tf = timeframe_ms
        self.last_exit: dict[str, int] = {}
        self.stop_times: list[int] = []
        self.peak: float | None = None
        self.locked_until: int = 0
        self.lock_reason: str = ""

    def _lock(self, until_ms: int, reason: str) -> None:
        self.locked_until = max(self.locked_until, until_ms)
        self.lock_reason = reason

    def on_exit(self, symbol: str, reason: str, ts_ms: int) -> None:
        self.last_exit[symbol] = ts_ms
        if not reason.startswith("stop_loss"):
            return
        window = self.cfg.stoploss_guard_window_candles * self.tf
        self.stop_times = [t for t in self.stop_times if ts_ms - t <= window] + [ts_ms]
        limit = self.cfg.stoploss_guard_stops
        if limit > 0 and len(self.stop_times) >= limit:
            self._lock(ts_ms + self.cfg.stoploss_guard_lock_candles * self.tf, "Stop-Loss-Serie")
            self.stop_times.clear()

    def update_equity(self, equity: float, ts_ms: int) -> None:
        if self.peak is None or equity > self.peak:
            self.peak = equity
        limit = self.cfg.max_drawdown_pct
        if limit > 0 and self.peak and (self.peak - equity) / self.peak * 100.0 >= limit and ts_ms >= self.locked_until:
            self._lock(ts_ms + self.cfg.max_drawdown_lock_candles * self.tf, "Drawdown-Grenze")
            self.peak = equity

    def entry_allowed(self, symbol: str, ts_ms: int) -> tuple[bool, str]:
        if ts_ms < self.locked_until:
            return False, f"{self.lock_reason}, Einstiege gesperrt"
        last = self.last_exit.get(symbol)
        if self.cfg.cooldown_candles > 0 and last is not None and ts_ms - last < self.cfg.cooldown_candles * self.tf:
            return False, "Abkuehlphase nach Ausstieg"
        return True, ""

    def to_dict(self) -> dict:
        return {
            "last_exit": self.last_exit,
            "stop_times": self.stop_times,
            "peak": self.peak,
            "locked_until": self.locked_until,
            "lock_reason": self.lock_reason,
        }

    def load(self, d: dict) -> None:
        self.last_exit = {k: int(v) for k, v in d.get("last_exit", {}).items()}
        self.stop_times = [int(t) for t in d.get("stop_times", [])]
        self.peak = d.get("peak")
        self.locked_until = int(d.get("locked_until", 0))
        self.lock_reason = d.get("lock_reason", "")
