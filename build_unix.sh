#!/usr/bin/env bash
# Builds the AgentDesk program on macOS or Linux (PyInstaller, agentdesk.spec) and packs it into dist/:
#   Linux: dist/AgentDesk-<version>-linux-<arch>.tar.gz   (the AgentDesk/ folder; start AgentDesk/AgentDesk)
#   macOS: dist/AgentDesk-<version>-macos-<arch>.zip      (AgentDesk.app, unsigned)
# The version is the one in agentdesk/__init__.py. Needs Python 3.11+ (python3, or set PYTHON=...).
# Linux also needs the GTK / WebKit2GTK development files to build PyGObject (the window); see
# .github/workflows/release.yml for the packages. Without PyGObject the program opens the browser instead.
#   bash build_unix.sh
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "build_unix.sh is for macOS and Linux; on Windows use build_installer.ps1" >&2; exit 1 ;;
esac
arch="$(uname -m)"
venv=.venv-build
if [ ! -x "$venv/bin/python" ]; then
  "${PYTHON:-python3}" -m venv "$venv"
fi
vpy="$venv/bin/python"
"$vpy" -m pip install --quiet --upgrade pip
"$vpy" -m pip install --quiet -r requirements.txt -r requirements-tray.txt -r requirements-build.txt
if [ "$os" = linux ]; then
  # PyGObject 3.51+ needs girepository-2.0 (GLib 2.80), newer than Ubuntu 22.04 has.
  "$vpy" -m pip install --quiet "${PYGOBJECT_SPEC:-PyGObject<3.51}" \
    || echo "warning: PyGObject did not install; the Linux build will open the dashboard in the browser" >&2
fi

version="$("$vpy" tools/release.py current)"
"$vpy" tools/release.py buildinfo >/dev/null   # agentdesk/_buildinfo.py: commit and build time
rm -rf build/AgentDesk dist/AgentDesk dist/AgentDesk.app
"$vpy" -m PyInstaller --noconfirm --clean agentdesk.spec

# The packed program must hold the program only.
bad="$(find dist/AgentDesk \( -name data -o -name accounts -o -name .venv -o -name worktrees -o -name __pycache__ -o -name .env \) -print)"
if [ -n "$bad" ]; then echo "dist/AgentDesk contains: $bad" >&2; exit 1; fi

if [ "$os" = macos ] && [ -d dist/AgentDesk.app ]; then
  bin=dist/AgentDesk.app/Contents/MacOS/AgentDesk
else
  bin=dist/AgentDesk/AgentDesk
fi
[ -x "$bin" ] || { echo "build produced no $bin" >&2; exit 1; }
# Smoke test: the packed program starts and knows its version (no window: `version` only prints).
got="$("$bin" version)"
case "$got" in
  "AgentDesk $version"*) echo "$got" ;;
  *) echo "the built program says '$got', expected AgentDesk $version" >&2; exit 1 ;;
esac

if [ "$os" = linux ]; then
  out="dist/AgentDesk-$version-linux-$arch.tar.gz"
  tar -C dist -czf "$out" AgentDesk
else
  out="dist/AgentDesk-$version-macos-$arch.zip"
  src=dist/AgentDesk
  [ -d dist/AgentDesk.app ] && src=dist/AgentDesk.app
  rm -f "$out"
  ditto -c -k --sequesterRsrc --keepParent "$src" "$out"   # keeps permissions and symlinks
fi
echo "Built $out"
