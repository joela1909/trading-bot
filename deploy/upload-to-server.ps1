# Bringt den Bot vom PC auf einen Linux-Server (z. B. Oracle Cloud) und richtet ihn dort ein. Kein GitHub noetig.
#
#   Erstes Mal (mit bisherigem Papierkonto, Trades und App-Einstellungen):
#     powershell -ExecutionPolicy Bypass -File deploy\upload-to-server.ps1 -Server ubuntu@203.0.113.10 -WithState
#   Spaeter, nur Code aktualisieren (Zustand auf dem Server bleibt):
#     powershell -ExecutionPolicy Bypass -File deploy\upload-to-server.ps1 -Server ubuntu@203.0.113.10
#
# Die .env (Schluessel) wird nie hochgeladen. Auf dem Server entsteht eine .env fuer den Papierhandel.
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$Key = "$env:USERPROFILE\.ssh\trading-bot-oracle",
  [switch]$WithState,
  [switch]$NoPublicDashboard
)
$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$remoteDir = "/opt/trading-bot"
# Windows-eigenes tar.exe verwenden: ein tar aus Git o. ae. haelt "C:" fuer einen Netzwerkrechner
$tar = Join-Path $env:SystemRoot "System32\tar.exe"
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30")

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

if (-not (Test-Path $Key)) { throw "SSH-Schluessel $Key nicht gefunden." }

Step "1/4 Verbindung zum Server pruefen"
ssh @ssh $Server "echo verbunden: `$(uname -m), `$(lsb_release -ds 2>/dev/null || cat /etc/os-release | head -1)"
if ($LASTEXITCODE -ne 0) { throw "Keine SSH-Verbindung zu $Server" }

Step "2/4 Bot packen"
$archive = Join-Path $env:TEMP "trading-bot-upload.tgz"
$exclude = @(".venv", "data", "logs", "reports", ".env", ".pytest_cache", "server.txt", "caddy",
             "cloudflare/node_modules", "cloudflare/.wrangler", "__pycache__", "*.egg-info", "*.zip")
if (-not $WithState) { $exclude += "state" }
$tarArgs = @("-czf", $archive)
foreach ($e in $exclude) { $tarArgs += "--exclude=$e" }
$tarArgs += @("-C", $project, ".")
& $tar @tarArgs
if ($LASTEXITCODE -ne 0) { throw "Packen fehlgeschlagen" }
"{0:N1} MB" -f ((Get-Item $archive).Length / 1MB)

Step "3/4 Hochladen"
scp @ssh $archive "${Server}:/tmp/trading-bot-upload.tgz"
if ($LASTEXITCODE -ne 0) { throw "Hochladen fehlgeschlagen" }
Remove-Item $archive -Force

Step "4/4 Auf dem Server einrichten (das erste Mal dauert einige Minuten)"
$public = if ($NoPublicDashboard) { "0" } else { "1" }
$remote = @"
set -e
sudo mkdir -p $remoteDir
sudo tar -xzf /tmp/trading-bot-upload.tgz -C $remoteDir
rm -f /tmp/trading-bot-upload.tgz
cd $remoteDir
if [ -f .env ] && grep -q '^COMPOSE_PROFILES=public' .env && [ "$public" = "1" ]; then
  echo '   Handy-Zugang ist schon eingerichtet, Bot wird nur neu gebaut.'
  sudo docker compose up -d --build
  sudo docker compose ps
else
  sudo env DIR=$remoteDir PUBLIC_DASHBOARD=$public bash deploy/server-setup.sh
fi
"@
ssh @ssh -t $Server $remote.Replace("`r", "")
if ($LASTEXITCODE -ne 0) { throw "Einrichtung auf dem Server fehlgeschlagen" }

Write-Host ""
Write-Host "Fertig. Der Bot laeuft auf $Server." -ForegroundColor Green
Write-Host "Wichtig: Den Bot auf dem PC jetzt beenden, damit nicht zwei Bots gleichzeitig laufen."
