<#
Builds dist\installer\AgentDesk-Setup.exe: runs build_exe.ps1 (the PyInstaller program), then Inno Setup's ISCC.exe
on installer\AgentDesk.iss. Run it from a clean checkout. Needs Inno Setup 6 (winget install JRSoftware.InnoSetup).
  .\build_installer.ps1 [-Version 1.0] [-SkipExe]   # -SkipExe reuses an existing dist\AgentDesk
#>
param([string]$Version = "1.0", [switch]$SkipExe)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not $SkipExe) {
    & "$PSScriptRoot\build_exe.ps1" -NoDesktopShortcut
    if (-not $?) { throw "build_exe.ps1 failed" }
}
if (-not (Test-Path "dist\AgentDesk\AgentDesk.exe")) { throw "dist\AgentDesk\AgentDesk.exe is missing; run without -SkipExe" }
# the packed folder must hold the program only
$bad = Get-ChildItem "dist\AgentDesk" -Recurse -Force | Where-Object { $_.Name -in @("data", "accounts", ".venv", "worktrees", "__pycache__", ".env") }
if ($bad) { throw "dist\AgentDesk contains: $($bad.FullName -join ', ')" }
$iscc = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found. Install it: winget install JRSoftware.InnoSetup" }
& $iscc "/DAppVersion=$Version" "installer\AgentDesk.iss"
if ($LASTEXITCODE) { throw "ISCC failed" }
$out = Join-Path $PSScriptRoot "dist\installer\AgentDesk-Setup.exe"
if (-not (Test-Path $out)) { throw "no installer produced at $out" }
Write-Host ("Built {0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB))
