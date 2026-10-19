#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/tradingbot ]; then echo "Bitte zuerst ./install.sh ausfuehren." >&2; exit 1; fi
read -r -p "Startdatum der Daten, Enter fuer 2021-01-01: " SINCE
SINCE=${SINCE:-2021-01-01}
exec .venv/bin/tradingbot compare --symbols BTC/USDT,ETH/USDT,SOL/USDT --since "$SINCE"
