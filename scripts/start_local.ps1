# Start local API + dashboard (run after scripts/local_setup.ps1).
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

if (-not (Test-Path ".\.venv\Scripts\uvicorn.exe")) {
    Write-Error "Run scripts/local_setup.ps1 first."
}

Write-Host "API:       http://127.0.0.1:8000/health"
Write-Host "Dashboard: http://127.0.0.1:5173"
Write-Host "Press Ctrl+C in each window to stop."

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$Root'; .\.venv\Scripts\uvicorn.exe api:app --host 127.0.0.1 --port 8000"
)

Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$Root\web'; npm run dev"
)
