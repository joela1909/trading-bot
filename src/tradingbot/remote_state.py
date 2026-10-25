"""Sichert Zustandsdateien zusaetzlich ausserhalb des Rechners, auf dem der Bot laeuft.

Auf Cloudflare Containers geht die Festplatte bei jedem Neustart verloren. Ist TRADINGBOT_STATE_URL gesetzt
(z. B. http://state.tradingbot/), wird jede Datei nach dem Schreiben per HTTP PUT hochgeladen und beim Start per
GET geholt, falls sie lokal fehlt. Der Worker leitet das an Cloudflare R2 weiter. Ohne die Variable passiert nichts.
Fehler werden nur geloggt: Ein kurzer Ausfall des Speichers darf den Handel nie anhalten."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

ENV_VAR = "TRADINGBOT_STATE_URL"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
TIMEOUT_SECONDS = 10.0


def _base_url() -> str | None:
    url = os.environ.get(ENV_VAR, "").strip()
    return url.rstrip("/") + "/" if url else None


def enabled() -> bool:
    return _base_url() is not None


def _url_for(path: Path) -> str | None:
    base = _base_url()
    if base is None:
        return None
    name = path.name
    if not _SAFE_NAME.match(name):
        log.warning("Zustandsdatei %s hat einen ungueltigen Namen und wird nicht gesichert", name)
        return None
    return base + name


def push(path: str | Path, text: str) -> bool:
    """Laedt den Inhalt hoch. True bei Erfolg, False bei Fehler oder wenn nichts konfiguriert ist."""
    url = _url_for(Path(path))
    if url is None:
        return False
    try:
        import httpx

        r = httpx.put(url, content=text.encode("utf-8"), headers={"Content-Type": "application/json"}, timeout=TIMEOUT_SECONDS)
        if r.status_code >= 300:
            log.warning("Zustand %s nicht gesichert: HTTP %s", Path(path).name, r.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Zustand %s nicht gesichert: %s", Path(path).name, exc)
        return False


def pull_if_missing(path: str | Path) -> bool:
    """Holt die Datei aus dem Speicher, wenn sie lokal fehlt. True, wenn sie danach lokal vorhanden ist."""
    path = Path(path)
    if path.exists():
        return True
    url = _url_for(path)
    if url is None:
        return False
    try:
        import httpx

        r = httpx.get(url, timeout=TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Zustand %s nicht abrufbar: %s", path.name, exc)
        return False
    if r.status_code == 404:
        log.info("Kein gesicherter Zustand %s vorhanden, Start mit leerem Zustand", path.name)
        return False
    if r.status_code >= 300:
        log.warning("Zustand %s nicht abrufbar: HTTP %s", path.name, r.status_code)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    log.info("Gesicherten Zustand %s wiederhergestellt (%d Bytes)", path.name, len(r.content))
    return True
