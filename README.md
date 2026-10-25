# Trading Bot

Vollautomatischer Krypto-Trading-Bot auf Basis von [ccxt](https://github.com/ccxt/ccxt), für Spot (nur Long)
oder USDT-Futures (**Long und Short**), spezialisiert auf **Tageshandel (Daytrading)**: Einstiege nur in einem
festen Zeitfenster, alle Positionen werden vor Feierabend geschlossen, nichts läuft über Nacht. Der Bot verwaltet Stop-Loss, Take-Profit und Trailing-Stop selbst
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
- **Tageshandel** (Standard, Abschnitt `daytrading` in der Konfiguration): Einstiegsfenster (z. B. 08:00–21:30),
  Zwangsschließung aller Positionen ab `flat_time` (z. B. 22:00), max. Einstiege pro Tag, optionale max. Haltedauer.
  Uhrzeiten in deiner Zeitzone inkl. Sommerzeit. Gilt auch für den KI-Bot. Backtest und Vergleich nutzen dieselben Regeln.
- **Short-Positionen** über USDT-Perpetual-Futures (`market: futures`, `strategy.allow_short: true`), Hebel einstellbar,
  Standard 1. Alle Strategien haben gespiegelte Short-Signale. Futures laufen bisher nur im Papierhandel.
- **Alle Strategien gleichzeitig** (`ensemble`, Standard): Jede Strategie gibt bei jeder Kerze ihre Stimme ab,
  ein Schiedsrichter steigt ein, wenn genug Strategien dieselbe Richtung sagen (`min_votes`). Backtestbar wie jede
  Strategie. Später soll Claude die Rolle des Schiedsrichters übernehmen können.
  Mit `solo_member` reicht das Signal einer ausgewählten Strategie allein, die anderen Stimmen können es nicht aufhalten.
- **Sieben Strategien**, alle mit ATR-basiertem Stop-Loss, Take-Profit und Trailing-Stop.
  Für den Tageshandel: `vwap_reversion` (Kauf deutlich unter dem Tages-VWAP, Verkauf am VWAP) und
  `opening_range` (Ausbruch aus der Eröffnungsspanne nach dem LuxAlgo-Indikator „Opening Range with Breakouts & Targets“,
  diese Datei steht unter CC BY-NC-SA 4.0 statt MIT). Außerdem `ema_cross` (EMA-Kreuz mit RSI-Filter), `donchian` (Ausbruch), `macd_trend` (MACD-Kreuz),
  `rsi_reversion` und `bollinger_reversion` (Rückkehr zum Mittelwert). Gewählt wird in `config.yaml` unter `strategy.name`.
- **KI-Bot** (`claude_advisor`): Claude entscheidet bei jeder neuen Kerze über Einstieg und Ausstieg. Positionsgröße,
  Stop-Loss und alle Risikogrenzen bleiben fest im Bot. Läuft getestet auf Bitget, siehe Abschnitt „KI-Bot für Bitget“.
- **Schutzmechanismen:** Abkühlphase nach Ausstiegen, Sperre nach Stop-Loss-Serien und eine Drawdown-Grenze,
  im Bot und im Backtest identisch.
- **Stop-Loss bei der Börse** (optional): Der Stop liegt als Order bei der Börse und schützt auch, wenn der Bot oder der PC ausfällt.
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

## Schnellstart per Doppelklick

Voraussetzung ist Python 3.11 oder neuer. Bei der Installation von Python unter Windows das Häkchen
„Add Python to PATH“ setzen.

| Schritt | Windows | Mac / Linux |
|---|---|---|
| Einmal installieren | `install.bat` | `./install.sh` |
| Beste Strategie finden | `vergleich.bat` | `./vergleich.sh` |
| Bot starten, Papierhandel | `start.bat` | `./start.sh` |
| Bot im Dauerbetrieb (24/7, Neustart nach Absturz) | `start-24-7.bat` | `./start-24-7.sh` |
| KI-Bot für Bitget starten | `start-ki.bat` | `./start-ki.sh` |

`start.bat` öffnet das Dashboard automatisch im Browser. Beenden mit Strg+C.

**Dauerbetrieb:** `start-24-7.bat` startet den Bot nach einem Absturz (z. B. Netzwerkfehler beim Start) nach 30 Sekunden
neu und schreibt das nach `logs/neustarts.log`. Unter Windows verhindert der Bot den Ruhemodus, solange er läuft
(`keep_awake: true`, ändert keine Energieeinstellung). Ein zugeklappter Laptop, ein Neustart von Windows oder ein
abgeschalteter PC stoppen ihn trotzdem. Für echten 24/7-Betrieb ohne eigenen PC eignet sich ein kleiner Server mit Docker
(siehe unten). Die Windows-Dateien sind nicht auf einem echten Windows-System getestet, die Mac/Linux-Skripte schon.

## Installation von Hand

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

## Die App

Beim Start öffnet der Bot seine App unter **http://127.0.0.1:8765** (auf dem Server bzw. Cloudflare unter der jeweiligen
https-Adresse mit Passwort). Sie folgt dem Hell/Dunkel-Modus des Systems und hat fünf Seiten:

