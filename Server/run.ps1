# Start the Lumen Asset Tracking server on port 8000 (reachable from the LAN).
# First run creates .venv and installs dependencies.
# Settings are environment variables; see README.md.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}
# Load settings from lumen.env (KEY=value lines, git-ignored) unless already set.
if (Test-Path lumen.env) {
    Get-Content lumen.env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object {
        if (-not [Environment]::GetEnvironmentVariable($Matches[1])) { Set-Item "env:$($Matches[1])" $Matches[2].Trim() }
    }
}
if (-not $env:LUMEN_API_TOKEN) { Write-Warning "LUMEN_API_TOKEN not set - checkpoints must use the default token 'change-me'" }
.\.venv\Scripts\python.exe -m uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000
