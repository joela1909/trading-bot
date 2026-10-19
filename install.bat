@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  Trading Bot - Installation
echo ============================================
echo.

set "PY="
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY=python"
)
if not defined PY goto :nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto :oldpython

if not exist ".venv\Scripts\python.exe" (
  echo Erstelle Python-Umgebung ...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)

echo Installiere Bot und Abhaengigkeiten ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto :fail

if not exist ".env" copy ".env.example" ".env" >nul

echo.
echo ============================================
echo  Fertig.
echo  1. Doppelklick auf vergleich.bat: beste Strategie finden
echo  2. Doppelklick auf start.bat: Bot im Papierhandel starten
echo ============================================
pause
exit /b 0

:nopython
echo Python wurde nicht gefunden.
echo Installiere Python 3.11 oder neuer von https://www.python.org/downloads/
echo und setze dabei das Haekchen "Add Python to PATH". Starte danach diese Datei erneut.
pause
exit /b 1

:oldpython
echo Dein Python ist zu alt. Es wird Python 3.11 oder neuer benoetigt.
echo Download: https://www.python.org/downloads/
pause
exit /b 1

:fail
echo.
echo Die Installation ist fehlgeschlagen. Bitte kopiere die Meldung oben und schick sie mir.
pause
exit /b 1
