#!/usr/bin/env python3
"""Builds agentdesk-server_<version>_all.deb (needs dpkg-deb; `--stage-only DIR` needs nothing, for tests).

    python3 packaging/debian/build_deb.py [--out DIR] [--version X.Y.Z] [--bundle-wheels] [--stage-only DIR]

Layout of the package (architecture "all": pure Python + sh; the virtualenv is created on the target by postinst):
    /opt/agentdesk-server/app/agentdesk/          the program (no GUI libraries or window icons)
    /opt/agentdesk-server/requirements-server.txt requirements-core.txt (no GUI libraries; the window is not used on a server)
    /opt/agentdesk-server/wheels/                 only with --bundle-wheels: wheels for an offline install (built for THIS
                                                  machine's Python / platform; build on the same distro as the target)
    /opt/agentdesk-server/venv/                   created by postinst (python3 -m venv + pip install)
    /usr/bin/agentdesk-server                     the console admin tool (runs as user agentdesk when started as root)
    /etc/agentdesk-server/server.toml             settings (conffile)    /etc/default/agentdesk-server   environment (conffile)
    /lib/systemd/system/agentdesk-server.service  state in /var/lib/agentdesk-server, logs in journald
    /usr/share/doc/agentdesk-server/              nginx / Caddy examples, README.Debian
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

NAME = "agentdesk-server"
OPT = "opt/agentdesk-server"


def read_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "agentdesk" / "__init__.py").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("no __version__ in agentdesk/__init__.py")
    return m.group(1)


GUI_PACKAGES = re.compile(r"\s*(pywebview|pystray|pillow)\b", re.I)
GUI_FILES = {".ico", ".icns"}  # window / tray icons: only the desktop App uses them


def server_requirements() -> str:
    """requirements-core.txt (the shared core, no GUI) minus any GUI package: a server has no window."""
    lines = (ROOT / "requirements-core.txt").read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", "-")) and not GUI_PACKAGES.match(ln)]
    return "\n".join(keep) + "\n"


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))  # LF always: these run on Linux
    path.chmod(mode)


def copy_tree(src: Path, dst: Path) -> None:
    for f in sorted(src.rglob("*")):
        if f.is_dir() or "__pycache__" in f.relative_to(src).parts or f.suffix in (".pyc", ".pyo") \
                or f.suffix in GUI_FILES or f.name == "_buildinfo.py":  # _buildinfo is the App's build stamp
            continue
        t = dst / f.relative_to(src)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, t)
        t.chmod(0o644)


def control(version: str, size_kb: int) -> str:
    return f"""Package: {NAME}
Version: {version}
Section: devel
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-venv, git, adduser, ca-certificates
Recommends: nodejs, npm, nginx | caddy
Suggests: sudo
Installed-Size: {size_kb}
Maintainer: AgentDesk <noreply@example.invalid>
Description: AgentDesk Server - multi-user AgentDesk for a LAN, headless
 Runs the AgentDesk dashboard for several people on one machine: a login
 gateway on the LAN (default port 8080) and one AgentDesk instance per user.
 No window, tray or browser is needed on the server.
 .
 The Claude Code CLI is not packaged: install it with
 "sudo npm install -g @anthropic-ai/claude-code" (nodejs and npm) and sign in
 as the service user, see /usr/share/doc/agentdesk-server/README.Debian.
 Put TLS (nginx or Caddy, examples included) in front; do not expose it to
 the public internet.
"""


UNIT = """[Unit]
Description=AgentDesk Server (login gateway and per-user AgentDesk instances)
Documentation=file:///usr/share/doc/agentdesk-server/README.Debian
After=network-online.target
Wants=network-online.target
# Restart forever after crashes: never give up (a burst of failures at boot must not leave the server down).
StartLimitIntervalSec=0

