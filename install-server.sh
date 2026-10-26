#!/bin/sh
# AgentDesk Server: one-command install on Debian / Ubuntu (run as root).
#
#   curl -fsSL https://raw.githubusercontent.com/janmaslovitz/agentdesk/main/install-server.sh | sudo sh
#   curl -fsSL .../install-server.sh | sudo sh -s -- --admin alice          # also create the first admin (asks the password)
#   sudo sh install-server.sh --deb ./agentdesk-server_1.5.3_all.deb       # no GitHub on this host: copy the .deb over (scp)
#
# Options: --deb FILE | --version X.Y.Z (default: latest) | --admin NAME | --no-start | --repo OWNER/NAME
# Installs python3, python3-venv, git, curl, ca-certificates (apt), the latest agentdesk-server .deb from the GitHub
# Release, its Python packages (pypi.org, else the Yandex mirror, with retries) and starts the service.
# Needs outbound HTTPS to: your apt mirror, GitHub (unless --deb), a PyPI index, api.anthropic.com.
set -eu

REPO="janmaslovitz/agentdesk"; DEB=""; VERSION=""; ADMIN=""; START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --deb) DEB="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --admin) ADMIN="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n 2,13p "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { echo "install-server: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root (sudo sh install-server.sh)"
command -v apt-get >/dev/null || die "this installer is for Debian / Ubuntu (apt)"
export DEBIAN_FRONTEND=noninteractive

say "System packages"
# A mistyped source line ("http://https://...") makes apt fail with confusing dependency errors: repair the known typo.
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
  [ -f "$f" ] && grep -q 'http://https://' "$f" && { cp -n "$f" "$f.bak-agentdesk"; sed -i 's#http://https://#https://#g' "$f"; echo "fixed a malformed source line in $f"; }
done
apt-get update -qq || echo "apt-get update had errors; going on with what is available" >&2
apt-get install -y -qq python3 python3-venv git curl ca-certificates adduser

if [ -z "$DEB" ]; then
  say "Downloading the agentdesk-server package from GitHub ($REPO)"
  if [ -n "$VERSION" ]; then api="https://api.github.com/repos/$REPO/releases/tags/v${VERSION#v}"
  else api="https://api.github.com/repos/$REPO/releases/latest"; fi
  url="$(curl -fsSL --retry 3 --connect-timeout 15 "$api" | grep -o '"browser_download_url": *"[^"]*agentdesk-server_[^"]*_all.deb"' | head -1 | sed 's/.*"\(https[^"]*\)"/\1/')" || true
  [ -n "$url" ] || die "cannot reach GitHub or no .deb in that release. Download agentdesk-server_*_all.deb elsewhere, copy it here and run: sh install-server.sh --deb FILE"
  DEB="/tmp/$(basename "$url")"
  curl -fL --retry 3 --connect-timeout 15 -o "$DEB" "$url" || die "download failed: $url"
fi
[ -f "$DEB" ] || die "no such file: $DEB"

say "Installing $DEB"
apt-get install -y "$(readlink -f "$DEB")"

# the Python packages: the package's postinst tries pypi.org and the Yandex mirror; verify and retry once more if needed
PY=/opt/agentdesk-server/venv/bin/python
if ! "$PY" -c 'import claude_agent_sdk, mcp' 2>/dev/null; then
  say "Python packages are missing: retrying the install (apt install --reinstall)"
  apt-get install -y --reinstall "$(readlink -f "$DEB")" || true
  "$PY" -c 'import claude_agent_sdk, mcp' 2>/dev/null || die "the Python packages could not be installed (no PyPI access?). Set PIP_INDEX_URL=<your mirror> in /etc/default/agentdesk-server and run: apt-get install --reinstall $DEB"
fi

if [ -n "$ADMIN" ]; then
  say "First admin: $ADMIN"
  if [ -r /dev/tty ]; then agentdesk-server init --admin "$ADMIN" </dev/tty || true
  else echo "no terminal for the password prompt: run  sudo agentdesk-server init --admin $ADMIN  yourself" >&2; fi
fi

if [ "$START" = 1 ]; then
  systemctl enable --now agentdesk-server 2>&1 | tail -1 || true
fi

say "Check"
agentdesk-server doctor || true
cat <<MSG

Done. Next:
  1. No admin yet?   sudo agentdesk-server init            (asks for the admin password)
  2. Open            http://<this-server>:8080/            and sign in
  3. Claude access   either an API-key account in /admin (put ANTHROPIC_API_KEY_x=... into /etc/default/agentdesk-server),
                     or sign in once: sudo -u agentdesk -H $(dirname "$PY")/../lib/python3*/site-packages/claude_agent_sdk/_bundled/claude auth login
  4. HTTPS           sudo agentdesk-server config set tls CERT KEY && sudo systemctl restart agentdesk-server
MSG
