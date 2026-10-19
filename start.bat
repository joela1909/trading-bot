@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\tradingbot.exe" (
  echo Der Bot ist noch nicht installiert. Bitte zuerst install.bat ausfuehren.
  pause
  exit /b 1
)
echo Der Bot startet im Modus laut config.yaml und .env. Standard ist Papierhandel ohne echtes Geld.
echo Dashboard: http://127.0.0.1:8765
echo Beenden mit Strg+C. Der PC darf dabei nicht in den Ruhemodus gehen.
echo.
".venv\Scripts\tradingbot.exe" run --open
echo.
pause
