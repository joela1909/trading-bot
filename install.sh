#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "Trading Bot - Installation"

PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 oder neuer wurde nicht gefunden. Bitte installieren: https://www.python.org/downloads/" >&2
  exit 1
fi

[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
[ -f .env ] || cp .env.example .env

echo
echo "Fertig."
echo "  ./vergleich.sh   beste Strategie finden"
echo "  ./start.sh       Bot im Papierhandel starten"
