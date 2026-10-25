@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\tradingbot.exe" (
  echo Der Bot ist noch nicht installiert. Bitte zuerst install.bat ausfuehren.
  pause
  exit /b 1
)
if not exist logs mkdir logs
echo Dauerbetrieb: Der Bot startet nach einem Absturz automatisch neu.
echo Modus laut config.yaml und .env, Standard ist Papierhandel ohne echtes Geld.
echo Dashboard: http://127.0.0.1:8765
echo Beenden: Strg+C, danach die Frage "Batchvorgang abbrechen" mit J beantworten.
echo.
set "OPEN=--open"
:loop
".venv\Scripts\tradingbot.exe" run %OPEN%
set "CODE=%ERRORLEVEL%"
set "OPEN="
if "%CODE%"=="0" goto end
if "%CODE%"=="3" (
  echo Neue Einstellungen gespeichert, der Bot startet neu ...
  goto loop
)
echo %date% %time% Bot mit Fehlercode %CODE% beendet, Neustart in 30 Sekunden>> logs\neustarts.log
echo.
echo Der Bot wurde mit Fehlercode %CODE% beendet. Neustart in 30 Sekunden, abbrechen mit Strg+C.
timeout /t 30 /nobreak >nul
goto loop
:end
echo.
echo Der Bot wurde regulaer beendet.
pause
