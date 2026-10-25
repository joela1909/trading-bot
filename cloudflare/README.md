# Trading Bot auf Cloudflare

Der Bot läuft als [Cloudflare Container](https://developers.cloudflare.com/containers/) in der EU, davor ein Worker
mit Passwort. Das Dashboard ist dann unter `https://trading-bot.<dein-name>.workers.dev` erreichbar, auch vom Handy.

## Was du brauchst

- Cloudflare-Konto mit **Workers-Paid-Plan** (5 $ im Monat, Containers gibt es nur dort). Dazu kommt die Nutzung:
  Ein Bot rund um die Uhr auf `basic` (1/4 vCPU, 1 GiB) liegt grob bei weiteren 7–8 $ im Monat
  (Stand Preisliste 10/2026: Speicher 0,0000025 $/GiB-s nach 25 GiB-h inklusive, CPU nur nach Nutzung).
- Docker Desktop (läuft während des Deploys) und Node.js.

## Deploy

Doppelklick auf `deploy.bat`. Das Skript

1. installiert Wrangler, meldet dich im Browser bei Cloudflare an (einmalig),
2. legt den R2-Speicher `trading-bot-state` an (Westeuropa),
3. baut das Docker-Image aus dem Projektordner und deployt Worker und Container,
4. erzeugt ein zufälliges Passwort und zeigt es **einmal** an.

Nach dem ersten Deploy braucht der Container einige Minuten, bis er antwortet.

## Wie es funktioniert

| Teil | Aufgabe |
|---|---|
| Worker `src/index.ts` | prüft Benutzer und Passwort (max. 10 Fehlversuche je Minute und IP), leitet ans Dashboard im Container weiter |
| Container | derselbe Bot wie lokal, `config.yaml` ist ins Image eingebaut, Papierhandel |
| Cron jede Minute | hält den Bot wach und startet ihn neu, falls Cloudflare ihn beendet hat |
| R2 `trading-bot-state` | Positionen, Trades und Papierkonto. Die Festplatte des Containers geht bei jedem Neustart verloren, der Bot holt seinen Zustand dann von hier |

- Der Bot schickt seinen Zustand an `http://state.tradingbot/`. Diese Adresse gibt es nur im Container, der Worker fängt sie ab und schreibt in R2. Von außen ist der Speicher nicht erreichbar.
- Der Container läuft in der EU (`constraints.jurisdiction`), weil Binance Anfragen aus manchen Ländern ablehnt.
- Einstellungen ändern: `config.yaml` im Projekt anpassen und erneut `deploy.bat` ausführen.
- Logs: `npx wrangler tail` oder im Cloudflare-Dashboard unter Workers › trading-bot.
- Neues Passwort: `deploy.bat` erneut ausführen und die Frage mit „j“ beantworten.

## Grenzen

- Cloudflare garantiert keine Laufzeit. Nach einem Neustart durch Cloudflare startet der Cron den Bot innerhalb einer Minute neu, in der Zwischenzeit werden keine Stops geprüft.
- Live-Handel ist auf Cloudflare nicht vorgesehen: Im Container ist `TRADING_MODE=paper` fest eingestellt.
- Lokal testen lässt sich der Container unter Windows nicht (`wrangler dev` braucht dafür WSL). Geprüft sind Typecheck und `wrangler deploy --dry-run`.
