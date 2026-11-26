@echo off
chcp 65001 >nul
rem Primero se detiene el supervisor (si no, reiniciaria el servidor), luego el servidor.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -match 'supervisor\.ps1' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Supervisor detenido PID ' + $_.ProcessId) }; $conns = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns.OwningProcess | Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force; Write-Host ('Detenido proceso PID ' + $_) } } else { Write-Host 'No habia ningun servidor EduAndes AI escuchando en el puerto 8765.' }"
pause
