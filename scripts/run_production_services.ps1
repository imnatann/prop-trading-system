# Windows Production Services Supervisor Script
# Menjalankan urutan booting aman:
# 1. Start Risk Watchdog terlebih dahulu (Safety Plane aktif sebelum engine)
# 2. Start Trading Engine
# 3. Auto-restart loop jika proses mati tak terduga

param (
    [string]$PythonExe = ".venv\Scripts\python.exe",
    [int]$RestartDelaySeconds = 3
)

$Host.UI.RawUI.WindowTitle = "QuantumPartners Prop Trading Engine Supervisor"
Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host " Starting Institutional Prop Trading Production Services" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan

# Step 1: Start Independent Risk Watchdog as background process
Write-Host "[1/2] Launching Independent Out-of-Band Risk Watchdog..." -ForegroundColor Yellow
$watchdogProcess = Start-Process -FilePath $PythonExe -ArgumentList "services\risk_watchdog.py" -PassThru -NoNewWindow
Start-Sleep -Seconds 2

if ($watchdogProcess.HasExited) {
    Write-Host "[ERROR] Risk Watchdog failed to start! Aborting Trading Engine launch." -ForegroundColor Red
    Exit 1
}
Write-Host "[OK] Risk Watchdog is active with PID $($watchdogProcess.Id)" -ForegroundColor Green

# Step 2: Launch Trading Engine with Supervisor Loop
Write-Host "[2/2] Launching Trading Engine with Auto-Restart Supervision..." -ForegroundColor Yellow
try {
    while ($true) {
        Write-Host "[SUPERVISOR] Starting Trading Engine Service..." -ForegroundColor Cyan
        $engineProcess = Start-Process -FilePath $PythonExe -ArgumentList "services\trading_engine.py" -PassThru -NoNewWindow -Wait

        Write-Host "[WARNING] Trading Engine exited with code $($engineProcess.ExitCode). Restarting in $RestartDelaySeconds seconds..." -ForegroundColor Red
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
finally {
    if (-not $watchdogProcess.HasExited) {
        Write-Host "[CLEANUP] Stopping Risk Watchdog PID $($watchdogProcess.Id)..." -ForegroundColor Yellow
        Stop-Process -Id $watchdogProcess.Id -Force
    }
}
