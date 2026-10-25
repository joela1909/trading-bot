@echo off
setlocal
cd /d "%~dp0"
rem Oeffnet das Dashboard des Bots auf dem Server ueber einen verschluesselten SSH-Tunnel.
rem Die Server-Adresse wird in server.txt gemerkt (z. B. root@203.0.113.10).
set "SERVER="
if exist server.txt set /p SERVER=<server.txt
if "%SERVER%"=="" (
  set /p SERVER=Server-Adresse, z. B. root@203.0.113.10:
)
if "%SERVER%"=="" exit /b 1
> server.txt echo %SERVER%
echo.
echo Tunnel zu %SERVER% wird aufgebaut. Dashboard: http://127.0.0.1:8766
echo Dieses Fenster offen lassen. Beenden mit Strg+C.
echo.
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8766"
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 8766:127.0.0.1:8765 %SERVER%
echo.
echo Tunnel beendet.
pause