| Seite | Inhalt |
|---|---|
| **Übersicht** | Kapital, heute, gesamt, realisiert. Je Paar die **Stimmen-Waage**: links die Short-, rechts die Long-Stimmen der Strategien, die Markierung zeigt, ab wie vielen Stimmen der Bot einsteigt. Offene Positionen mit Größe, Stop, Ziel und Risiko. Kapitalverlauf, Pause-Schalter |
| **Chart** | Interaktiver Kerzenchart ([TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/), lokal eingebunden): zoomen, verschieben, Fadenkreuz mit Kurs und Linienwerten. Linien der Strategien ein-/ausblenden, Trades als Pfeile auf Ein- und Ausstiegskurs, offene Position mit Einstieg, Stop und Ziel an der Preisachse. Darunter, warum der Bot gerade (nicht) einsteigt |
| **Trades** | Alle abgeschlossenen Trades mit Größe, Kursen, Ergebnis und Grund, filterbar nach Paar |
| **Strategien** | Je Paar die Stimme jeder Strategie (Long, Short, keine) und ob sie aussteigen würde |
| **Einstellungen** | Paare, Zeitrahmen, Strategie bzw. Ensemble (Stimmen, Mitglieder), Shorts, Risiko, Handelszeiten |

**Einstellungen in der App:** „Speichern und neu starten“ prüft alle Werte wie beim Start, speichert sie in
`state/settings_override.yaml` (hat Vorrang vor `config.yaml`) und startet den Bot neu. Das klappt automatisch mit
`start-24-7.bat`, Docker und Cloudflare. Mit `start.bat` musst du ihn danach selbst wieder starten.
Modus (Papier/Live), Börse, Markt, Hebel und Schlüssel lassen sich in der App bewusst **nicht** ändern.
„Auf config.yaml zurücksetzen“ löscht die App-Fassung.

**Aufs Handy:** Die App ist eine PWA. Im Handy-Browser über „Zum Startbildschirm hinzufügen“ installieren, sie öffnet
sich dann ohne Browserleiste.

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

## KI-Bot für Bitget

Hier entscheidet Claude, ob der Bot kauft, verkauft oder wartet. Der Bot schickt der KI bei jeder neuen Kerze die
letzten Kerzen, die Indikatoren, die Position und das Kapital. Die KI antwortet mit Kaufen, Verkaufen oder Halten,
einer Sicherheit zwischen 0 und 1 und einer kurzen Begründung. Alles Weitere bleibt im Bot:

- **Positionsgröße und Stop-Loss** berechnet der Bot nach festen Regeln, nicht die KI.
- **Schutzregeln** gelten unverändert: Tagesverlustlimit, maximale Positionen, Abkühlphase, Drawdown-Grenze, Pause-Schalter.
- **Fehler führen zu Halten:** Bei Netzwerkfehler, Zeitüberschreitung, Ablehnung, abgeschnittener oder unlesbarer Antwort kauft
  der Bot nichts. Offene Positionen bleiben durch ihre Stops geschützt.
- **Mindest-Sicherheit:** Käufe unter 0,6 und Verkäufe unter 0,5 werden verworfen. Beides ist einstellbar.
- **Kostendeckel:** Höchstens 200 KI-Aufrufe pro Tag. Gefragt wird nur bei einer neuen abgeschlossenen Kerze, nie bei jedem Durchlauf.
  Vorher werden alle kostenlosen Prüfungen erledigt, also Pause, Tageslimit, Abkühlphase und maximale Positionen.
