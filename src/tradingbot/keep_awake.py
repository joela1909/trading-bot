"""Haelt Windows wach, solange der Bot laeuft, so wie es ein Videoplayer tut.

Es wird keine Energieeinstellung geaendert: Die Anforderung gilt nur, solange der Prozess laeuft, und endet mit ihm.
Der Bildschirm darf weiter ausgehen. Zugeklappte Laptops koennen je nach Einstellung trotzdem schlafen."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> bool:
    """True, wenn Windows die Anforderung angenommen hat. Auf anderen Systemen passiert nichts."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as exc:  # noqa: BLE001
        log.warning("Ruhemodus konnte nicht verhindert werden: %s", exc)
        return False
