@echo off
rem Bot auf den Server hochladen. Aufruf: upload-to-server.bat ubuntu@IP [-WithState]
cd /d "%~dp0"
if "%~1"=="" (
  echo Aufruf: upload-to-server.bat ubuntu@IP-DES-SERVERS [-WithState]
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0upload-to-server.ps1" -Server %*
pause
