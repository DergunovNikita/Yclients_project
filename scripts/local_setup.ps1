# Local dev bootstrap (PostgreSQL 16 + Python). Does not touch git remotes.
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$pgBin = "C:\Program Files\PostgreSQL\16\bin"
if (-not (Test-Path "$pgBin\psql.exe")) {
    Write-Error "PostgreSQL 16 not found at $pgBin. Install PostgreSQL 16 and ensure the service is running."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — set DB_PASSWORD to your local postgres password."
}

$envContent = Get-Content ".env" -Raw
if ($envContent -match 'DB_PASSWORD=(.+)') { $env:PGPASSWORD = $Matches[1].Trim() }
if (-not $env:PGPASSWORD) { Write-Error "Set DB_PASSWORD in .env" }

& "$pgBin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -tc "SELECT 1 FROM pg_database WHERE datname='yclients_db'" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Cannot connect to PostgreSQL. Check DB_PASSWORD and that postgresql-x64-16 is running." }

$exists = & "$pgBin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -tAc "SELECT 1 FROM pg_database WHERE datname='yclients_db'"
if (-not $exists) {
    & "$pgBin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -c "CREATE DATABASE yclients_db ENCODING 'UTF8';"
    Write-Host "Created database yclients_db"
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}
& ".\.venv\Scripts\pip.exe" install -q -r requirements.txt

& ".\.venv\Scripts\python.exe" migrate.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& ".\.venv\Scripts\python.exe" seed_fake_data.py --wipe --companies 3 --days 90
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Ready. Start API:  .\.venv\Scripts\uvicorn.exe api:app --host 127.0.0.1 --port 8000"
Write-Host "Start UI:       cd web; npm install; npm run dev"
Write-Host "Health:         http://127.0.0.1:8000/health"
Write-Host "Dashboard:      http://127.0.0.1:5173"