- **Nachvollziehbar:** Das Dashboard zeigt jede Entscheidung mit Zeit, Sicherheit, ob sie umgesetzt wurde, und der Begründung.

### Einrichten

1. Auf https://console.anthropic.com einen API-Schlüssel anlegen. Er ist getrennt von einem Claude-Abo und wird nach Verbrauch abgerechnet.
2. In der `.env` eintragen: `ANTHROPIC_API_KEY=...`
3. Starten, zuerst immer im Papierhandel:
   ```
   tradingbot --config config.bitget-ki.yaml run
   ```
   Per Doppelklick geht es mit `start-ki.bat` bzw. `./start-ki.sh`.

Der Papierhandel braucht für Bitget keinen Börsen-Schlüssel, nur den Anthropic-Schlüssel.

### Kosten

Standardmodell ist `claude-opus-5-5`. Eine Entscheidung braucht etwa 2000 bis 3000 Eingabe-Tokens plus die Denk- und Antwort-Tokens.
Bei zwei Paaren und Stundenkerzen sind das 48 Aufrufe am Tag. Das ergibt grob wenige Dollar pro Tag, je nach Aufwand
der KI. Günstiger sind `claude-sonnet-5-5` und `claude-haiku-5-5`, einstellbar unter `ai.model` in der Konfiguration.
`ai.effort` steuert, wie gründlich die KI nachdenkt. Beobachte die Kosten in der Anthropic-Konsole und senke bei Bedarf `max_calls_per_day`.

### Live-Handel auf Bitget

1. Bei Bitget einen API-Schlüssel anlegen. Rechte: Lesen und Spot-Handel, **keine** Auszahlung. Wenn möglich auf deine IP beschränken.
   Bitget verlangt dabei eine **Passphrase**, die du selbst festlegst.
2. In `.env`: `TRADING_MODE=live`, `LIVE_TRADING_CONFIRMED=true`, `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET` und `EXCHANGE_API_PASSWORD` (die Passphrase).
3. Zuerst `tradingbot --config config.bitget-ki.yaml run --once` und prüfen, dass Guthaben und Bestände stimmen.
4. Mit einem sehr kleinen Betrag beginnen.

Für Bitget sind Besonderheiten eingebaut: Market-Käufe geben den Preis mit, weil Bitget ihn zur Kostenberechnung verlangt. Gebühren in
der gekauften Währung verringern die Positionsgröße, und beim Verkauf wird nie mehr als der freie Bestand angeboten.
Die Live-Anbindung ist gegen eine simulierte Börse getestet, nicht gegen die echte Bitget-Schnittstelle.

### Ehrlich zur KI

Es gibt keinen Beleg, dass ein Sprachmodell am Markt dauerhaft Gewinn macht. Die Antworten sind nicht reproduzierbar.
Ein Backtest ist nicht möglich, weil das Modell die Vergangenheit teilweise aus dem Training kennt und alte Daten scheinbar perfekt handeln würde.
Bewerten kannst du die KI nur im Papierhandel über mehrere Wochen, am besten im Vergleich zur besten regelbasierten Strategie aus `tradingbot compare`.

## Stop-Loss bei der Börse

Standardmäßig überwacht nur der Bot die Stops und verkauft per Market-Order. Fällt der Bot oder der PC aus, schützt dich dann nichts.
Mit der folgenden Einstellung hinterlegt der Bot zu jeder Position eine Stop-Order bei der Börse:

```yaml
execution:
  stoploss_on_exchange: true
```

Der Bot zieht die Order beim Trailing-Stop nach, storniert sie vor jedem anderen Ausstieg und erkennt, wenn sie ausgelöst wurde.
Klappt das Hinterlegen nicht, meldet der Bot eine Warnung und der Stop im Bot gilt weiter. Das ist im Live-Betrieb empfohlen,
aber nur gegen eine simulierte Börse getestet. Probiere es zuerst mit einem sehr kleinen Betrag.

## Prüfung auf Blick in die Zukunft

```bash
tradingbot check --since 2023-01-01
```

