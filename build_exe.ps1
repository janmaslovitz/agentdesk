<#
Builds dist\AgentDesk\AgentDesk.exe (PyInstaller, no Python needed to run it) and a Desktop shortcut "AgentDesk".
  .\build_exe.ps1 [-NoDesktopShortcut]
#>
param([switch]$NoDesktopShortcut)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$venv = Join-Path $PSScriptRoot ".venv-build"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    $py = (Get-Command py -ErrorAction SilentlyContinue)
    if ($py) { & py -3 -m venv $venv } else { & python -m venv $venv }
    if ($LASTEXITCODE) { throw "could not create the build venv (is Python 3.11+ installed?)" }
}
$vpy = "$venv\Scripts\python.exe"
& $vpy -m pip install --quiet -r requirements.txt -r requirements-tray.txt -r requirements-build.txt
if ($LASTEXITCODE) { throw "pip install failed" }
& $vpy -m PyInstaller --noconfirm --clean agentdesk.spec
if ($LASTEXITCODE) { throw "PyInstaller failed" }
$exe = Join-Path $PSScriptRoot "dist\AgentDesk\AgentDesk.exe"
if (-not (Test-Path $exe)) { throw "build produced no $exe" }
Write-Host "Built $exe"

if (-not $NoDesktopShortcut) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "AgentDesk.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)   # opens the existing shortcut if there is one: re-running updates it
    $s.TargetPath = $exe
    $s.WorkingDirectory = Split-Path $exe
    $s.IconLocation = (Join-Path $PSScriptRoot "agentdesk\web\agentdesk.ico") + ",0"
    $s.Description = "AgentDesk"
    $s.Save()
    Write-Host "Desktop shortcut: $lnk"
}
