from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Position, Trade


@dataclass
class BotState:
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    last_candle_ts: dict[str, int] = field(default_factory=dict)
    day: str | None = None
    day_start_equity: float | None = None
    entries_halted_for_day: bool = False

    def to_dict(self) -> dict:
        return {
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "trades": [t.to_dict() for t in self.trades],
            "last_candle_ts": self.last_candle_ts,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "entries_halted_for_day": self.entries_halted_for_day,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BotState":
        return cls(
            positions={k: Position.from_dict(v) for k, v in d.get("positions", {}).items()},
            trades=[Trade.from_dict(t) for t in d.get("trades", [])],
            last_candle_ts={k: int(v) for k, v in d.get("last_candle_ts", {}).items()},
            day=d.get("day"),
            day_start_equity=d.get("day_start_equity"),
            entries_halted_for_day=bool(d.get("entries_halted_for_day", False)),
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> "BotState":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