Der Befehl prüft jede Strategie auf zwei Fehlerquellen, die Backtests unbemerkt verfälschen: Signale, die sich ändern, wenn spätere Kerzen
dazukommen, und Abweichungen zwischen dem Backtest und dem Live-Bot, der nur die letzten Kerzen sieht. `tradingbot compare` führt die Prüfung
automatisch aus und schließt auffällige Strategien aus.

## Schutzmechanismen

In der `config.yaml` unter `protections`. Sie gelten im Papier- und Live-Handel und im Backtest.

| Einstellung | Wirkung |
|---|---|
| `cooldown_candles` | Nach einem Ausstieg wird dasselbe Paar einige Kerzen lang nicht gekauft |
| `stoploss_guard_stops`, `_window_candles`, `_lock_candles` | Mehrere Stop-Losses in kurzer Zeit sperren alle Einstiege für eine Weile |
| `max_drawdown_pct`, `max_drawdown_lock_candles` | Fällt das Kapital zu weit unter sein Hoch, sperrt der Bot alle Einstiege |

Im Vergleich lässt sich das mit `--no-protections` abschalten. Außerdem wählt `--metric calmar|sharpe|sortino|profit` das Maß für die Parameterwahl.

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

## Short-Positionen (Futures)

Im Spot-Handel kann man nur kaufen, was man hat. Für Shorts handelt der Bot USDT-Perpetual-Futures:

```yaml
market: futures            # spot | futures
symbols: [BTC/USDT, ETH/USDT, SOL/USDT]   # wird automatisch zu BTC/USDT:USDT usw.
futures:
  leverage: 1              # 1 = kein zusätzliches Risiko durch Hebel
  margin_mode: isolated
strategy:
  allow_short: true
paper:
  fee_pct: 0.05            # Futures-Gebühren sind niedriger als Spot
```

- **Signale:** Jede Strategie hat gespiegelte Short-Regeln, z. B. `vwap_reversion` shortet, wenn der Kurs mehrere ATR
  **über** dem Tages-VWAP liegt, und schließt am VWAP. Trendfilter: Long nur über, Short nur unter `trend_ema`.
- **Stop-Loss** liegt bei Short über dem Einstieg, Take-Profit darunter. Der Trailing-Stop zieht bei fallenden Kursen nach unten nach.
- **Konto:** Eine Position bindet Nennwert / Hebel als Sicherheit. Gebühren fallen auf den Nennwert an.
- **Grenzen:** Funding-Gebühren und Liquidation werden im Papierhandel und Backtest nicht simuliert. Bei Hebel 1
  liegt die Liquidation weit hinter dem Stop. **Live-Handel mit Futures ist gesperrt**, bis die Anbindung gegen
  eine echte Börse getestet ist. Der KI-Bot (`claude_advisor`) kann noch nicht shorten.
- Futures haben einen eigenen Zustand (`state/state_paper_futures.json`), Spot-Positionen werden nicht vermischt.
- Prüfe vorher, ob dein Wohnsitzland Krypto-Derivate bei deiner Börse überhaupt erlaubt.

## Tageshandel

In `config.yaml` handelt der Bot rund um die Uhr auf 1-Minuten-Kerzen (`entry_start: "00:00"`, `entry_end: "23:50"`,
`flat_time: "24:00"`): Einstiege den ganzen Tag, um Mitternacht wird alles geschlossen und der Handelstag beginnt neu.
Der KI-Bot handelt nur tagsüber auf 15-Minuten-Kerzen, etwa so:

```yaml
timeframe: 1m
daytrading:
  enabled: true
  timezone: Europe/Berlin
  entry_start: "08:00"       # ab hier neue Einstiege, Beginn der Eröffnungsspanne
  entry_end: "21:30"         # danach keine neuen Einstiege
  flat_time: "22:00"         # ab hier werden alle offenen Positionen verkauft
  max_trades_per_day: 6      # über alle Coins, 0 = unbegrenzt
  max_hold_minutes: 0        # 0 = nur durch flat_time begrenzt
```

- Eine Position, die noch vom Vortag stammt (z. B. weil der Rechner zur `flat_time` aus war), wird beim nächsten
  Durchlauf sofort geschlossen.
