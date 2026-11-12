#!/bin/bash
# Deploy AgentDesk from a git checkout to an installed AgentDesk Server (/opt/agentdesk-server) and restart it.
#
#   sudo tools/deploy_server.sh --repo /var/lib/agentdesk-server/users/<user>/repos/agentdesk [--wait] [--no-restart]
#
# Steps: refuse a dirty checkout; smoke-import the new code; back up the installed copy (keeps the last 5); copy the
# package; stamp agentdesk/_buildinfo.py with the commit (the dashboard shows it); fix permissions; restart the service
# and check its health. --wait first waits until no project has a run or an architect going (only needed while the
# installed version still ends runs on a restart; newer ones leave runs going). Output goes to stdout.
set -euo pipefail

REPO=""; WAIT=0; RESTART=1
OPT=/opt/agentdesk-server
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --wait) WAIT=1; shift ;;
    --no-restart) RESTART=0; shift ;;
    --opt) OPT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$REPO" ] || { echo "--repo is required" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
SRC="$REPO/agentdesk"; DST="$OPT/app/agentdesk"; PY="$OPT/venv/bin/python"
G() { sudo -u "$(stat -c %U "$REPO")" git -C "$REPO" "$@"; }

[ -d "$SRC" ] && [ -d "$DST" ] || { echo "no $SRC or $DST" >&2; exit 1; }
if [ -n "$(G status --porcelain --untracked-files=no)" ]; then echo "the checkout has uncommitted changes; commit first" >&2; exit 1; fi
COMMIT=$(G rev-parse --short HEAD); BUILT=$(date '+%Y-%m-%d %H:%M')
echo "$(date '+%F %T') deploying $COMMIT"

# The new code must at least import with the server's Python and dependencies.
(cd /tmp && PYTHONPATH="$REPO" "$PY" -c "import agentdesk.core, agentdesk.server, agentdesk.service, agentdesk.mcp_server, agentdesk.gateway.app") \
  || { echo "the new code doesn't import; nothing changed" >&2; exit 1; }

if [ "$WAIT" = 1 ]; then
  HOMES=$(ls -d /var/lib/agentdesk-server/users/*/ 2>/dev/null || true)
  busy() {
    for h in $HOMES; do
      sudo -u agentdesk env AGENTDESK_HOME="$h" "$PY" -c "
from agentdesk import core, service
import sys; sys.exit(0 if any(service.busy(p) or service.busy(p, 'architect') for p in core.projects()) else 1)" 2>/dev/null && return 0
    done
    return 1
  }
  echo "$(date '+%F %T') waiting for runs to end"
  while busy; do sleep 30; done
fi

BAK="/root/agentdesk.bak-$(date +%F-%H%M%S)-before-$COMMIT"
cp -a "$DST" "$BAK"
ls -d /root/agentdesk.bak-* 2>/dev/null | sort | head -n -5 | xargs -r rm -rf
rsync -a --delete --exclude __pycache__ --exclude _buildinfo.py "$SRC"/ "$DST"/
printf '# Written by tools/deploy_server.sh; not in git.\nCOMMIT = "%s"\nBUILT = "%s"\n' "$COMMIT" "$BUILT" > "$DST/_buildinfo.py"
chown -R root:root "$DST"; find "$DST" -type d -exec chmod 755 {} +; find "$DST" -type f -exec chmod 644 {} +
echo "copied; backup $BAK"

if [ "$RESTART" = 1 ]; then
  systemctl restart agentdesk-server
  for _ in $(seq 1 30); do systemctl is-active --quiet agentdesk-server && break; sleep 1; done
  echo "service: $(systemctl is-active agentdesk-server)"
fi
echo "$(date '+%F %T') done: $COMMIT"
