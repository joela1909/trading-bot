#!/usr/bin/env bash
# Oeffnet das Dashboard des Bots auf dem Server ueber einen verschluesselten SSH-Tunnel.
cd "$(dirname "$0")"
SERVER="${1:-$(cat server.txt 2>/dev/null || true)}"
if [ -z "$SERVER" ]; then read -r -p "Server-Adresse, z. B. root@203.0.113.10: " SERVER; fi
[ -z "$SERVER" ] && exit 1
echo "$SERVER" > server.txt
echo "Dashboard: http://127.0.0.1:8766  (Beenden mit Strg+C)"
( sleep 4; (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8766) || open http://127.0.0.1:8766 ) >/dev/null 2>&1 &
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 8766:127.0.0.1:8765 "$SERVER"
