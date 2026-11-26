@echo off
chcp 65001 >nul
title EduAndes AI - Servidor Local
cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY_EXE=%ROOT%\.venv\Scripts\python.exe"
set "HF_CACHE=%ROOT%\.cache\huggingface"

if not exist "%PY_EXE%" (
  set "PY_EXE=C:\Users\NASHO\.gemini\antigravity\scratch\educore-binacional\.venv\Scripts\python.exe"
)
if not exist "%HF_CACHE%" (
  set "HF_CACHE=C:\Users\NASHO\.gemini\antigravity\scratch\educore-binacional\.cache\huggingface"
)

if not exist "%PY_EXE%" (
  echo No se encontro el entorno Python .venv. Revisa la instalacion en README.md.
  pause
  exit /b 1
)

set "EDUCORE_DB=%ROOT%\data\educore.sqlite3"
set "EDUCORE_ADAPTER_DIR=%ROOT%\artifacts\main-01\adapter"
set "EDUCORE_ENGINE=local"
set "EDUCORE_REINFORCEMENT_ENGINE=trained-base"
set "EDUCORE_ROOT=%ROOT%"
set "HF_HOME=%HF_CACHE%"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "USE_TF=0"
set "TRANSFORMERS_NO_TF=1"
set "USE_TORCH=1"
set "PYTHONUTF8=1"
set "PYTHONPATH=%ROOT%\src;%ROOT%"

echo Iniciando EduAndes AI en http://127.0.0.1:8765/#/ ...
start "" /B "%PY_EXE%" "%ROOT%\scripts\laptop\servir.py" --host 127.0.0.1 --port 8765
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8765/#/"
echo Servidor activo. Para detenerlo ejecuta DETENER_EDUANDES_AI.cmd.
