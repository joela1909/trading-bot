#!/usr/bin/env bash
# Richtet den Trading Bot auf einem frischen Linux-Server (Ubuntu 22.04/24.04 oder Debian 12) ein.
# Aufruf auf dem Server als root:
#   curl -fsSL https://raw.githubusercontent.com/joela1909/trading-bot/main/deploy/server-setup.sh | bash
# oder nach dem Klonen:  bash deploy/server-setup.sh
set -euo pipefail

REPO="${REPO:-https://github.com/joela1909/trading-bot.git}"
DIR="${DIR:-$HOME/trading-bot}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Bitte als root ausfuehren (oder mit sudo)."
  exit 1
fi

# Kleine Server (z. B. Oracle E2.1.Micro mit 1 GB RAM): 2 GB Auslagerungsdatei, damit Bauen und Bot nicht am Speicher scheitern
mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
if [ "${mem_kb:-0}" -lt 2000000 ] && ! swapon --show | grep -q .; then
  echo "== Wenig Arbeitsspeicher: lege 2 GB Auslagerungsdatei an"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 1/4 Docker und Git installieren"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq git ca-certificates curl
  if ! apt-get install -y -qq docker.io docker-compose-v2 2>/dev/null; then
    # Fallback: offizielles Installationsskript von Docker
    curl -fsSL https://get.docker.com | sh
  fi
fi
command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git; }
systemctl enable --now docker >/dev/null 2>&1 || true

echo "== 2/4 Code holen nach $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
elif [ -f "$DIR/docker-compose.yml" ]; then
  echo "   Code ist schon da (vom PC hochgeladen), kein Download noetig."
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

echo "== 3/4 Konfiguration"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "   .env aus .env.example angelegt (Papierhandel, keine Schluessel noetig)."
fi
chmod 600 .env
mkdir -p state logs data reports

echo "== 4/4 Bot bauen und starten"
docker compose up -d --build

# Handy-Zugang: bei PUBLIC_DASHBOARD=1 ohne Nachfrage, sonst fragen (auch bei curl | bash ueber /dev/tty)
answer="${PUBLIC_DASHBOARD:-}"
if [ -z "$answer" ] && [ -r /dev/tty ]; then
  read -r -p "Dashboard vom Handy aus erreichbar machen (HTTPS mit Passwort)? [J/n] " answer < /dev/tty || answer="n"
fi
case "${answer:-j}" in
  [JjYy1]*) bash deploy/public-dashboard.sh ;;
  *) echo "   Uebersprungen. Spaeter einschalten mit: bash $DIR/deploy/public-dashboard.sh" ;;
esac

sleep 20
echo
docker compose ps
echo
docker compose logs --tail 15 bot
echo
echo "Fertig. Der Bot laeuft jetzt dauerhaft und startet nach einem Server-Neustart von selbst."
echo "  Logs ansehen:     cd $DIR && docker compose logs -f"
echo "  Aktualisieren:    bash $DIR/deploy/update.sh"
echo "  Stoppen:          cd $DIR && docker compose down"
echo "  Dashboard:        Handy/Browser ueber die oben angezeigte https-Adresse,"
echo "                    oder ohne Passwort-Zugang per SSH-Tunnel: dashboard-server.bat auf deinem PC"
