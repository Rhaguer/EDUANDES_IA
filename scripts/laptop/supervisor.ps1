# Supervisor de EduAndes AI: mantiene el servidor vivo en 127.0.0.1:8765.
#  - Si el proceso se cae o se cierra, lo vuelve a levantar (espera creciente si falla seguido).
#  - Si el proceso sigue vivo pero deja de responder, lo mata y lo reinicia.
#  - Guarda todo en logs\servidor.log y logs\supervisor.log
#  - Solo permite una instancia del supervisor.
param([int]$Port = 8765, [int]$CheckSeconds = 5, [int]$GraceSeconds = 60)

$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Py   = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$SupLog = Join-Path $LogDir 'supervisor.log'
$SrvLog = Join-Path $LogDir 'servidor.log'
$SrvErr = Join-Path $LogDir 'servidor.err.log'

function Log($m) { "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -Path $SupLog -Encoding UTF8 }

# Una sola instancia (mutex global del usuario)
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\EduAndesAI_Supervisor_$Port", [ref]$created)
if (-not $created) { Log "Ya hay un supervisor corriendo. Salgo."; exit 0 }

if (-not (Test-Path $Py)) { Log "No existe $Py"; exit 1 }

# Rotar log si es muy grande (>10 MB)
foreach ($f in @($SrvLog, $SrvErr, $SupLog)) {
    if ((Test-Path $f) -and (Get-Item $f).Length -gt 10MB) { Move-Item $f "$f.old" -Force }
}

# Variables de entorno (las mismas que INICIAR_EDUANDES_AI.cmd)
$env:EDUCORE_DB = Join-Path $Root 'data\educore.sqlite3'
$env:EDUCORE_ADAPTER_DIR = Join-Path $Root 'artifacts\main-01\adapter'
$env:EDUCORE_ENGINE = 'local'
$env:EDUCORE_REINFORCEMENT_ENGINE = 'trained-base'
$env:EDUCORE_ROOT = $Root
$env:HF_HOME = Join-Path $Root '.cache\huggingface'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:USE_TF = '0'
$env:TRANSFORMERS_NO_TF = '1'
$env:USE_TORCH = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = "$Root\src;$Root"

function Test-Alive {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 8
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

# Si algo ya escucha en el puerto, lo reemplazamos para que quede bajo supervision
$old = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($old) {
    $old.OwningProcess | Select-Object -Unique | ForEach-Object { Log "Reemplazo proceso previo PID $_"; Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

function Free-Port {
    # El python.exe del .venv es un lanzador con un proceso hijo: liberamos el puerto por si quedo huerfano
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($c) { $c.OwningProcess | Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }
}

$fails = 0
while ($true) {
    Free-Port
    $started = Get-Date
    Log "Iniciando servidor..."
    $p = Start-Process -FilePath $Py -ArgumentList @("`"$Root\scripts\laptop\servir.py`"", '--host', '127.0.0.1', '--port', $Port) `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $SrvLog -RedirectStandardError $SrvErr
    Log "PID $($p.Id)"

    $bad = 0
    $sawListen = $false
    while ($true) {
        Start-Sleep -Seconds $CheckSeconds
        $listening = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($listening) { $sawListen = $true }
        # Murio el lanzador, o el servidor real desaparecio despues de haber escuchado
        if ($p.HasExited -or ($sawListen -and -not $listening)) { break }
        if (((Get-Date) - $started).TotalSeconds -lt $GraceSeconds) { continue }   # periodo de arranque
        if (Test-Alive) { $bad = 0 } else { $bad++ }
        if ($bad -ge 3) {
            Log "No responde (3 chequeos fallidos). Reinicio."
            break
        }
    }
    & taskkill.exe /PID $p.Id /T /F 2>$null | Out-Null
    Free-Port
    $p.WaitForExit(5000) | Out-Null
    $ran = ((Get-Date) - $started).TotalSeconds
    Log ("Servidor termino (codigo {0}) tras {1:N0}s" -f $p.ExitCode, $ran)

    if ($ran -gt 120) { $fails = 0 } else { $fails++ }
    $wait = [Math]::Min(2 * [Math]::Pow(2, [Math]::Min($fails, 5)), 60)
    Log "Reintento en $wait s"
    Start-Sleep -Seconds $wait
}