[Service]
# notify: the gateway sends READY=1 after it listens and WATCHDOG=1 pings while its self-check passes; a hung
# gateway misses the pings and is killed and restarted after WatchdogSec.
Type=notify
NotifyAccess=main
WatchdogSec=60
User=agentdesk
Group=agentdesk
EnvironmentFile=-/etc/default/agentdesk-server
ExecStart=/opt/agentdesk-server/venv/bin/python -m agentdesk gateway --home ${AGENTDESK_SERVER_HOME}
Restart=always
RestartSec=3
# SIGTERM: instances get shutdown_grace_seconds (25) to finish their step; then SIGKILL after TimeoutStopSec.
TimeoutStopSec=45
SyslogIdentifier=agentdesk-server
# State (users, projects, instance data). Logs go to the journal: journalctl -u agentdesk-server
StateDirectory=agentdesk-server
StateDirectoryMode=0750
WorkingDirectory=/var/lib/agentdesk-server
Environment=HOME=/var/lib/agentdesk-server/home
UMask=0027

# Hardening. The agents run git, the claude CLI, npm, compilers ... as this user, so the system is read-only but the
# state folder (and /tmp, private to the service) are writable. Repositories outside /var/lib/agentdesk-server need
# a drop-in: systemctl edit agentdesk-server -> [Service] ReadWritePaths=/srv/repos (and ProtectHome=read-only/false
# for repositories under /home).
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
# run_as_users = true needs sudo, which NoNewPrivileges forbids: override it in a drop-in (see README).

[Install]
WantedBy=multi-user.target
"""

DEFAULTS_FILE = """# Environment of agentdesk-server.service and of the `agentdesk-server` command.
# Python package index for the install / upgrade (pip reads PIP_INDEX_URL, PIP_PROXY). Uncomment behind a firewall:
#PIP_INDEX_URL=https://mirror.yandex.ru/pypi/web/simple/
AGENTDESK_SERVER_HOME=/var/lib/agentdesk-server
AGENTDESK_SERVER_CONFIG=/etc/agentdesk-server/server.toml
"""

WRAPPER = """#!/bin/sh
# AgentDesk Server console tool: agentdesk-server init|user|status|config|doctor   (agentdesk-server --help)
[ -r /etc/default/agentdesk-server ] && . /etc/default/agentdesk-server
export AGENTDESK_SERVER_HOME="${AGENTDESK_SERVER_HOME:-/var/lib/agentdesk-server}"
export AGENTDESK_SERVER_CONFIG="${AGENTDESK_SERVER_CONFIG:-/etc/agentdesk-server/server.toml}"
PY=/opt/agentdesk-server/venv/bin/python
[ -x "$PY" ] || { echo "agentdesk-server: $PY is missing; run: sudo apt install --reinstall ./agentdesk-server_*.deb" >&2; exit 1; }
# As root, act as the service user so that files the tool creates belong to it.
if [ "$(id -u)" = 0 ] && id agentdesk >/dev/null 2>&1; then
  exec runuser -u agentdesk -- env "AGENTDESK_SERVER_HOME=$AGENTDESK_SERVER_HOME" \\
    "AGENTDESK_SERVER_CONFIG=$AGENTDESK_SERVER_CONFIG" HOME=/var/lib/agentdesk-server/home "$PY" -m agentdesk server "$@"
fi
exec "$PY" -m agentdesk server "$@"
"""

POSTINST = """#!/bin/sh
set -e
OPT=/opt/agentdesk-server
STATE=/var/lib/agentdesk-server

