#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/tradingbot ]; then echo "Bitte zuerst ./install.sh ausfuehren." >&2; exit 1; fi
echo "Dashboard: http://127.0.0.1:8765  (Beenden mit Strg+C)"
exec .venv/bin/tradingbot run --open
