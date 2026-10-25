#!/usr/bin/env bash
# Dauerbetrieb: startet den Bot nach einem Absturz automatisch neu. Beenden mit Strg+C.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/tradingbot ]; then
  echo "Der Bot ist noch nicht installiert. Bitte zuerst ./install.sh ausfuehren."
  exit 1
fi
mkdir -p logs
trap 'echo; echo "Beendet."; exit 0' INT TERM
OPEN="--open"
while true; do
  .venv/bin/tradingbot run $OPEN
  code=$?
  OPEN=""
  [ "$code" -eq 0 ] && break
  if [ "$code" -eq 3 ]; then echo "Neue Einstellungen gespeichert, der Bot startet neu ..."; continue; fi
  echo "$(date '+%F %T') Bot mit Fehlercode $code beendet, Neustart in 30 Sekunden" >> logs/neustarts.log
  echo "Der Bot wurde mit Fehlercode $code beendet. Neustart in 30 Sekunden, abbrechen mit Strg+C."
  sleep 30
done