case "$1" in
  configure)
    if ! getent passwd agentdesk >/dev/null; then
      adduser --system --group --home "$STATE/home" --no-create-home --shell /usr/sbin/nologin \\
        --gecos "AgentDesk Server" agentdesk
    fi
    install -d -o agentdesk -g agentdesk -m 0750 "$STATE" "$STATE/home"
    chown root:agentdesk /etc/agentdesk-server /etc/agentdesk-server/server.toml
    chmod 0750 /etc/agentdesk-server
    chmod 0640 /etc/agentdesk-server/server.toml

    # The virtualenv: only what the server needs (no pywebview). Recreated when python3 changed.
    if [ ! -x "$OPT/venv/bin/python" ] || ! "$OPT/venv/bin/python" -c 'import sys' 2>/dev/null; then
      rm -r -f "$OPT/venv"
      python3 -m venv "$OPT/venv"
    fi
    SITE="$("$OPT/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    echo "$OPT/app" > "$SITE/agentdesk-app.pth"
    [ -r /etc/default/agentdesk-server ] && . /etc/default/agentdesk-server && export PIP_INDEX_URL PIP_PROXY
    ok=0
    if [ -d "$OPT/wheels" ] && "$OPT/venv/bin/python" -m pip install --quiet --no-index --find-links "$OPT/wheels" \\
         -r "$OPT/requirements-server.txt"; then
      ok=1
    elif "$OPT/venv/bin/python" -m pip install --quiet --disable-pip-version-check --timeout 60 --retries 5 -r "$OPT/requirements-server.txt"; then
      ok=1
    elif "$OPT/venv/bin/python" -m pip install --quiet --disable-pip-version-check --timeout 60 --retries 5 \
           --index-url https://mirror.yandex.ru/pypi/web/simple/ -r "$OPT/requirements-server.txt"; then
      ok=1  # pypi.org unreachable (hosts that only see the Yandex mirrors): the Yandex PyPI mirror
    fi
    if [ "$ok" != 1 ]; then
      echo "agentdesk-server: could not install the Python packages (no network?)." >&2
      echo "  Retry later with: sudo apt install --reinstall ./agentdesk-server_*.deb" >&2
      echo "  or by hand: sudo $OPT/venv/bin/pip install --timeout 120 --retries 10 -r $OPT/requirements-server.txt   (a mirror: add --index-url URL; a proxy: --proxy URL)" >&2
    fi
    "$OPT/venv/bin/python" -m compileall -q "$OPT/app" >/dev/null 2>&1 || true

    if [ -d /run/systemd/system ]; then
      systemctl daemon-reload || true
      # enabled at install: the server comes back by itself after a reboot (started once `init` made the first admin)
      systemctl enable agentdesk-server >/dev/null 2>&1 || true
      if systemctl is-active --quiet agentdesk-server 2>/dev/null; then
        systemctl try-restart agentdesk-server || true
      fi
    fi
    cat <<'MSG'

AgentDesk Server is installed and enabled at boot, but not started yet. Next:
  sudo agentdesk-server init                 # first admin (asks for a password)
  sudo systemctl start agentdesk-server
  sudo agentdesk-server doctor               # git, claude CLI and login, port, disk
See /usr/share/doc/agentdesk-server/README.Debian (TLS, firewall, claude login).

MSG
    ;;
esac
exit 0
"""

PRERM = """#!/bin/sh
set -e
case "$1" in
  remove|deconfigure)
    if [ -d /run/systemd/system ]; then
      systemctl stop agentdesk-server 2>/dev/null || true
      systemctl disable agentdesk-server 2>/dev/null || true
    fi
    ;;
esac
exit 0
"""

POSTRM = """#!/bin/sh
set -e
case "$1" in
  remove|purge)
    rm -r -f /opt/agentdesk-server/venv /opt/agentdesk-server/app
    if [ -d /run/systemd/system ]; then systemctl daemon-reload || true; fi
    ;;
esac
# State in /var/lib/agentdesk-server (users, projects) and the system user are kept, even on purge: delete by hand.
exit 0
"""

README_DEBIAN = """AgentDesk Server on Debian / Ubuntu
===================================

Files
  /etc/agentdesk-server/server.toml    settings (agentdesk-server config get|set ...)
  /var/lib/agentdesk-server/           state: users.json, users/<name>/, audit.log (back this up)
  journalctl -u agentdesk-server       logs
  service user: agentdesk (home /var/lib/agentdesk-server/home)

First steps
  sudo agentdesk-server init
  sudo systemctl enable --now agentdesk-server
  sudo agentdesk-server doctor

Claude Code CLI (not packaged; needs nodejs and npm)
  sudo npm install -g @anthropic-ai/claude-code
  sudo -u agentdesk -H claude          # sign in once; or give users API-key accounts in /admin

Network
  The gateway listens on 0.0.0.0:8080 by default: LAN only. Allow only your LAN, e.g.
    sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
  Do not forward the port from a router or expose it to the internet.
  For HTTPS put nginx or Caddy in front (nginx-agentdesk.conf.example, Caddyfile.example), then set
    sudo agentdesk-server config set host 127.0.0.1
  so that only the proxy can reach the gateway, and restart the service.

