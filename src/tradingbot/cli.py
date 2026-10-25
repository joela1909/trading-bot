from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from .analysis import check_lookahead, check_recursive
from .backtest import Backtester
from .bot import TradingBot
from .config import Settings, load_base_config, load_settings
from .dashboard import ApiError, DashboardServer
from .data import CcxtDataFeed, load_csv, load_history
from .exchange import LiveBroker, PaperBroker, PaperFuturesBroker
from .keep_awake import keep_awake
from .logging_setup import setup_logging
from .notify import Notifier, TelegramNotifier
from .risk import RiskManager
from .snapshot import make_snapshot
from .state import BotState
from .research import format_report, grid_dataframe, run_comparison
from .strategy import LIVE_ONLY, REGISTRY, build_strategy
from .synthetic import make_series

log = logging.getLogger(__name__)


def build_bot(settings: Settings) -> TradingBot:
    cfg = settings.bot
    mode = settings.mode
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    feed = CcxtDataFeed(cfg.exchange, market_type=cfg.market)
    if mode == "live":
        broker = LiveBroker(
            cfg.exchange,
            settings.secrets.exchange_api_key,
            settings.secrets.exchange_api_secret,
            cfg.quote_currency,
            password=settings.secrets.exchange_api_password,
        )
        log.warning("LIVE-MODUS AKTIV: Der Bot handelt mit echtem Geld auf %s.", cfg.exchange)
    elif cfg.is_futures:
        broker = PaperFuturesBroker(
            feed,
            cfg.quote_currency,
            cfg.paper.starting_balance,
            cfg.paper.fee_pct,
            cfg.paper.slippage_pct,
            leverage=cfg.leverage,
            state_path=state_dir / "paper_futures_balances.json",
        )
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

    # Futures bekommen einen eigenen Zustand, damit sich Spot- und Futures-Positionen nie vermischen
    state_path = state_dir / (f"state_{mode}_futures.json" if cfg.is_futures else f"state_{mode}.json")
    bot = TradingBot(
        settings=settings,
        feed=feed,
        broker=broker,
        strategy=build_strategy(cfg.strategy, ai=cfg.ai, api_key=sec.anthropic_api_key, session=cfg.session_cfg, timeframe=cfg.timeframe),
        risk=RiskManager(cfg.risk),
        state=BotState.load(state_path),
        notifier=notifier,
        state_path=state_path,
    )
    spot_signals = cfg.strategy.signal_source == "spot" and cfg.market != "spot"
    if spot_signals:
        # Signale und Chart aus Spot-Kursen (wie der TradingView-Chart auf dem Spot-Paar), gehandelt auf cfg.market
        bot.signal_feed = CcxtDataFeed(cfg.exchange, market_type="spot")
    bot.chart_feed = CcxtDataFeed(cfg.exchange, market_type="spot" if spot_signals else cfg.market)  # eigener Zugang fuer den Chart
    return bot


def _app_queries(bot: TradingBot, settings: Settings, stocks=None) -> dict:
    from .app_settings import current

    def chart(params: dict) -> dict:
        symbol = params.get("symbol", "")
        if symbol not in bot.cfg.symbols:
            raise ApiError(f"Unbekanntes Paar {symbol!r}", 404)
        tf = params.get("tf") or None
        if tf is not None and tf not in bot.CHART_TIMEFRAMES:
            raise ApiError(f"Unbekannter Zeitrahmen {tf!r}")
        try:
            since = int(params["since"]) if params.get("since") else None
        except ValueError:
            raise ApiError("since muss eine Zahl sein (ms)") from None
        return bot.chart(symbol, tf, since)

    queries = {
        "chart": chart,
        "settings": lambda _p: current(settings),
    }
    if stocks is not None:
        queries.update({
            "stocks": stocks.snapshot, "stocks-chart": stocks.chart,
            "stocks-settings": lambda _p: current(settings, market="stocks"),
        })
    return queries


