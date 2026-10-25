#!/usr/bin/env bash
# Macht das Dashboard vom Handy aus erreichbar: HTTPS-Adresse mit Benutzername und Passwort (Caddy).
# Der Bot selbst bleibt nur intern erreichbar, Caddy leitet nach erfolgreicher Anmeldung weiter.
#   bash deploy/public-dashboard.sh                     Adresse aus der IP (z. B. 203-0-113-10.sslip.io)
#   DOMAIN=bot.meine-domain.de bash deploy/public-dashboard.sh   eigene Domain (DNS-A-Eintrag auf den Server)
#   bash deploy/public-dashboard.sh --off               wieder abschalten
set -euo pipefail
cd "$(dirname "$0")/.."
CADDY_IMAGE="caddy:2.8.4"

set_env() {  # set_env NAME WERT  -> Zeile in .env setzen oder ersetzen
  touch .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

if [ "${1:-}" = "--off" ]; then
  set_env COMPOSE_PROFILES ""
  set_env DASHBOARD_HOSTS ""
  docker compose --profile public stop caddy >/dev/null 2>&1 || true
  docker compose --profile public rm -f caddy >/dev/null 2>&1 || true
  docker compose up -d
  echo "Oeffentliches Dashboard abgeschaltet."
  exit 0
fi

if [ -z "${DOMAIN:-}" ]; then
  IP="$(curl -fsS4 https://api.ipify.org || curl -fsS4 https://ifconfig.me || true)"
  if [ -z "$IP" ]; then echo "Oeffentliche IP nicht gefunden. Bitte DOMAIN=... angeben."; exit 1; fi
  DOMAIN="$(echo "$IP" | tr '.' '-').sslip.io"
fi
USER_NAME="${DASH_USER:-bot}"
if [ -z "${DASH_PASSWORD:-}" ]; then
  DASH_PASSWORD="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)"
fi

echo "== Adresse:  https://$DOMAIN"
mkdir -p caddy/data caddy/config
HASH="$(docker run --rm "$CADDY_IMAGE" caddy hash-password --plaintext "$DASH_PASSWORD")"
cat > caddy/Caddyfile <<EOF
# Erzeugt von deploy/public-dashboard.sh. Neu erzeugen statt von Hand aendern.
$DOMAIN {
	basic_auth {
		$USER_NAME $HASH
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
	reverse_proxy 127.0.0.1:8765
}
EOF
chmod 600 caddy/Caddyfile
docker run --rm -v "$PWD/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" caddy validate --config /etc/caddy/Caddyfile >/dev/null

# Manche Anbieter (z. B. Oracle Cloud) sperren in Ubuntu per iptables alles ausser SSH. 80 und 443 vor der Sperre freigeben.
if command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | grep -q -- "-j REJECT"; then
  line="$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" {print $1; exit}')"
  for port in 80 443; do
    iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT "$line" -p tcp -m state --state NEW --dport "$port" -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then netfilter-persistent save >/dev/null 2>&1 || true; fi
  echo "   Firewall des Servers: Ports 80 und 443 freigegeben."
fi

set_env COMPOSE_PROFILES public
set_env DASHBOARD_HOSTS "$DOMAIN"
chmod 600 .env
docker compose up -d --build

echo
echo "Fertig. Das HTTPS-Zertifikat wird beim ersten Aufruf geholt, das kann eine Minute dauern."
echo
echo "   Adresse:   https://$DOMAIN"
echo "   Benutzer:  $USER_NAME"
echo "   Passwort:  $DASH_PASSWORD"
echo
echo "Das Passwort wird nur jetzt angezeigt und nirgends im Klartext gespeichert. Speichere es z. B. im Passwort-Manager."
echo "Neues Passwort: Skript erneut ausfuehren. Abschalten: bash deploy/public-dashboard.sh --off"
echo "Die Ports 80 und 443 muessen offen sein (bei einer Firewall des Anbieters freigeben)."
