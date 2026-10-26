"""KI-Konto: ein zweiter Krypto-Papier-Bot im selben Prozess. Ein Sprachmodell entscheidet ueber Ein- und Ausstieg
(Strategie claude_advisor); Positionsgroesse, Stop, Ziel und Schutz bleiben beim Bot. Eigenes Konto und eigener
Zustand, damit sich das Ergebnis mit dem normalen Krypto-Bot vergleichen laesst."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .bot import TradingBot
from .config import BotConfig, Settings
from .dashboard import ApiError
from .data import CcxtDataFeed
from .exchange import PaperFuturesBroker
from .notify import Notifier
from .risk import RiskManager
from .state import BotState
from .strategy import build_strategy

log = logging.getLogger(__name__)

STATE_FILE = "state_paper_ai.json"
BALANCE_FILE = "ai_paper_balances.json"


def ai_settings(settings: Settings) -> Settings:
    """Einstellungen fuer das KI-Konto: Grundlage ist die Krypto-Konfiguration, ersetzt durch ai_bot.*"""
    b = settings.bot
    a = b.ai_bot
    data = b.model_dump()
    strategy = {**data["strategy"], **a.strategy, "name": "claude_advisor", "allow_short": a.allow_short,
                "realtime_signals": False, "exits": {}}
    data.update(
        mode="paper",
        market="futures",
        symbols=[s.split(":")[0] for s in a.symbols],
        timeframe=a.timeframe,
        keep_awake=False,
        paper={**data["paper"], "starting_balance": a.starting_balance},
        strategy=strategy,
        ai={**data["ai"], "provider": a.provider, "model": a.model, "fallback_models": a.fallback_models,
            "max_calls_per_day": a.max_calls_per_day,
            "min_confidence": a.min_confidence, "candles_in_prompt": a.candles_in_prompt},
        risk={**data["risk"], **a.risk},
        # die KI ist nicht nachspielbar (kein Bilanz-Filter); kein Scanner, damit die Zahl der Abfragen fest bleibt
        protections={**data["protections"], "track_record_trades": 0},
        futures={**data["futures"], "leverage": a.leverage},
        scanner_top=0,
        stocks={**data["stocks"], "enabled": False},
        ai_bot={**data["ai_bot"], "enabled": False},
    )
    return Settings(bot=BotConfig(**data), secrets=settings.secrets.model_copy(update={"trading_mode": "paper"}))


def build_ai_bot(settings: Settings, feed=None, client=None) -> TradingBot:
    s = ai_settings(settings)
    cfg = s.bot
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    feed = feed or CcxtDataFeed(cfg.exchange, market_type="futures")
    broker = PaperFuturesBroker(feed, cfg.quote_currency, cfg.paper.starting_balance, cfg.paper.fee_pct, cfg.paper.slippage_pct,
                                leverage=cfg.leverage, state_path=state_dir / BALANCE_FILE)
    state_path = state_dir / STATE_FILE
    bot = TradingBot(
        settings=s,
        feed=feed,
        broker=broker,
        strategy=build_strategy(cfg.strategy, ai=cfg.ai, client=client,
                                api_key=settings.secrets.openrouter_api_key if cfg.ai.provider == "openrouter" else settings.secrets.anthropic_api_key,
                                session=cfg.session_cfg, timeframe=cfg.timeframe),
        risk=RiskManager(cfg.risk),
        state=BotState.load(state_path),
        notifier=Notifier(),
        state_path=state_path,
    )
    if cfg.strategy.signal_source == "spot":
        bot.signal_feed = CcxtDataFeed(cfg.exchange, market_type="spot")
    bot.chart_feed = CcxtDataFeed(cfg.exchange, market_type="spot" if bot.signal_feed is not None else "futures")
    return bot


class AiRunner:
    """Startet das KI-Konto in einem eigenen Thread und beantwortet die Fragen der App."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.bot.ai_bot
        self.bot: TradingBot | None = None
        self.thread: threading.Thread | None = None
        self.problem: str | None = None

    @property
    def ready(self) -> bool:
        return self.bot is not None

    def start(self) -> None:
        if not self.cfg.enabled:
            self.problem = "KI-Konto ist aus (ai_bot.enabled in config.yaml)"
            return
        if self.cfg.provider == "anthropic" and not self.settings.secrets.anthropic_api_key:
            self.problem = "ANTHROPIC_API_KEY fehlt"
            return
        if self.cfg.provider == "openrouter" and not self.settings.secrets.openrouter_api_key:
            self.problem = "OPENROUTER_API_KEY fehlt"
            return
        try:
            self.bot = build_ai_bot(self.settings)
        except Exception as exc:  # noqa: BLE001
            self.problem = f"KI-Konto startet nicht: {exc}"
            log.warning("KI-Konto: %s", exc)
            return
        self.thread = threading.Thread(target=self._run, name="ki", daemon=True)
        self.thread.start()
        log.info("KI-Konto gestartet: %s, %s, %s", ", ".join(self.bot.cfg.symbols), self.bot.cfg.timeframe, self.cfg.model)

    def _run(self) -> None:
        try:
            self.bot.run_forever()
        except Exception:  # noqa: BLE001
            log.exception("KI-Konto abgestuerzt")
            self.problem = "Das KI-Konto ist abgestuerzt, Details im Log"

    def stop(self) -> None:
        if self.bot is not None:
            self.bot.stop()

    def snapshot(self, _params: dict | None = None) -> dict:
        base = {"enabled": self.cfg.enabled, "ready": self.ready, "problem": self.problem, "provider": self.cfg.provider,
                "model": self.cfg.model}
        if self.bot is None:
            return base
        snap = self.bot.get_snapshot()
        adv = getattr(self.bot.strategy, "advisor", None)
        if adv is not None:
            base["ai_calls_today"] = adv.calls
            base["ai_decisions"] = list(adv.decisions)[-20:]
        return {**snap, **base}

    def chart(self, params: dict) -> dict:
        if self.bot is None:
            raise ApiError("KI-Konto laeuft nicht", 409)
        symbol = params.get("symbol", "")
        if symbol not in self.bot.cfg.symbols:
            raise ApiError(f"Unbekanntes Paar {symbol!r}", 404)
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
            raise ApiError("KI-Konto laeuft nicht", 409)
        self.bot.pause()
        return self.snapshot()

    def resume(self, _body: dict | None = None) -> dict:
        if self.bot is None:
            raise ApiError("KI-Konto laeuft nicht", 409)
        self.bot.resume()
        return self.snapshot()

    def account_reset(self, body: dict) -> dict:
        if self.bot is None:
            raise ApiError("KI-Konto laeuft nicht", 409)
        try:
            return self.bot.request_account_reset(bool(body.get("clear_trades", False)))
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
