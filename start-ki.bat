@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\tradingbot.exe" (
  echo Der Bot ist noch nicht installiert. Bitte zuerst install.bat ausfuehren.
  pause
  exit /b 1
)
echo KI-Bot fuer Bitget. Papierhandel ohne echtes Geld, solange .env nicht auf live steht.
echo Dashboard: http://127.0.0.1:8765
echo Beenden mit Strg+C. Der PC darf nicht in den Ruhemodus gehen.
echo.
".venv\Scripts\tradingbot.exe" --config config.bitget-ki.yaml run --open
echo.
pause
