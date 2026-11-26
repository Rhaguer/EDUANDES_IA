@echo off
chcp 65001 >nul
title EduAndes AI - Servidor Local
cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo No se encontro el entorno Python .venv. Revisa la instalacion en README.md.
  pause
  exit /b 1
)

echo Iniciando EduAndes AI (con auto-reinicio) en http://127.0.0.1:8765/#/ ...
rem El supervisor corre oculto y desacoplado: cerrar esta ventana NO apaga el servidor.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%\scripts\laptop\supervisor.ps1" -Port 8765

echo Esperando que el servidor responda...
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){ try { if((Invoke-WebRequest 'http://127.0.0.1:8765/' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep 1 }; exit 1"
start "" "http://127.0.0.1:8765/#/"
echo Servidor activo. Logs en la carpeta logs\. Para detenerlo ejecuta DETENER_EDUANDES_AI.cmd.
timeout /t 3 >nul
