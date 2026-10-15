# Trading Bot

Vollautomatischer Krypto-Trading-Bot für den Spot-Handel (long-only) auf Basis von
[ccxt](https://github.com/ccxt/ccxt). Läuft rund um die Uhr, handelt nach einer
Trendfolge-Strategie, verwaltet Stop-Loss, Take-Profit und Trailing-Stop selbst
und begrenzt das Risiko pro Trade und pro Tag.

> **Wichtig:** Krypto-Handel kann zum Totalverlust führen. Kein Bot und keine
> Strategie garantiert Gewinne. Teste ausführlich im Papierhandel und mit
> Backtests, bevor du echtes Geld einsetzt, und setze nur Geld ein, dessen
> Verlust du verkraften kannst. Gewinne aus Krypto-Handel sind in Deutschland
> grundsätzlich steuerpflichtig; die Trade-Historie lässt sich als CSV exportieren.

## Funktionen

- **Papierhandel** (Standard): Simulation mit echten Live-Kursen, Gebühren und Slippage, ohne API-Key.
- **Live-Handel**: echte Market-Orders über ccxt (Binance, Kraken, Bybit, OKX, Coinbase, Bitget, ...).
  Muss doppelt freigeschaltet werden (`TRADING_MODE=live` und `LIVE_TRADING_CONFIRMED=true`).
- **Strategie `ema_cross`**: Kauf beim Kreuzen der schnellen über die langsame EMA mit RSI-Filter,
  Verkauf beim Gegenkreuz. Stop-Loss und Take-Profit werden über die ATR (Volatilität) bemessen,
  der Stop zieht bei steigenden Kursen nach (Trailing-Stop).
- **Risikomanagement**: Positionsgröße nach festem Risiko pro Trade, maximale Positionsgröße,
  maximale Anzahl offener Positionen, Tagesverlustlimit, Mindestordergröße der Börse.
- **Backtesting** auf historischen Daten (Download von der Börse oder CSV) mit Rendite,
  Drawdown, Trefferquote, Profitfaktor und Vergleich zu Buy & Hold.
- **Robuster Betrieb**: Zustand (Positionen, Trades) wird auf Platte gespeichert und übersteht Neustarts,
  Abgleich mit dem echten Bestand beim Start, Netzwerkfehler brechen den Bot nicht ab,
  Rotating-Logs, optional Telegram-Benachrichtigungen, Docker-Setup für 24/7.

## Installation

Voraussetzung: Python 3.11 oder neuer.

```bash
git clone https://github.com/joela1909/trading-bot.git
cd trading-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

## Schnellstart (Papierhandel)

```bash
tradingbot run
```

Der Bot lädt `config.yaml`, holt sich Kurse von der eingestellten Börse (ohne API-Key),
simuliert mit 1000 USDT Startkapital und schreibt Zustand nach `state/` und Logs nach `logs/`.
Beenden mit `Ctrl+C`.

Status anzeigen:

```bash
tradingbot status
```

Einen einzelnen Zyklus ausführen (z. B. zum Testen der Verbindung):

```bash
tradingbot run --once
```

## Backtest

Historische Daten von der Börse laden und die Strategie testen:

```bash
tradingbot backtest --symbol BTC/USDT --since 2024-01-01 --trades
```

Mit eigener CSV (Spalten `timestamp,open,high,low,close,volume`, Timestamp als Millisekunden oder ISO-Datum):

```bash
tradingbot backtest --symbol BTC/USDT --csv daten/btc_1h.csv --export trades.csv
```

Die Strategie-Parameter kommen aus `config.yaml`. Ein Backtest ersetzt keinen
Papierhandel über mehrere Wochen: er kennt weder Orderbuch noch echte Latenz.

## Konfiguration

Alle Parameter stehen kommentiert in [`config.yaml`](config.yaml). Die wichtigsten:

| Bereich | Parameter | Bedeutung |
|---|---|---|
| allgemein | `exchange`, `symbols`, `timeframe` | Börse (ccxt-ID), Handelspaare, Kerzen-Zeitrahmen |
| `strategy` | `fast_ema`, `slow_ema`, `rsi_max_entry` | Trendfilter und Überkauft-Schwelle |
| `strategy` | `stop_atr_mult`, `take_profit_atr_mult`, `trailing_stop` | Stop-Loss, Take-Profit, Trailing |
| `risk` | `risk_per_trade_pct` | Max. Verlust pro Trade in Prozent des Kapitals |
| `risk` | `max_position_pct`, `max_open_positions` | Begrenzung der Positionsgröße und -anzahl |
| `risk` | `max_daily_loss_pct` | Ab diesem Tagesverlust keine neuen Einstiege bis zum nächsten Tag |
| `paper` | `starting_balance`, `fee_pct`, `slippage_pct` | Simulationsparameter |

Geheimnisse (API-Keys, Telegram-Token) gehören ausschließlich in `.env`, niemals in `config.yaml`.

## Live-Handel freischalten

1. API-Key bei der Börse anlegen. Rechte: **nur Spot-Handel**, **kein Withdrawal**.
   Wenn möglich auf die IP des Servers beschränken.
2. In `.env` eintragen:
   ```
   TRADING_MODE=live
   LIVE_TRADING_CONFIRMED=true
   EXCHANGE_API_KEY=...
   EXCHANGE_API_SECRET=...
   ```
3. `tradingbot run --once` ausführen und prüfen, dass Guthaben und Bestände korrekt gelesen werden.
4. `tradingbot run` starten. Paper- und Live-Zustand werden getrennt gespeichert
   (`state/state_paper.json`, `state/state_live.json`).

Der Bot handelt nur mit dem freien Guthaben in der Quote-Währung (`quote_currency`).
Positionen, die du manuell verkaufst, erkennt er beim nächsten Start am Bestand und verwirft sie.

## Dauerbetrieb mit Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`state/` und `logs/` werden als Volumes gemountet, `config.yaml` wird read-only eingebunden.

## Telegram-Benachrichtigungen

Bot bei [@BotFather](https://t.me/BotFather) anlegen, Token und eigene Chat-ID in `.env` eintragen
und in `config.yaml` `notifications.telegram_enabled: true` setzen. Der Bot meldet Start, Stop,
jeden Kauf und Verkauf sowie das Erreichen des Tagesverlustlimits.

## Eigene Strategie

Neue Klasse in `src/tradingbot/strategy/` von `Strategy` ableiten, `warmup_bars`,
`prepare(df)` (Indikatoren berechnen) und `decide(df, i)` (Signal für Kerze `i`) implementieren
und in `strategy/__init__.py` registrieren. Backtester und Bot nutzen dieselbe Schnittstelle.

## Tests

```bash
pytest
```

## Projektstruktur

```
src/tradingbot/
├── cli.py            Kommandozeile: run, backtest, status
├── bot.py            Handelsschleife, Positionsverwaltung, Tagesverlustlimit
├── backtest.py       Backtester mit Kennzahlen
├── config.py         config.yaml + .env (pydantic)
├── data.py           Marktdaten über ccxt, CSV-Import
├── exchange.py       PaperBroker (Simulation) und LiveBroker (ccxt)
├── indicators.py     EMA, RSI, ATR
├── risk.py           Positionsgröße, Limits
├── state.py          Persistenter Zustand
├── notify.py         Log- und Telegram-Benachrichtigungen
└── strategy/         Strategie-Schnittstelle und ema_cross
```

## Lizenz

MIT
