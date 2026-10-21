# One-time setup on Windows (PowerShell). Run from the project root:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path config\websites.yaml)) { Copy-Item config\websites.example.yaml config\websites.yaml }
if (-not (Test-Path config\workflows.yaml)) { Copy-Item config\workflows.example.yaml config\workflows.yaml }
.\.venv\Scripts\python -m pytest -q
Write-Host "`nSetup complete. Next: put your real websites in config\websites.yaml, then:"
Write-Host "  .\.venv\Scripts\python -m app.cli sync"
Write-Host "  .\.venv\Scripts\python -m app.cli serve      # http://127.0.0.1:8000"
