<#
Makes a release: runs the tests, bumps the version, moves CHANGELOG.md's "Unreleased" notes under the new version,
commits "Release x.y.z", tags vx.y.z and builds dist\installer\AgentDesk-Setup-x.y.z.exe. Never pushes.
  .\release.ps1                      # patch release: 1.1.0 -> 1.1.1
  .\release.ps1 -Bump minor          # 1.1.0 -> 1.2.0   (major: 2.0.0)
  .\release.ps1 -Version 1.4.0       # an exact version
  .\release.ps1 -SkipTests -NoBuild  # only the bookkeeping
Write what changed under "## Unreleased" in CHANGELOG.md first; the tracked files must be committed.
#>
param([ValidateSet("patch", "minor", "major")][string]$Bump = "patch", [string]$Version = "", [switch]$SkipTests, [switch]$NoBuild)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
if (-not $SkipTests) {
    & $py -m pytest -q -p no:cacheprovider tests
    if ($LASTEXITCODE) { throw "the tests failed: not releasing" }
}
$relArgs = if ($Version) { @("--version", $Version) } else { @("--bump", $Bump) }
$out = & python tools\release.py release @relArgs
if ($LASTEXITCODE) { throw "release failed (see the message above)" }
$new = ($out | Select-Object -Last 1).Trim()
Write-Host "Released ${new}: committed as 'Release ${new}' and tagged v${new}. Nothing was pushed (git push --follow-tags when you want to)."
if (-not $NoBuild) { & "$PSScriptRoot\build_installer.ps1" -Version $new }
