from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from .backtest import Backtester
from .bot import TradingBot
from .config import Settings, load_settings
from .data import CcxtDataFeed, load_csv
from .exchange import LiveBroker, PaperBroker
from .logging_setup import setup_logging
from .notify import Notifier, TelegramNotifier
from .risk import RiskManager
from .state import BotState
from .strategy import build_strategy

log = logging.getLogger(__name__)


def build_bot(settings: Settings) -> TradingBot:
    cfg = settings.bot
    mode = settings.mode
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    feed = CcxtDataFeed(cfg.exchange)
    if mode == "live":
        broker = LiveBroker(
            cfg.exchange,
            settings.secrets.exchange_api_key,
            settings.secrets.exchange_api_secret,
            cfg.quote_currency,
            password=settings.secrets.exchange_api_password,
        )
        log.warning("LIVE-MODUS AKTIV: Der Bot handelt mit echtem Geld auf %s.", cfg.exchange)
    else:
        broker = PaperBroker(
            feed,
            cfg.quote_currency,
            cfg.paper.starting_balance,
            cfg.paper.fee_pct,
            cfg.paper.slippage_pct,
            state_path=state_dir / "paper_balances.json",
        )

    sec = settings.secrets
    if cfg.notifications.telegram_enabled and sec.telegram_bot_token and sec.telegram_chat_id:
        notifier: Notifier = TelegramNotifier(sec.telegram_bot_token, sec.telegram_chat_id)
    else:
        notifier = Notifier()

    state_path = state_dir / f"state_{mode}.json"
    return TradingBot(
        settings=settings,
        feed=feed,
        broker=broker,
        strategy=build_strategy(cfg.strategy),
        risk=RiskManager(cfg.risk),
        state=BotState.load(state_path),
        notifier=notifier,
        state_path=state_path,
    )


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    setup_logging(settings.bot.log_dir, logging.DEBUG if args.verbose else logging.INFO)
    settings.validate_for_run()
    bot = build_bot(settings)

    if args.once:
        bot.reconcile()
        equity = bot.run_once()
        print(f"Kapital: {equity:.2f} {settings.bot.quote_currency}, offene Positionen: {len(bot.state.positions)}")
        return 0

    def _handle_signal(signum, _frame):
        log.info("Signal %s empfangen, Bot wird beendet ...", signum)
        bot.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    bot.run_forever()
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    setup_logging(settings.bot.log_dir, logging.DEBUG if args.verbose else logging.INFO)
    cfg = settings.bot
    timeframe = args.timeframe or cfg.timeframe
    symbol = args.symbol or cfg.symbols[0]

    if args.csv:
        df = load_csv(args.csv)
        limits = None
    else:
        if not args.since:
            print("Entweder --csv oder --since YYYY-MM-DD angeben.", file=sys.stderr)
            return 2
        feed = CcxtDataFeed(cfg.exchange)
        since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000)
        until_ms = int(datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc).timestamp() * 1000) if args.until else None
        log.info("Lade %s %s ab %s von %s ...", symbol, timeframe, args.since, cfg.exchange)
        df = feed.fetch_ohlcv_range(symbol, timeframe, since_ms, until_ms)
        try:
            limits = feed.market_limits(symbol)
        except Exception:  # noqa: BLE001
            limits = None
    log.info("%d Kerzen geladen", len(df))

    bt = Backtester(build_strategy(cfg.strategy), RiskManager(cfg.risk), cfg.strategy, cfg.paper, limits)
    result = bt.run(df, symbol)
    print(result.summary())
    if args.trades:
        print("\nTrades:")
        for t in result.trades:
            print(f"  {t.opened_at} -> {t.closed_at}  {t.entry_price:.4f} -> {t.exit_price:.4f}  {t.pnl_quote:+.2f} ({t.pnl_pct:+.2f}%)  {t.reason}")
    if args.export:
        import pandas as pd

        pd.DataFrame([t.to_dict() for t in result.trades]).to_csv(args.export, index=False)
        print(f"\nTrades exportiert nach {args.export}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    cfg = settings.bot
    state_path = Path(cfg.state_dir) / f"state_{settings.mode}.json"
    state = BotState.load(state_path)
    print(f"Modus: {settings.mode}   Boerse: {cfg.exchange}   Zustand: {state_path}")
    print(f"Handelstag: {state.day}   Tages-Startkapital: {state.day_start_equity}   Einstiege gestoppt: {state.entries_halted_for_day}")
    print(f"\nOffene Positionen ({len(state.positions)}):")
    for p in state.positions.values():
        print(f"  {p.symbol}: {p.amount:.6f} @ {p.entry_price:.4f}  Stop {p.stop_loss:.4f}  TP {p.take_profit}  seit {p.opened_at}")
    print(f"\nAbgeschlossene Trades ({len(state.trades)}):")
    total = 0.0
    for t in state.trades[-args.last:]:
        print(f"  {t.closed_at}  {t.symbol}  {t.pnl_quote:+.2f} ({t.pnl_pct:+.2f}%)  {t.reason}")
    for t in state.trades:
        total += t.pnl_quote
    print(f"\nRealisierter Gesamt-PnL: {total:+.2f} {cfg.quote_currency}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradingbot", description="Vollautomatischer Krypto-Trading-Bot")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Bot starten (Endlosschleife)")
    p_run.add_argument("--once", action="store_true", help="nur einen Zyklus ausfuehren")
    p_run.set_defaults(func=cmd_run)

    p_bt = sub.add_parser("backtest", help="Strategie auf historischen Daten testen")
    p_bt.add_argument("--symbol")
    p_bt.add_argument("--timeframe")
    p_bt.add_argument("--csv", help="OHLCV-CSV statt Download")
    p_bt.add_argument("--since", help="Startdatum YYYY-MM-DD (Download von der Boerse)")
    p_bt.add_argument("--until", help="Enddatum YYYY-MM-DD")
    p_bt.add_argument("--trades", action="store_true", help="alle Trades auflisten")
    p_bt.add_argument("--export", help="Trades als CSV speichern")
    p_bt.set_defaults(func=cmd_backtest)

    p_st = sub.add_parser("status", help="Positionen und Trades anzeigen")
    p_st.add_argument("--last", type=int, default=20, help="Anzahl der letzten Trades")
    p_st.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)
