<#
Makes a release: runs the tests, bumps the version, moves CHANGELOG.md's "Unreleased" notes under the new version,
commits "Release x.y.z", tags vx.y.z and builds the chosen product(s). Never pushes.

Two products share one version and one core:
  app     the desktop App (Windows): dist\installer\AgentDesk-Setup-x.y.z.exe          (default; needs Inno Setup)
  server  the headless Server (Linux): dist\agentdesk-server_x.y.z_all.deb             (needs dpkg-deb: Linux or WSL)
  both    both of the above

Usage:
  .\release.ps1                          # patch release 1.1.0 -> 1.1.1, builds the App
  .\release.ps1 -Target server           # builds only the .deb (through WSL on Windows; CI builds it on the tag push too)
  .\release.ps1 -Target both -Bump minor # 1.1.0 -> 1.2.0   (-Bump major: 2.0.0)
  .\release.ps1 -Version 1.4.0           # an exact version
  .\release.ps1 -SkipTests -NoBuild      # only the bookkeeping (tag + changelog), no artifact
Write what changed under "## Unreleased" in CHANGELOG.md first; the tracked files must be committed.
#>
param([ValidateSet("patch", "minor", "major")][string]$Bump = "patch", [string]$Version = "",
      [ValidateSet("app", "server", "both")][string]$Target = "app", [switch]$SkipTests, [switch]$NoBuild)
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
if ($NoBuild) { return }
if ($Target -in "app", "both") { & "$PSScriptRoot\build_installer.ps1" -Version $new }
if ($Target -in "server", "both") {
    if (Get-Command dpkg-deb -ErrorAction SilentlyContinue) {
        & python packaging\debian\build_deb.py --out dist --version $new
    } elseif (Get-Command wsl -ErrorAction SilentlyContinue) {
        & wsl python3 packaging/debian/build_deb.py --out dist --version $new
    } else {
        Write-Warning "No dpkg-deb or WSL here: the .deb was not built. Run 'python3 packaging/debian/build_deb.py' on Debian / Ubuntu, or push the tag (CI builds agentdesk-server_${new}_all.deb)."
        return
    }
    if ($LASTEXITCODE) { throw "build_deb.py failed" }
}
