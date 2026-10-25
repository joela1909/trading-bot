import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tradingbot import remote_state
from tradingbot.dashboard import DashboardServer
from tradingbot.exchange import PaperFuturesBroker
from tradingbot.models import Position
from tradingbot.state import BotState


class _Store:
    """Kleiner HTTP-Speicher wie der Cloudflare-Worker: GET/PUT je Dateiname."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.puts = 0
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                body = store.files.get(self.path.lstrip("/"))
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                if body is not None:
                    self.wfile.write(body)

            def do_PUT(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                store.files[self.path.lstrip("/")] = self.rfile.read(n)
                store.puts += 1
                self.send_response(204)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setenv(remote_state.ENV_VAR, s.url)
    yield s
    s.close()


def test_disabled_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv(remote_state.ENV_VAR, raising=False)
    assert not remote_state.enabled()
    assert remote_state.push(tmp_path / "x.json", "{}") is False
    assert remote_state.pull_if_missing(tmp_path / "x.json") is False


def test_state_survives_lost_disk(store, tmp_path):
    """Container-Neustart: lokale Dateien weg, Zustand kommt aus dem Speicher zurueck."""
    first = tmp_path / "run1" / "state_paper_futures.json"
    st = BotState(positions={"SOL/USDT:USDT": Position("SOL/USDT:USDT", 2.31, 107.9, 108.28, 107.4, "t", 107.9, side="short")})
    st.entries_today = 2
    st.save(first)
    assert "state_paper_futures.json" in store.files

    second = tmp_path / "run2" / "state_paper_futures.json"  # leere Festplatte
    loaded = BotState.load(second)
    assert second.exists()
    assert loaded.positions["SOL/USDT:USDT"].side == "short" and loaded.entries_today == 2


def test_paper_futures_balance_survives_lost_disk(store, tmp_path):
    class Feed:
        def fetch_price(self, s):
            return 100.0

    b = PaperFuturesBroker(Feed(), "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "a" / "paper_futures_balances.json")
    b.open_position("BTC/USDT:USDT", "short", 1.0)
    b2 = PaperFuturesBroker(Feed(), "USDT", 1000.0, 0.1, 0.0, state_path=tmp_path / "b" / "paper_futures_balances.json")
    assert b2.balance_quote() == pytest.approx(b.balance_quote())
    assert b2.balance_base("BTC/USDT:USDT") == 1.0


def test_local_file_wins_and_errors_never_raise(monkeypatch, tmp_path):
    path = tmp_path / "state_paper.json"
    path.write_text(json.dumps(BotState().to_dict()), encoding="utf-8")
    monkeypatch.setenv(remote_state.ENV_VAR, "http://127.0.0.1:9/")  # nichts lauscht dort
    assert remote_state.pull_if_missing(path) is True, "lokale Datei hat Vorrang, kein Abruf"
    BotState().save(path)  # Speicher nicht erreichbar: nur Warnung, kein Fehler
    assert remote_state.pull_if_missing(tmp_path / "fehlt.json") is False


def test_rejects_unsafe_names(store, tmp_path):
    assert remote_state.push(tmp_path / "bad name!.json", "{}") is False
    assert store.puts == 0


def test_dashboard_container_bind_only_when_allowed():
    with pytest.raises(ValueError):
        DashboardServer(lambda: {}, host="0.0.0.0", port=0)
    srv = DashboardServer(lambda: {}, host="0.0.0.0", port=0, allow_container_bind=True)
    srv.start()
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/api/health", timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True}
    finally:
        srv.stop()
    with pytest.raises(ValueError):
        DashboardServer(lambda: {}, host="192.168.1.5", port=0, allow_container_bind=True)