def _app_commands(bot: TradingBot, settings: Settings, config_path: str, stocks=None) -> dict:
    from .app_settings import reset, validate_and_save

    def save(body: dict) -> dict:
        try:
            validate_and_save(settings, load_base_config(config_path), body)
        except ValueError as exc:
            raise ApiError(str(exc)) from exc
        threading.Timer(1.0, bot.request_restart).start()  # erst antworten, dann neu starten
        return {"ok": True, "restart": True}

    def reset_settings(_body: dict) -> dict:
        reset(settings, market="crypto")
        threading.Timer(1.0, bot.request_restart).start()
        return {"ok": True, "restart": True}

    def delete_trades(body: dict) -> dict:
        count = body.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10000:
            raise ApiError("count: ganze Zahl von 1 bis 10000 erwartet")
        return bot.delete_oldest_trades(count)

    def account_reset(body: dict) -> dict:
        try:
            return bot.request_account_reset(bool(body.get("clear_trades", False)))
        except ValueError as exc:
            raise ApiError(str(exc)) from exc

    def fees_reprice(body: dict) -> dict:
        try:
            return bot.request_fee_reprice()
        except ValueError as exc:
            raise ApiError(str(exc)) from exc

    commands = {"settings": save, "settings-reset": reset_settings, "trades-delete-oldest": delete_trades, "account-reset": account_reset,
                "fees-reprice": fees_reprice}
    if stocks is not None:
        def save_stocks(body: dict) -> dict:
            try:
                validate_and_save(settings, load_base_config(config_path), body, market="stocks")
            except ValueError as exc:
                raise ApiError(str(exc)) from exc
            threading.Timer(1.0, bot.request_restart).start()
            return {"ok": True, "restart": True}

        def reset_stocks(_body: dict) -> dict:
            reset(settings, market="stocks")
            threading.Timer(1.0, bot.request_restart).start()
            return {"ok": True, "restart": True}

        def stock_account_reset(body: dict) -> dict:
            if stocks.bot is None:
                raise ApiError("Aktienhandel laeuft nicht", 409)
            try:
                return stocks.bot.request_account_reset(bool(body.get("clear_trades", False)))
            except ValueError as exc:
                raise ApiError(str(exc)) from exc

        def delete_stock_trades(body: dict) -> dict:
            count = body.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10000:
                raise ApiError("count: ganze Zahl von 1 bis 10000 erwartet")
            if stocks.bot is None:
                raise ApiError("Aktienhandel laeuft nicht", 409)
            return stocks.bot.delete_oldest_trades(count)

        commands.update({
            "stocks-pause": stocks.pause, "stocks-resume": stocks.resume, "stocks-settings": save_stocks,
            "stocks-settings-reset": reset_stocks, "stocks-trades-delete-oldest": delete_stock_trades,
            "stocks-account-reset": stock_account_reset,
        })
    return commands


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

    # Aktien laufen als zweiter Bot im selben Prozess (nur Papierhandel, braucht Alpaca-Schluessel)
    from .stocks import StockRunner

    stocks = StockRunner(settings)
    stocks.start()

    server = None
    if settings.bot.dashboard.enabled and not args.no_dashboard:
        port = args.port if args.port is not None else settings.bot.dashboard.port
        try:
            hosts = list(settings.bot.dashboard.allowed_hosts) + settings.secrets.dashboard_host_list
            in_container = os.environ.get("TRADINGBOT_CONTAINER") == "1"
            bind = os.environ.get("DASHBOARD_BIND", "127.0.0.1") if in_container else "127.0.0.1"
            server = DashboardServer(
                bot.get_snapshot, {"pause": bot.pause, "resume": bot.resume}, host=bind, port=port,
                allowed_hosts=hosts, allow_container_bind=in_container,
                queries=_app_queries(bot, settings, stocks),
                commands=_app_commands(bot, settings, args.config, stocks),
            )
            server.start()
            log.info("Dashboard: %s (nur von diesem Rechner erreichbar)", server.url)
            if hosts:
                log.info("Dashboard ausserdem ueber Reverse-Proxy erlaubt fuer: %s", ", ".join(hosts))
            if args.open:
                webbrowser.open(server.url)
        except OSError as exc:
            log.warning("Dashboard konnte nicht starten (%s). Der Bot laeuft ohne Dashboard weiter.", exc)
    awake = settings.bot.keep_awake and keep_awake(True)
    if awake:
        log.info("Ruhemodus von Windows ist verhindert, solange der Bot laeuft")
    try:
        bot.run_forever()
    finally:
        stocks.stop()
        if awake:
            keep_awake(False)
        if server is not None:
            server.stop()
    return bot.exit_code


