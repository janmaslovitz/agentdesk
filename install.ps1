# AgentDesk setup for Windows: a private Python environment in .venv next to the app.
# Needs Python 3.11+ (https://www.python.org/downloads/ or `winget install Python.Python.3.13`) and git.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
Write-Host "AgentDesk is ready. Start it with AgentDesk.cmd (hidden, no console); stop it with AgentDesk.cmd stop."
