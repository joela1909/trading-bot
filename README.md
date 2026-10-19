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
- **Fünf Strategien**, alle mit ATR-basiertem Stop-Loss, Take-Profit und Trailing-Stop:
  `ema_cross` (EMA-Kreuz mit RSI-Filter), `donchian` (Ausbruch), `macd_trend` (MACD-Kreuz),
  `rsi_reversion` und `bollinger_reversion` (Rückkehr zum Mittelwert). Gewählt wird in `config.yaml` unter `strategy.name`.
- **Strategievergleich** (`tradingbot compare`): testet alle Strategien mit vielen Parametern auf mehreren Coins
  und prüft die Gewinner auf einem Zeitraum, der bei der Auswahl nie benutzt wurde.
- **Risikomanagement**: Positionsgröße nach festem Risiko pro Trade, maximale Positionsgröße,
  maximale Anzahl offener Positionen, Tagesverlustlimit, Mindestordergröße der Börse.
- **Backtesting** auf historischen Daten (Download von der Börse oder CSV) mit Rendite,
  Drawdown, Trefferquote, Profitfaktor und Vergleich zu Buy & Hold.
- **Lokales Dashboard** im Browser: Kapitalverlauf, offene Positionen, Trades, Kennzahlen und ein
  Schalter zum Pausieren neuer Einstiege. Nur von deinem eigenen Rechner aus erreichbar.
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

Das Dashboard startet automatisch und ist unter http://127.0.0.1:8765 erreichbar (siehe unten).

Status anzeigen:

```bash
tradingbot status
```

Einen einzelnen Zyklus ausführen (z. B. zum Testen der Verbindung):

```bash
tradingbot run --once
```

## Dashboard

Beim Start mit `tradingbot run` öffnet der Bot ein Dashboard unter **http://127.0.0.1:8765**.
Es zeigt Kapital, Rendite, Tagesverlauf, Kapitalverlauf als Diagramm, offene Positionen mit
aktuellem Gewinn/Verlust, die letzten Trades und die Kennzahlen. Der Modus (Papierhandel oder
**LIVE**) steht dauerhaft im Kopf der Seite und im Tab-Titel. Die Seite aktualisiert sich alle 5 Sekunden
und folgt dem Hell/Dunkel-Modus deines Systems.

Der Button **Einstiege pausieren** stoppt nur neue Käufe. Offene Positionen laufen weiter mit
Stop-Loss, Take-Profit und Trailing-Stop. Die Pause wird gespeichert und gilt nach einem Neustart weiter.

Sicherheit:

- Der Server bindet ausschließlich an `127.0.0.1`. Eine andere Adresse wird abgelehnt, das Dashboard ist aus dem
  Netzwerk nicht erreichbar.
- Anfragen mit fremdem Host- oder Origin-Header werden abgelehnt (Schutz gegen Zugriffe von fremden Webseiten
  aus deinem Browser). Die Seite lädt keine externen Skripte oder Schriften.
- Das Dashboard kann nur pausieren und fortsetzen. Es kann keine Einstellungen ändern, keine Orders auslösen
  und zeigt keine API-Keys.

Optionen:

```bash
tradingbot run --port 9000          # anderer Port
tradingbot run --no-dashboard       # ohne Dashboard
tradingbot dashboard                # nur Ansicht des gespeicherten Stands, wenn der Bot in einem anderen Fenster läuft
```

Dauerhaft ändern lässt sich das im Abschnitt `dashboard` der `config.yaml`. Mit Docker ist das Dashboard
nicht erreichbar, weil es nur an den Loopback-Port im Container bindet. Für die lokale Nutzung auf dem PC
den Bot direkt starten (nicht per Docker).

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

## Beste Strategie finden

```bash
tradingbot compare --symbols BTC/USDT,ETH/USDT,SOL/USDT --since 2021-01-01
```

Der Befehl lädt die Kerzen (und merkt sie sich im Ordner `data/`), testet jede Strategie mit allen Parameter-
Kombinationen und schreibt einen Bericht nach `reports/`. Dauer: je nach Datenmenge ein bis wenige Minuten.

So wird Überanpassung vermieden:

1. Die Daten werden geteilt, standardmäßig 60 % Auswahlzeitraum und 40 % Prüfzeitraum.
2. Die besten Parameter jeder Strategie werden **nur** im Auswahlzeitraum bestimmt, über alle Coins gemeinsam.
3. Bewertet und gerankt wird **nur** im Prüfzeitraum, den die Auswahl nie gesehen hat.
4. Eine Strategie gilt nur als robust, wenn sie in beiden Zeiträumen Gewinn macht, im Prüfzeitraum mindestens
   30 Trades und einen Profitfaktor ab 1,2 hat und bei mindestens 60 % der Coins im Plus liegt.
   Andernfalls sagt der Bericht ausdrücklich, dass keine Strategie überzeugt.

Ein automatischer Test (`tests/`) stellt sicher, dass Kerzen nach dem Split die Auswahl nicht beeinflussen können.
Der Bericht zeigt zusätzlich Buy & Hold als Vergleich, die Ergebnisse je Coin und einen fertigen Eintrag für die
`config.yaml`. Alle getesteten Varianten stehen als CSV daneben, damit du sehen kannst, wie stark Auswahl- und
Prüfzeitraum auseinanderliegen.

Optionen: `--timeframe 4h`, `--until 2025-01-01`, `--strategies donchian,macd_trend`, `--split 0.7`.
Zum Ausprobieren ohne Börse: `tradingbot compare --demo trend` mit künstlichen Daten. Die Ergebnisse
daraus sagen nichts über echte Märkte.

Grenzen: Ein Backtest kennt keine Orderbuchtiefe, keine Latenz und keine Handelsausfälle. Ein einzelner
Prüfzeitraum ist keine Garantie. Teste einen Gewinner anschließend mehrere Wochen im Papierhandel.

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

Neue Klasse in `src/tradingbot/strategy/` von `Strategy` ableiten und `warmup_bars`, `_add_indicators(df)` und
`signals(df)` (Kauf- und Verkaufs-Bool-Arrays) implementieren. Über `param_grid` legst du fest, welche Werte der
Vergleich durchprobiert. Dann in `strategy/__init__.py` registrieren. Backtester, Vergleich und Bot nutzen dieselben Signale.

## Tests

```bash
pytest
```

## Projektstruktur

```
src/tradingbot/
├── cli.py            Kommandozeile: run, backtest, status
├── bot.py            Handelsschleife, Positionsverwaltung, Tagesverlustlimit
├── backtest.py       Backtester mit Kennzahlen, auch für Teilzeiträume
├── research.py       Strategievergleich mit Auswahl- und Prüfzeitraum
├── synthetic.py      Künstliche Kursreihen zum Testen des Werkzeugs
├── dashboard.py      Lokaler Webserver (nur Loopback) für das Dashboard
├── dashboard_page.py HTML, CSS und JavaScript des Dashboards
├── snapshot.py       Datenstand für das Dashboard (Kennzahlen, Positionen, Verlauf)
├── config.py         config.yaml + .env (pydantic)
├── data.py           Marktdaten über ccxt, CSV-Import
├── exchange.py       PaperBroker (Simulation) und LiveBroker (ccxt)
├── indicators.py     EMA, RSI, ATR
├── risk.py           Positionsgröße, Limits
├── state.py          Persistenter Zustand
├── notify.py         Log- und Telegram-Benachrichtigungen
└── strategy/         Strategie-Schnittstelle und die fünf Strategien
```

## Lizenz

MIT