def cmd_dashboard(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    cfg = settings.bot
    state_path = Path(cfg.state_dir) / (f"state_{settings.mode}_futures.json" if cfg.is_futures else f"state_{settings.mode}.json")
    port = args.port if args.port is not None else cfg.dashboard.port

    def snapshot() -> dict:
        state = BotState.load(state_path)
        return make_snapshot(
            state=state,
            mode=settings.mode,
            exchange=cfg.exchange,
            timeframe=cfg.timeframe,
            quote=cfg.quote_currency,
            symbols=cfg.symbols,
            equity=None,
            free_quote=None,
            prices={},
            last_cycle=state.equity_history[-1][0] if state.equity_history else None,
            running=False,
            controls=False,
            ai_meta=(
                {"model": cfg.ai.model, "effort": cfg.ai.effort, "max_calls": cfg.ai.max_calls_per_day, "min_confidence": cfg.ai.min_confidence}
                if cfg.strategy.name in LIVE_ONLY
                else None
            ),
        )

    try:
        server = DashboardServer(snapshot, port=port)
    except OSError as exc:
        print(f"Port {port} ist nicht verfuegbar: {exc}\nLaeuft der Bot schon? Dann ist das Dashboard dort unter http://127.0.0.1:{port} erreichbar.", file=sys.stderr)
        return 1
    server.start()
    print(f"Dashboard (nur Ansicht, Stand aus {state_path}): {server.url}\nBeenden mit Strg+C")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.wait(1.0):
        pass
    server.stop()
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    setup_logging(settings.bot.log_dir, logging.DEBUG if args.verbose else logging.INFO)
    cfg = settings.bot
    timeframe = args.timeframe or cfg.timeframe
    symbol = cfg.market_symbol(args.symbol) if args.symbol else cfg.symbols[0]
    if cfg.strategy.name in LIVE_ONLY:
        print(f"Die Strategie {cfg.strategy.name} ist nicht backtestbar. Nutze den Papierhandel: tradingbot run", file=sys.stderr)
        return 2

    if args.csv:
        df = load_csv(args.csv)
        limits = None
    else:
        if not args.since:
            print("Entweder --csv oder --since YYYY-MM-DD angeben.", file=sys.stderr)
            return 2
        feed = CcxtDataFeed(cfg.exchange, market_type=cfg.market)
        since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000)
        until_ms = int(datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc).timestamp() * 1000) if args.until else None
        log.info("Lade %s %s ab %s von %s ...", symbol, timeframe, args.since, cfg.exchange)
        df = feed.fetch_ohlcv_range(symbol, timeframe, since_ms, until_ms)
        try:
            limits = feed.market_limits(symbol)
        except Exception:  # noqa: BLE001
            limits = None
    log.info("%d Kerzen geladen", len(df))

    daytrading = None if args.no_daytrading else cfg.session_cfg
    bt = Backtester(
        build_strategy(cfg.strategy, session=daytrading, timeframe=timeframe), RiskManager(cfg.risk), cfg.strategy, cfg.paper, limits,
        protections_cfg=cfg.protections, daytrading=daytrading, leverage=cfg.leverage,
    )
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


def _load_market_data(args: argparse.Namespace, cfg, timeframe: str):
    """Gemeinsame Datenbeschaffung fuer compare und check. Gibt None bei Fehlern zurueck."""
    if args.demo:
        data = {f"DEMO{i + 1}/USDT": make_series(args.demo, args.demo_bars, seed=100 + i) for i in range(3)}
        print(f"\n*** DEMO-MODUS: synthetische '{args.demo}'-Daten, keine echten Kurse ***\n")
        return data
    if not args.since:
        print("Bitte --since YYYY-MM-DD angeben (oder --demo trend|meanrev|random zum Ausprobieren).", file=sys.stderr)
        return None
    symbols = [cfg.market_symbol(x.strip()) for x in args.symbols.split(",")] if args.symbols else cfg.symbols
    feed = CcxtDataFeed(cfg.exchange, market_type=cfg.market)
    return {s: load_history(feed, s, timeframe, args.since, args.until, args.cache_dir) for s in symbols}