- Der Handelstag für das Tagesverlustlimit beginnt um Mitternacht in der eingestellten Zeitzone.
- Krypto wird rund um die Uhr gehandelt. Das Zeitfenster ist deshalb deine Wahl, nicht die der Börse.
- Die Zeitprüfung kommt vor der Strategie: Außerhalb des Fensters fragt der KI-Bot Claude nicht nach Einstiegen,
  das spart Aufrufe. Bei 15-Minuten-Kerzen und zwei Paaren sind es höchstens rund 110 Aufrufe am Tag statt 48 bei 1h.
- `enabled: false` schaltet zurück auf den Modus mit Positionen über Nacht (dann z. B. `timeframe: 1h`).
- Mit `--no-daytrading` testen `backtest`, `check` und `compare` ohne diese Regeln.

Strategievergleich für den Tageshandel:

```bash
tradingbot compare --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframe 15m --since 2023-01-01
```

**Gebühren sind beim Tageshandel der größte Gegner.** Bei 0,1 % pro Order kostet jeder Trade hin und zurück
rund 0,3 % inklusive Kursabweichung. Auf 15-Minuten-Kerzen ist ein typischer Stop nur wenige Zehntel Prozent
entfernt, die Kosten fressen also einen großen Teil jedes Gewinns. Trage deine echten Gebühren in `paper.fee_pct` ein,
damit Backtest und Papierhandel realistisch rechnen.

## Konfiguration

Alle Parameter stehen kommentiert in [`config.yaml`](config.yaml). Die wichtigsten:

| Bereich | Parameter | Bedeutung |
|---|---|---|
| allgemein | `exchange`, `symbols`, `timeframe` | Börse (ccxt-ID), Handelspaare, Kerzen-Zeitrahmen |
| `strategy` | `fast_ema`, `slow_ema`, `rsi_max_entry` | Trendfilter und Überkauft-Schwelle |
| `strategy` | `stop_atr_mult`, `take_profit_atr_mult`, `trailing_stop` | Stop-Loss, Take-Profit, Trailing |
| `strategy` | `trailing_pct` | Trailing-Stop mit festem Abstand in % zum besten Kurs, wandert nur in Richtung der Position (0 = wie der Stop) |
| `strategy` | `solo_member` | Ensemble: Signal dieser Strategie reicht allein |
| `risk` | `risk_per_trade_pct` | Max. Verlust pro Trade in Prozent des Kapitals |
| `risk` | `max_position_pct`, `max_open_positions` | Begrenzung der Positionsgröße und -anzahl |
| `risk` | `max_daily_loss_pct` | Ab diesem Tagesverlust keine neuen Einstiege bis zum nächsten Tag |
| `daytrading` | `entry_start`, `entry_end`, `flat_time`, `timezone` | Einstiegsfenster und Zwangsschließung (siehe Tageshandel) |
| `daytrading` | `max_trades_per_day`, `max_hold_minutes` | Max. Einstiege pro Tag, max. Haltedauer |
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

## Bot auf einem Server (läuft ohne deinen PC)

> **Alternative ohne eigenen Server:** komplett auf Cloudflare (Container + Worker mit Passwort), siehe [cloudflare/README.md](cloudflare/README.md).

Damit der Bot rund um die Uhr läuft, auch wenn dein PC aus ist, braucht er einen Server, der immer an ist.
Ein kleiner Linux-Server (1–2 GB RAM genügen) kostet bei Anbietern wie Hetzner, Netcup oder Contabo einige Euro im Monat.

**1. Server mieten:** Ubuntu 24.04 oder 22.04 wählen (Debian 12 geht auch), **Standort in der EU**, z. B. Deutschland
oder Finnland. Binance sperrt Anfragen aus manchen Ländern, z. B. den USA. Beim Anlegen deinen SSH-Schlüssel
hinterlegen (Windows: `ssh-keygen` im Terminal, dann den Inhalt von `%USERPROFILE%\.ssh\id_ed25519.pub` einfügen).

**2. Einrichten**, als root auf dem Server (`ssh root@DEINE-IP`):

```bash
curl -fsSL https://raw.githubusercontent.com/joela1909/trading-bot/main/deploy/server-setup.sh | bash
```

Das Skript installiert Docker und Git, holt den Code nach `~/trading-bot`, legt eine `.env` für den Papierhandel an
und startet den Bot. Er startet nach Abstürzen und nach einem Neustart des Servers von selbst wieder.
Am Ende fragt es, ob das Dashboard vom Handy aus erreichbar sein soll.

