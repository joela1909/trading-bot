"""Aktienhandel neben dem Krypto-Bot: ein zweiter TradingBot im selben Prozess, mit US-Aktien von Alpaca.

Er nutzt dieselben Strategien, Risiko- und Tageshandelsregeln, hat aber ein eigenes Papierkonto (USD) und eigene
Zustandsdateien (state_paper_stocks.json, stocks_paper_balances.json). Orders gehen nie an eine Boerse."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
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
        scanner_top=0,  # Aktien haben ihren eigenen Scanner (stocks.scanner_top)
        news_minutes=0,
    )
    return Settings(bot=BotConfig(**data), secrets=settings.secrets.model_copy(update={"trading_mode": "paper"}))


def build_stock_bot(settings: Settings, feed=None, chart_feed=None, suffix: str = "") -> TradingBot:
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
                                leverage=cfg.leverage, state_path=state_dir / BALANCE_FILE.replace(".json", f"{suffix}.json"))
    state_path = state_dir / STATE_FILE.replace(".json", f"{suffix}.json")
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
            bot = self._build()
            bot.feed.check()
        except AlpacaError as exc:
            self.problem = str(exc)
            log.warning("Aktien: %s", exc)
            return
        self.bot = bot
        self._base_symbols = list(bot.cfg.symbols)
        self._scan_day: str | None = None
        if self.cfg.scanner_top > 0:
            bot.primary.before_cycle = self._refresh_universe
        if self.cfg.news_block_minutes > 0:
            if self.cfg.news_mode == "direction":
                from .insights import NewsDirectionGuard

                guard = NewsDirectionGuard(bot.feed.recent_news, self.cfg.news_block_minutes)
            else:
                guard = self._news_guard
            for b in bot.all_bots():
                b.entry_guard = guard
        self.thread = threading.Thread(target=self._run, name="aktien", daemon=True)
        self.thread.start()
        log.info("Aktien-Bot gestartet: %s, %s", ", ".join(bot.cfg.symbols), bot.cfg.timeframe)

    def _build(self):
        """Ein Aktien-Bot je Zeitrahmen (stocks.timeframe + stocks.timeframes), zusammengefasst als MultiBot."""
        from .multi_tf import MultiBot, ordered_timeframes

        primary = self.cfg.timeframe
        bots = {}
        for tf in ordered_timeframes(primary, self.cfg.timeframes):
            st = self.settings.bot.stocks.model_copy(update={"timeframe": tf})
            s = self.settings.model_copy(update={"bot": self.settings.bot.model_copy(deep=True, update={"stocks": st})})
            bots[tf] = build_stock_bot(s, suffix="" if tf == primary else f"_{tf}")
        return MultiBot(bots, primary)

    def _refresh_universe(self) -> None:
        """Einmal je Handelstag nach 9:35 New York: meistgehandelte Aktien zur festen Liste dazunehmen."""
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
        day = now.strftime("%Y-%m-%d")
        if self._scan_day == day or now.weekday() >= 5 or (now.hour, now.minute) < (9, 35) or now.hour >= 16:
            return
        self._scan_day = day
        scanned = self.bot.feed.most_actives(self.cfg.scanner_top)
        keep = [s for s in self.bot.open_symbols() if s not in self._base_symbols]  # offene Positionen bleiben
        symbols = list(dict.fromkeys(self._base_symbols + scanned + keep))
        for b in self.bot.all_bots():
            b.cfg.symbols = list(symbols)
        self.scanned = scanned
        log.info("Aktien-Scanner %s: %s", day, ", ".join(scanned) or "nichts gefunden")

    def _news_guard(self, symbol: str, side: str = "") -> tuple[bool, str]:
        news = self.bot.feed.recent_news(symbol, self.cfg.news_block_minutes)
        if news:
            return False, f"News in den letzten {self.cfg.news_block_minutes} min: {news[0][:80]}"
        return True, ""

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
