# Bringt den Trading Bot auf Cloudflare (Worker + Container in der EU + R2-Speicher).
# Voraussetzungen: Cloudflare-Konto mit Workers-Paid-Plan, Docker Desktop laeuft, Node.js installiert.
# Aufruf: Doppelklick auf deploy.bat oder  powershell -ExecutionPolicy Bypass -File deploy.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

Step "1/6 Docker pruefen"
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop laeuft nicht. Bitte starten und erneut ausfuehren." }

Step "2/6 Abhaengigkeiten installieren"
npm install --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "npm install fehlgeschlagen" }

Step "3/6 Bei Cloudflare anmelden"
npx wrangler whoami *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Es oeffnet sich der Browser. Bitte dort bei Cloudflare anmelden und den Zugriff fuer Wrangler erlauben."
  npx wrangler login
  if ($LASTEXITCODE -ne 0) { throw "Anmeldung bei Cloudflare fehlgeschlagen" }
}

Step "4/6 R2-Speicher fuer den Zustand anlegen"
$buckets = npx wrangler r2 bucket list 2>&1 | Out-String
if ($buckets -notmatch "trading-bot-state") {
  # Standort-Hinweis Westeuropa (eine EU-Jurisdiktion braeuchte zusaetzlich eine Einstellung im Binding)
  npx wrangler r2 bucket create trading-bot-state --location weur
  if ($LASTEXITCODE -ne 0) { throw "R2-Speicher konnte nicht angelegt werden (ist R2 im Konto aktiviert?)" }
} else { Write-Host "   schon vorhanden" }

Step "5/6 Worker und Container deployen (das erste Mal dauert einige Minuten)"
npx wrangler deploy
if ($LASTEXITCODE -ne 0) { throw "Deploy fehlgeschlagen" }

Step "6/6 Passwort fuer das Dashboard setzen"
$setPassword = $true
$existing = npx wrangler secret list 2>&1 | Out-String
if ($existing -match "DASHBOARD_PASSWORD") {
  $answer = Read-Host "Es gibt schon ein Passwort. Neues erzeugen? [j/N]"
  $setPassword = $answer -match "^[jJyY]"
}
if ($setPassword) {
  $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789".ToCharArray()
  $bytes = New-Object byte[] 24
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $password = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
  $file = Join-Path $env:TEMP ("tb-secrets-" + [guid]::NewGuid() + ".json")
  try {
    @{ DASHBOARD_USER = "bot"; DASHBOARD_PASSWORD = $password } | ConvertTo-Json | Set-Content -Path $file -Encoding utf8
    npx wrangler secret bulk $file
    if ($LASTEXITCODE -ne 0) { throw "Passwort konnte nicht gesetzt werden" }
  } finally {
    Remove-Item $file -Force -ErrorAction SilentlyContinue
  }
}

Write-Host ""
Write-Host "Fertig." -ForegroundColor Green
Write-Host "Die Adresse steht oben in der Ausgabe von 'wrangler deploy' (https://trading-bot.<dein-name>.workers.dev)."
if ($setPassword) {
  Write-Host ""
  Write-Host "   Benutzer:  bot"
  Write-Host "   Passwort:  $password" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "Das Passwort wird nur jetzt angezeigt. Speichere es z. B. im Passwort-Manager."
}
Write-Host "Der Bot braucht nach dem ersten Deploy einige Minuten, bis er antwortet."
Write-Host "Logs: npx wrangler tail   oder im Cloudflare-Dashboard unter Workers > trading-bot > Logs."