**3. Dashboard vom Handy:** Mit „J“ (oder später `bash deploy/public-dashboard.sh`) richtet das Skript
[Caddy](https://caddyserver.com) ein. Du bekommst eine Adresse wie `https://203-0-113-10.sslip.io`, einen Benutzer und
ein zufälliges Passwort, das nur einmal angezeigt wird. Die Adresse im Handy-Browser öffnen, anmelden, fertig.

- Caddy holt das HTTPS-Zertifikat selbst und fragt vor jeder Seite nach Benutzer und Passwort. Der Bot selbst bleibt
  nur intern erreichbar und nimmt nur Anfragen für genau diese Adresse an.
- Eigene Domain statt sslip.io: DNS-A-Eintrag auf die Server-IP, dann `DOMAIN=bot.deine-domain.de bash deploy/public-dashboard.sh`.
- Neues Passwort: Skript erneut ausführen. Abschalten: `bash deploy/public-dashboard.sh --off`.
- Die Ports 80 und 443 müssen offen sein. Hat dein Anbieter eine Firewall, dort freigeben.
- Wer das Passwort kennt, kann das Dashboard sehen und neue Einstiege pausieren, aber nicht handeln oder Geld bewegen.

**Ohne Passwort-Zugang:** auf dem PC `dashboard-server.bat` (Mac/Linux: `./dashboard-server.sh`) starten und
`root@DEINE-IP` eingeben. Das baut einen verschlüsselten SSH-Tunnel auf und öffnet http://127.0.0.1:8766.

Weitere Befehle auf dem Server:

```bash
cd ~/trading-bot
docker compose logs -f          # Logs live ansehen
bash deploy/update.sh           # neuesten Stand aus GitHub holen und neu starten
docker compose down             # Bot stoppen
nano config.yaml                # Einstellungen ändern, danach: docker compose restart
```

- `state/`, `logs/`, `data/` und `reports/` liegen auf dem Server und überstehen Updates. `config.yaml` wird nur gelesen.
- Bisherigen Papierhandel mitnehmen (optional, Bot auf dem PC vorher beenden):
  `scp -r state root@DEINE-IP:~/trading-bot/` und danach auf dem Server `docker compose restart`.
- **Nicht gleichzeitig auf PC und Server laufen lassen.** Im Papierhandel ist das nur verwirrend, im Live-Handel
  würde doppelt gehandelt.
- Live-Handel auf dem Server: Schlüssel in `~/trading-bot/.env` eintragen (`nano .env`, Rechte nur Lesen und Handel,
  keine Auszahlung, wenn möglich auf die IP des Servers beschränken), dann `docker compose restart`.

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
├── session.py        Handelszeiten für den Tageshandel (Bot und Backtest)
├── backtest.py       Backtester mit Kennzahlen, auch für Teilzeiträume
├── research.py       Strategievergleich mit Auswahl- und Prüfzeitraum
├── analysis.py       Prüfung auf Blick in die Zukunft und Live-Abweichung
├── ai.py             Anbindung an Claude für den KI-Bot
├── protections.py    Abkühlphase, Stop-Loss-Serie, Drawdown-Grenze
├── synthetic.py      Künstliche Kursreihen zum Testen des Werkzeugs
├── dashboard.py      Webserver der App (Loopback, Host-/Origin-Prüfung, API)
├── web/              Die App: index.html, app.js, app.css, PWA, Chart-Bibliothek, Schriften
├── app_settings.py   Einstellungen aus der App: Prüfung, Speichern, Zurücksetzen
├── remote_state.py   Zustand zusätzlich extern sichern (Cloudflare R2)
├── snapshot.py       Datenstand für das Dashboard (Kennzahlen, Positionen, Verlauf)
├── config.py         config.yaml + .env (pydantic)
├── data.py           Marktdaten über ccxt, CSV-Import
├── exchange.py       PaperBroker (Spot), PaperFuturesBroker (Long/Short) und LiveBroker (ccxt, Spot)
├── indicators.py     EMA, RSI, ATR, MACD, Tages-VWAP
├── risk.py           Positionsgröße, Limits
├── state.py          Persistenter Zustand
├── notify.py         Log- und Telegram-Benachrichtigungen
└── strategy/         Strategie-Schnittstelle, sieben Strategien, Ensemble und die KI-Strategie
```

## Lizenz

MIT
