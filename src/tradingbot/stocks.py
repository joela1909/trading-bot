"""Aktienhandel neben dem Krypto-Bot: ein zweiter TradingBot im selben Prozess, mit US-Aktien von Alpaca.

Er nutzt dieselben Strategien, Risiko- und Tageshandelsregeln, hat aber ein eigenes Papierkonto (USD) und eigene
Zustandsdateien (state_paper_stocks.json, stocks_paper_balances.json). Orders gehen nie an eine Boerse."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .alpaca import AlpacaDataFeed, AlpacaError, us_market_open
from .bot import TradingBot
from .config import BotConfig, Settings
from .dashboard import ApiError
from .exchange import PaperFuturesBroker
from .notify import Notifier
from .risk import RiskManager
from .state import BotState
from .strategy import build_strategy

log = logging.getLogger(__name__)

STATE_FILE = "state_paper_stocks.json"
BALANCE_FILE = "stocks_paper_balances.json"


def stock_settings(settings: Settings) -> Settings:
    """Einstellungen fuer den Aktien-Bot: Grundlage ist die Krypto-Konfiguration, ersetzt durch stocks.*"""
    b = settings.bot
    st = b.stocks
    data = b.model_dump()
    strategy = {**data["strategy"], "allow_short": st.allow_short, **st.strategy}
    data.update(
        mode="paper",
        exchange="alpaca",
        market="stocks",
        symbols=list(st.symbols),
        quote_currency="USD",
        timeframe=st.timeframe,
        poll_interval_seconds=st.poll_interval_seconds,
        keep_awake=False,
        paper={**data["paper"], "starting_balance": st.starting_balance, "fee_pct": st.fee_pct, "slippage_pct": st.slippage_pct},
        strategy=strategy,
        risk={**data["risk"], **st.risk},
        protections={**data["protections"], **st.protections},
        daytrading=st.daytrading.model_dump(),
        futures={**data["futures"], "leverage": st.leverage},
        stocks={**data["stocks"], "enabled": False},
    )
    return Settings(bot=BotConfig(**data), secrets=settings.secrets.model_copy(update={"trading_mode": "paper"}))


def build_stock_bot(settings: Settings, feed=None, chart_feed=None) -> TradingBot:
    s = stock_settings(settings)
    cfg = s.bot
    sec = settings.secrets
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if feed is None:
        feed = AlpacaDataFeed(sec.alpaca_api_key, sec.alpaca_api_secret, feed=settings.bot.stocks.data_feed)
        chart_feed = AlpacaDataFeed(sec.alpaca_api_key, sec.alpaca_api_secret, feed=settings.bot.stocks.data_feed)
    # Futures-Papierkonto: kann Long und Short wie ein Aktienkonto mit Leerverkauf, Hebel wie eingestellt
    broker = PaperFuturesBroker(feed, cfg.quote_currency, cfg.paper.starting_balance, cfg.paper.fee_pct, cfg.paper.slippage_pct,
                                leverage=cfg.leverage, state_path=state_dir / BALANCE_FILE)
    state_path = state_dir / STATE_FILE
    bot = TradingBot(
        settings=s,
        feed=feed,
        broker=broker,
        strategy=build_strategy(cfg.strategy, session=cfg.session_cfg, timeframe=cfg.timeframe),
        risk=RiskManager(cfg.risk),
        state=BotState.load(state_path),
        notifier=Notifier(),
        state_path=state_path,
    )
    bot.chart_feed = chart_feed
    return bot


class StockRunner:
    """Startet den Aktien-Bot in einem eigenen Thread und beantwortet die Fragen der App."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.bot.stocks
        self.bot: TradingBot | None = None
        self.thread: threading.Thread | None = None
        self.problem: str | None = None

    @property
    def ready(self) -> bool:
        return self.bot is not None

    def start(self) -> None:
        if not self.cfg.enabled:
            self.problem = "Aktienhandel ist aus (stocks.enabled in config.yaml)"
            return
        sec = self.settings.secrets
        if not sec.alpaca_api_key or not sec.alpaca_api_secret:
            self.problem = "Alpaca-Schluessel fehlen"
            log.warning("Aktien: %s. Ohne sie startet nur der Krypto-Bot.", self.problem)
            return
        try:
            bot = build_stock_bot(self.settings)
            bot.feed.check()
        except AlpacaError as exc:
            self.problem = str(exc)
            log.warning("Aktien: %s", exc)
            return
        self.bot = bot
        self.thread = threading.Thread(target=self._run, name="aktien", daemon=True)
        self.thread.start()
        log.info("Aktien-Bot gestartet: %s, %s", ", ".join(bot.cfg.symbols), bot.cfg.timeframe)

    def _run(self) -> None:
        try:
            self.bot.run_forever()
        except Exception:  # noqa: BLE001
            log.exception("Aktien-Bot abgestuerzt")
            self.problem = "Der Aktien-Bot ist abgestuerzt, Details im Log"

    def stop(self) -> None:
        if self.bot is not None:
            self.bot.stop()

    def snapshot(self, _params: dict | None = None) -> dict:
        base = {"enabled": self.cfg.enabled, "ready": self.ready, "problem": self.problem, "market_open": us_market_open(),
                "symbols": list(self.cfg.symbols), "data_feed": self.cfg.data_feed}
        if self.bot is None:
            return base
        return {**self.bot.get_snapshot(), **base}

    def chart(self, params: dict) -> dict:
        if self.bot is None:
            raise ApiError("Aktienhandel laeuft nicht", 409)
        symbol = params.get("symbol", "")
        if symbol not in self.bot.cfg.symbols:
            raise ApiError(f"Unbekannte Aktie {symbol!r}", 404)
        tf = params.get("tf") or None
        if tf is not None and tf not in TradingBot.CHART_TIMEFRAMES:
            raise ApiError(f"Unbekannter Zeitrahmen {tf!r}")
        try:
            since = int(params["since"]) if params.get("since") else None
        except ValueError:
            raise ApiError("since muss eine Zahl sein (ms)") from None
        return self.bot.chart(symbol, tf, since)

    def pause(self, _body: dict | None = None) -> dict:
        if self.bot is None:
            raise ApiError("Aktienhandel laeuft nicht", 409)
        self.bot.pause()
        return self.snapshot()

    def resume(self, _body: dict | None = None) -> dict:
        if self.bot is None:
            raise ApiError("Aktienhandel laeuft nicht", 409)
        self.bot.resume()
        return self.snapshot()
