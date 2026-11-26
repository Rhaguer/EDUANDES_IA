@echo off
chcp 65001 >nul
rem Hace que EduAndes AI arranque solo (con auto-reinicio) cada vez que inicias sesion en Windows.
rem No necesita permisos de administrador. Para quitarlo: schtasks /Delete /TN "EduAndesAI" /F
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
schtasks /Create /TN "EduAndesAI" /SC ONLOGON /RL LIMITED /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%ROOT%\scripts\laptop\supervisor.ps1\" -Port 8765"
if errorlevel 1 (echo No se pudo crear la tarea.) else (echo Listo: EduAndes AI se iniciara al abrir sesion.)
pause
