# One-command launch: installs the http extra if missing, loads a local
# .env (e.g. EAIS_LLM_* for Groq) without overriding vars already set in
# the shell, starts the web server, and opens the dashboard in a browser.
# See README.md's "Run everything with one command" section.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -c "import flask" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing eais-scheduling-agent with the http extra..."
    python -m pip install -e ".[http]"
}

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $name = $matches[1]
            if (-not (Test-Path "env:$name")) {
                Set-Item "env:$name" $matches[2]
            }
        }
    }
}

Write-Host "Starting EAIS Scheduling Agent at http://127.0.0.1:5000 (Ctrl+C to stop) ..."
Start-Job -ScriptBlock { Start-Sleep -Seconds 2; Start-Process "http://127.0.0.1:5000/" } | Out-Null

python -c "from eais_scheduling_agent.http_api import run; run()"