Separate OS accounts per user (run_as_users) need sudo, which the unit's NoNewPrivileges=true forbids:
  sudo systemctl edit agentdesk-server      ->   [Service]
                                                 NoNewPrivileges=false
See the project README ("Server (Debian, headless)") for details.
"""

NGINX = """# /etc/nginx/sites-available/agentdesk  -  TLS in front of the AgentDesk gateway.
# agentdesk-server config set host 127.0.0.1   (then the gateway is reachable only through this proxy)
# agentdesk-server config set allowed_hosts agentdesk.lan
server {
    listen 443 ssl;
    server_name agentdesk.lan;
    ssl_certificate     /etc/ssl/agentdesk/fullchain.pem;
    ssl_certificate_key /etc/ssl/agentdesk/privkey.pem;

    client_max_body_size 200m;           # uploads / project zips (max_body_mb in server.toml)

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_buffering off;             # streamed chat answers
        proxy_read_timeout 3600s;
    }
}
server { listen 80; server_name agentdesk.lan; return 301 https://$host$request_uri; }
"""

CADDY = """# /etc/caddy/Caddyfile  -  TLS in front of the AgentDesk gateway.
# On a LAN use Caddy's internal CA ("tls internal") or your own certificate.
agentdesk.lan {
    tls internal
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1                # streamed chat answers
    }
}
"""

COPYRIGHT = "AgentDesk Server. See the project repository for the license.\n"


def stage(dest: Path, version: str | None = None, bundle_wheels: bool = False) -> Path:
    """Write the package tree (including DEBIAN/) into dest, which must not exist yet."""
    from agentdesk.gateway import config
    version = version or read_version()
    if dest.exists():
        raise SystemExit(f"{dest} exists already")
    copy_tree(ROOT / "agentdesk", dest / OPT / "app" / "agentdesk")
    write(dest / OPT / "requirements-server.txt", server_requirements())
    if bundle_wheels:
        wheels = dest / OPT / "wheels"
        wheels.mkdir(parents=True)
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "-r", str(dest / OPT / "requirements-server.txt"),
                        "-d", str(wheels)], check=True)
    write(dest / "usr/bin/agentdesk-server", WRAPPER, 0o755)
    write(dest / "etc/agentdesk-server/server.toml", config.EXAMPLE)
    write(dest / "etc/default/agentdesk-server", DEFAULTS_FILE)
    write(dest / "lib/systemd/system/agentdesk-server.service", UNIT)
    doc = dest / "usr/share/doc" / NAME
    write(doc / "README.Debian", README_DEBIAN)
    write(doc / "nginx-agentdesk.conf.example", NGINX)
    write(doc / "Caddyfile.example", CADDY)
    write(doc / "copyright", COPYRIGHT)
    d = dest / "DEBIAN"
    size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024 + 1
    write(d / "control", control(version, size_kb))
    write(d / "conffiles", "/etc/agentdesk-server/server.toml\n/etc/default/agentdesk-server\n")
    for n, t in (("postinst", POSTINST), ("prerm", PRERM), ("postrm", POSTRM)):
        write(d / n, t, 0o755)
    for dd in [dest, *[p for p in dest.rglob("*") if p.is_dir()]]:
        dd.chmod(0o755)
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist"), help="folder for the .deb (default dist/)")
    ap.add_argument("--version", help="package version (default: agentdesk/__init__.py)")
    ap.add_argument("--bundle-wheels", action="store_true", help="include the Python wheels for an offline install")
    ap.add_argument("--stage-only", metavar="DIR", help="only write the package tree to DIR (new folder), no dpkg-deb")
    a = ap.parse_args(argv)
    version = a.version or read_version()
    if a.stage_only:
        print(stage(Path(a.stage_only), version, a.bundle_wheels))
        return 0
    if not shutil.which("dpkg-deb"):
        print("build_deb.py: dpkg-deb not found (run this on Debian / Ubuntu: apt install dpkg)", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / "build" / f"deb-{version}"
    if tmp.exists():
        shutil.rmtree(tmp)
    stage(tmp, version, a.bundle_wheels)
    deb = out / f"{NAME}_{version}_all.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(tmp), str(deb)], check=True)
    shutil.rmtree(tmp)
    print(deb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
