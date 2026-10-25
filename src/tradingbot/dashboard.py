from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WEB_DIR = Path(__file__).parent / "web"
MAX_BODY_BYTES = 64 * 1024

# Der Hash erlaubt genau einen Style-Block: den des TradingView-Logos im Chart (Lizenz verlangt die Namensnennung).
# Gilt fuer lightweight-charts 5.2.1. Nach einem Update meldet die Browser-Konsole den neuen Hash.
CHART_LOGO_STYLE_HASH = "'sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY='"
CSP = (
    f"default-src 'self'; style-src 'self' {CHART_LOGO_STYLE_HASH}; script-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
}
# Dateien, die an der Wurzel liegen muessen (Service Worker gilt sonst nicht fuer die ganze App)
ROOT_FILES = {"/": "index.html", "/index.html": "index.html", "/sw.js": "sw.js", "/manifest.webmanifest": "manifest.webmanifest"}

Snapshot = Callable[[], dict]
Action = Callable[[], dict]
Query = Callable[[dict], dict]  # GET /api/<name>?a=b  -> Parameter als dict
Command = Callable[[dict], dict]  # POST /api/<name> mit JSON-Objekt


class ApiError(Exception):
    """Fachlicher Fehler, der als Meldung an die App geht (z. B. ungueltige Einstellung)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _static_file(rel: str) -> Path | None:
    """Datei unter web/, ohne aus dem Ordner herauszukommen."""
    try:
        path = (WEB_DIR / rel).resolve()
        path.relative_to(WEB_DIR.resolve())
    except (ValueError, OSError):
        return None
    if not path.is_file() or path.suffix not in STATIC_TYPES:
        return None
    return path


class DashboardServer:
    """Dashboard-App des Bots. Bindet an Loopback und prueft Host- und Origin-Header.

    allowed_hosts: weitere Host-Namen, unter denen ein vorgeschalteter Reverse-Proxy (z. B. Caddy mit HTTPS und
    Passwort) das Dashboard ausliefert. allow_container_bind: in einem Container ohne eigene oeffentliche Adresse
    (Cloudflare Containers) an 0.0.0.0 lauschen, damit der Worker davor ihn erreicht."""

    def __init__(
        self,
        get_snapshot: Snapshot,
        actions: dict[str, Action] | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        allowed_hosts: tuple[str, ...] | list[str] = (),
        allow_container_bind: bool = False,
        queries: dict[str, Query] | None = None,
        commands: dict[str, Command] | None = None,
    ):
        if host not in LOOPBACK_HOSTS and not (allow_container_bind and host == "0.0.0.0"):
            raise ValueError(f"Dashboard darf nur an {LOOPBACK_HOSTS} binden, nicht an {host!r}")
        self.allowed_names = frozenset(LOOPBACK_HOSTS) | {h.strip().lower() for h in allowed_hosts if h.strip()}
        self.get_snapshot = get_snapshot
        self.actions = actions or {}
        self.queries = queries or {}
        self.commands = commands or {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "tradingbot-dashboard"

            def log_message(self, fmt, *args):  # noqa: A003
                log.debug("dashboard: " + fmt, *args)

            # -- helpers
            def _host_ok(self) -> bool:
                name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return name in outer.allowed_names

            def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: dict) -> None:
                self._send(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

            def _run(self, label: str, fn, *args) -> None:
                try:
                    return self._json(200, fn(*args))
                except ApiError as exc:
                    return self._json(exc.status, {"error": str(exc)})
                except Exception:  # noqa: BLE001
                    log.exception("%s fehlgeschlagen", label)
                    return self._json(500, {"error": f"{label} fehlgeschlagen"})

            # -- routes
            def do_GET(self):  # noqa: N802
                if not self._host_ok():
                    return self._json(403, {"error": "ungueltiger Host"})
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/api/health":
                    return self._json(200, {"ok": True})
                if path == "/api/snapshot":
                    return self._run("Snapshot", outer.get_snapshot)
                if path.startswith("/api/"):
                    query = outer.queries.get(path[len("/api/"):])
                    if query is None:
                        return self._json(404, {"error": "nicht gefunden"})
                    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    return self._run(path, query, params)
                rel = ROOT_FILES.get(path) or (path[len("/static/"):] if path.startswith("/static/") else None)
                file = _static_file(rel) if rel else None
                if file is None:
                    return self._json(404, {"error": "nicht gefunden"})
                # App-Dateien immer frisch pruefen lassen, damit ein Update sofort ankommt
                return self._send(200, file.read_bytes(), STATIC_TYPES[file.suffix], cache="no-cache")

            def do_POST(self):  # noqa: N802
                if not self._host_ok():
                    return self._json(403, {"error": "ungueltiger Host"})
                if self.headers.get("X-Dashboard") != "1":
                    return self._json(403, {"error": "fehlender Header"})
                origin = self.headers.get("Origin")
                if origin is not None:
                    origin_host = urlparse(origin).hostname or ""
                    if origin_host.lower() not in outer.allowed_names:
                        return self._json(403, {"error": "fremde Herkunft"})
                path = urlparse(self.path).path
                if not path.startswith("/api/"):
                    return self._json(404, {"error": "nicht gefunden"})
                name = path[len("/api/"):]
                if name in outer.commands:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY_BYTES:
                        return self._json(413, {"error": "Anfrage zu gross"})
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        return self._json(400, {"error": "ungueltiges JSON"})
                    if not isinstance(body, dict):
                        return self._json(400, {"error": "JSON-Objekt erwartet"})
                    return self._run(path, outer.commands[name], body)
                action = outer.actions.get(name)
                if action is None:
                    return self._json(404, {"error": "Aktion nicht verfuegbar"})
                return self._run(path, action)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.host = host
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        return f"http://{shown}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