def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    setup_logging(settings.bot.log_dir, logging.DEBUG if args.verbose else logging.INFO)
    cfg = settings.bot
    timeframe = args.timeframe or cfg.timeframe
    names = [n.strip() for n in args.strategies.split(",")] if args.strategies else list(REGISTRY)
    live_only = [n for n in names if n in LIVE_ONLY]
    if live_only:
        print(f"{live_only} ist nicht backtestbar (die KI kennt die Vergangenheit teilweise aus dem Training). Nur Papier- oder Live-Handel.", file=sys.stderr)
        return 2
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        print(f"Unbekannte Strategie(n): {unknown}. Verfuegbar: {sorted(REGISTRY)}", file=sys.stderr)
        return 2
    data = _load_market_data(args, cfg, timeframe)
    if data is None:
        return 2
    symbol = next(iter(data))
    daytrading = None if args.no_daytrading else cfg.session_cfg
    print(f"Pruefung auf {symbol}, {len(data[symbol])} Kerzen\n")
    print(f"{'Strategie':24s} {'Blick in die Zukunft':22s} {'Live-Kerzen vs. Historie'}")
    failed = False
    for name in names:
        strat = build_strategy(cfg.strategy.model_copy(update={"name": name}), session=daytrading, timeframe=timeframe)
        look = check_lookahead(strat, data[symbol])
        rec = check_recursive(strat, data[symbol])
        look_text = f"ok ({look.checked} Stellen)" if look.ok else f"FEHLER bei {len(look.mismatches)} Stellen"
        rec_text = f"ok ({rec.mismatch_pct:.1f}% Abweichung)" if rec.ok else f"WARNUNG ({rec.mismatch_pct:.1f}% Abweichung)"
        print(f"{name:24s} {look_text:22s} {rec_text}")
        failed |= not look.ok
    return 1 if failed else 0


