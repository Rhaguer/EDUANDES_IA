@echo off
chcp 65001 >nul
powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns.OwningProcess | Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force; Write-Host ('Detenido proceso PID ' + $_) } } else { Write-Host 'No habia ningun servidor EduAndes AI escuchando en el puerto 8765.' }"
pause
