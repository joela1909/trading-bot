from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

from .dashboard_page import APP_JS, INDEX_HTML, STYLE_CSS

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

CSP = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

Snapshot = Callable[[], dict]
Action = Callable[[], dict]


class DashboardServer:
    """Lokales Dashboard. Bindet ausschliesslich an Loopback und prueft Host- und Origin-Header."""

    def __init__(self, get_snapshot: Snapshot, actions: dict[str, Action] | None = None, host: str = "127.0.0.1", port: int = 8765):
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"Dashboard darf nur an {LOOPBACK_HOSTS} binden, nicht an {host!r}")
        self.get_snapshot = get_snapshot
        self.actions = actions or {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "tradingbot-dashboard"

            def log_message(self, fmt, *args):  # noqa: A003
                log.debug("dashboard: " + fmt, *args)

            # -- helpers
            def _host_ok(self) -> bool:
                name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return name in LOOPBACK_HOSTS

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: dict) -> None:
                self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            # -- routes
            def do_GET(self):  # noqa: N802
                if not self._host_ok():
                    return self._json(403, {"error": "ungueltiger Host"})
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/app.js":
                    return self._send(200, APP_JS.encode("utf-8"), "text/javascript; charset=utf-8")
                if path == "/style.css":
                    return self._send(200, STYLE_CSS.encode("utf-8"), "text/css; charset=utf-8")
                if path == "/api/snapshot":
                    try:
                        return self._json(200, outer.get_snapshot())
                    except Exception:  # noqa: BLE001
                        log.exception("Snapshot fehlgeschlagen")
                        return self._json(500, {"error": "Snapshot fehlgeschlagen"})
                return self._json(404, {"error": "nicht gefunden"})

            def do_POST(self):  # noqa: N802
                if not self._host_ok():
                    return self._json(403, {"error": "ungueltiger Host"})
                if self.headers.get("X-Dashboard") != "1":
                    return self._json(403, {"error": "fehlender Header"})
                origin = self.headers.get("Origin")
                if origin is not None:
                    origin_host = urlparse(origin).hostname or ""
                    if origin_host not in LOOPBACK_HOSTS:
                        return self._json(403, {"error": "fremde Herkunft"})
                path = urlparse(self.path).path
                if not path.startswith("/api/"):
                    return self._json(404, {"error": "nicht gefunden"})
                action = outer.actions.get(path[len("/api/"):])
                if action is None:
                    return self._json(404, {"error": "Aktion nicht verfuegbar"})
                try:
                    return self._json(200, action())
                except Exception:  # noqa: BLE001
                    log.exception("Aktion %s fehlgeschlagen", path)
                    return self._json(500, {"error": "Aktion fehlgeschlagen"})

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.host = host
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{'localhost' if self.host == 'localhost' else self.host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