def cmd_compare(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    setup_logging(settings.bot.log_dir, logging.DEBUG if args.verbose else logging.INFO)
    cfg = settings.bot
    timeframe = args.timeframe or cfg.timeframe
    names = [n.strip() for n in args.strategies.split(",")] if args.strategies else list(REGISTRY)
    live_only = [n for n in names if n in LIVE_ONLY]
    if live_only:
        print(f"{live_only} ist nicht backtestbar (die KI kennt die Vergangenheit teilweise aus dem Training). Nur Papier- oder Live-Handel.", file=sys.stderr)
        return 2
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        print(f"Unbekannte Strategie(n): {unknown}. Verfuegbar: {sorted(REGISTRY)}", file=sys.stderr)
        return 2

    data = _load_market_data(args, cfg, timeframe)
    if data is None:
        return 2

    notes: list[str] = []
    usable: list[str] = []
    first_symbol = next(iter(data))
    daytrading = None if args.no_daytrading else cfg.session_cfg
    for name in names:
        strat = build_strategy(cfg.strategy.model_copy(update={"name": name}), session=daytrading, timeframe=timeframe)
        look = check_lookahead(strat, data[first_symbol])
        rec = check_recursive(strat, data[first_symbol])
        if not look.ok:
            print(f"! {name}: Blick in die Zukunft erkannt, Strategie wird nicht getestet.")
            notes.append(f"{name}: **ausgeschlossen**, die Signale aendern sich, wenn spaetere Kerzen hinzukommen (Blick in die Zukunft).")
            continue
        usable.append(name)
        if not rec.ok:
            print(f"! {name}: Live-Bot kaeme bei {rec.mismatch_pct:.1f}% der Pruefstellen zu anderen Signalen als der Backtest.")
            notes.append(f"{name}: Warnung, mit nur den Live-Kerzen weichen {rec.mismatch_pct:.1f}% der Signale vom Backtest ab.")
    if not usable:
        print("Keine Strategie hat die Pruefung bestanden.", file=sys.stderr)
        return 1
    names = usable

    comparison = run_comparison(
        data,
        names,
        cfg.strategy,
        cfg.risk,
        cfg.paper,
        timeframe,
        split=args.split,
        min_trades=args.min_trades,
        progress=lambda msg: print(msg, flush=True),
        protections=None if args.no_protections else cfg.protections,
        metric=args.metric,
        daytrading=daytrading,
        leverage=cfg.leverage,
    )
    report = format_report(comparison, synthetic=bool(args.demo), notes=notes)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = out / f"vergleich_{stamp}.md"
    grid_path = out / f"vergleich_{stamp}_alle_varianten.csv"
    report_path.write_text(report, encoding="utf-8")
    grid_dataframe(comparison).to_csv(grid_path, index=False)
    print("\n" + report)
    print(f"Bericht: {report_path}\nAlle Varianten: {grid_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    cfg = settings.bot
    state_path = Path(cfg.state_dir) / (f"state_{settings.mode}_futures.json" if cfg.is_futures else f"state_{settings.mode}.json")
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
    p_run.add_argument("--no-dashboard", action="store_true", help="Dashboard nicht starten")
    p_run.add_argument("--port", type=int, help="Dashboard-Port (Standard aus config.yaml)")
    p_run.add_argument("--open", action="store_true", help="Dashboard im Browser oeffnen")
    p_run.set_defaults(func=cmd_run)

    p_dash = sub.add_parser("dashboard", help="Dashboard nur als Ansicht starten (liest den gespeicherten Zustand)")
    p_dash.add_argument("--port", type=int, help="Dashboard-Port (Standard aus config.yaml)")
    p_dash.set_defaults(func=cmd_dashboard)

    p_bt = sub.add_parser("backtest", help="Strategie auf historischen Daten testen")
    p_bt.add_argument("--symbol")
    p_bt.add_argument("--timeframe")
    p_bt.add_argument("--csv", help="OHLCV-CSV statt Download")
    p_bt.add_argument("--since", help="Startdatum YYYY-MM-DD (Download von der Boerse)")
    p_bt.add_argument("--until", help="Enddatum YYYY-MM-DD")
    p_bt.add_argument("--trades", action="store_true", help="alle Trades auflisten")
    p_bt.add_argument("--export", help="Trades als CSV speichern")
    p_bt.add_argument("--no-daytrading", action="store_true", help="Tageshandel-Regeln aus der Konfiguration ignorieren")
    p_bt.set_defaults(func=cmd_backtest)

    p_chk = sub.add_parser("check", help="Strategien auf Blick in die Zukunft und Abweichung zwischen Backtest und Live pruefen")
    p_chk.add_argument("--symbols", help="Das erste Paar wird geprueft (Standard: aus config.yaml)")
    p_chk.add_argument("--timeframe")
    p_chk.add_argument("--since", help="Startdatum YYYY-MM-DD")
    p_chk.add_argument("--until")
    p_chk.add_argument("--strategies", help="Komma-getrennte Auswahl (Standard: alle)")
    p_chk.add_argument("--cache-dir", default="data")
    p_chk.add_argument("--demo", choices=["trend", "meanrev", "random"])
    p_chk.add_argument("--demo-bars", type=int, default=6000, help=argparse.SUPPRESS)
    p_chk.add_argument("--no-daytrading", action="store_true", help="Tageshandel-Regeln aus der Konfiguration ignorieren")
    p_chk.set_defaults(func=cmd_check)

    p_cmp = sub.add_parser("compare", help="Alle Strategien mit vielen Parametern vergleichen und die robusteste finden")
    p_cmp.add_argument("--symbols", help="Komma-getrennt, z. B. BTC/USDT,ETH/USDT,SOL/USDT (Standard: aus config.yaml)")
    p_cmp.add_argument("--timeframe", help="Zeitrahmen, z. B. 1h oder 4h (Standard: aus config.yaml)")
    p_cmp.add_argument("--since", help="Startdatum YYYY-MM-DD, empfohlen sind mindestens 3 Jahre")
    p_cmp.add_argument("--until", help="Enddatum YYYY-MM-DD (Standard: heute)")
    p_cmp.add_argument("--strategies", help="Komma-getrennte Auswahl (Standard: alle)")
    p_cmp.add_argument("--split", type=float, default=0.6, help="Anteil der Daten fuer die Parameterwahl, Rest ist Pruefzeitraum")
    p_cmp.add_argument("--min-trades", type=int, default=10, help="Mindestzahl Trades im Auswahlzeitraum")
    p_cmp.add_argument("--cache-dir", default="data", help="Zwischenspeicher fuer heruntergeladene Kerzen")
    p_cmp.add_argument("--out", default="reports", help="Ordner fuer den Bericht")
    p_cmp.add_argument("--demo", choices=["trend", "meanrev", "random"], help="Mit synthetischen Daten ausprobieren")
    p_cmp.add_argument("--metric", default="calmar", choices=["calmar", "sharpe", "sortino", "profit"], help="Bewertungsmass fuer die Parameterwahl")
    p_cmp.add_argument("--no-protections", action="store_true", help="Schutzmechanismen im Test abschalten")
    p_cmp.add_argument("--no-daytrading", action="store_true", help="Tageshandel-Regeln aus der Konfiguration ignorieren")
    p_cmp.add_argument("--demo-bars", type=int, default=20000, help=argparse.SUPPRESS)
    p_cmp.set_defaults(func=cmd_compare)

    p_st = sub.add_parser("status", help="Positionen und Trades anzeigen")
    p_st.add_argument("--last", type=int, default=20, help="Anzahl der letzten Trades")
    p_st.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
