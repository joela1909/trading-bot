@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\tradingbot.exe" (
  echo Der Bot ist noch nicht installiert. Bitte zuerst install.bat ausfuehren.
  pause
  exit /b 1
)
set "SINCE="
set /p SINCE=Startdatum der Daten, Enter fuer 2021-01-01: 
if "%SINCE%"=="" set "SINCE=2021-01-01"
echo.
echo Lade Kurse und teste alle Strategien. Das dauert einige Minuten ...
".venv\Scripts\tradingbot.exe" compare --symbols BTC/USDT,ETH/USDT,SOL/USDT --since %SINCE%
echo.
if exist reports start "" explorer reports
pause
