"""Server home folder and `<server-home>/server.toml`."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .. import storage

DEFAULTS: dict = {
    "host": "0.0.0.0",
    "port": 8080,
    "tls_cert": "",                # PEM certificate chain; with tls_key the gateway speaks HTTPS
    "tls_key": "",
    "allowed_hosts": [],           # if set, the Host header must be one of these ("name" or "name:port")
    "session_hours": 12.0,         # a login lasts at most this long
    "session_idle_minutes": 120.0,  # ... and ends after this long without a request
    "login_max_failures": 5,       # failed logins per user + IP within login_window_minutes before a lockout
    "login_ip_max_failures": 30,   # failed logins per IP (any user names) within the window
    "login_window_minutes": 15.0,
    "login_lockout_minutes": 15.0,
    "idle_stop_minutes": 30.0,     # stop a user's instance after this long without requests (never while busy)
    "instance_start_seconds": 60.0,
    "run_as_users": False,         # start instances as OS user agentdesk-<username> (see README)
    "run_as": "sudo",              # "sudo" (sudo -n -u agentdesk-<username>) or "systemd-run"
    "python": "",                  # interpreter for the instances (default: the gateway's own)
    "max_body_mb": 200,            # largest request body passed through (uploads)
    "autostart_instances": True,   # at start, start the instances of users that were active before a reboot / crash
    "shutdown_grace_seconds": 25.0,  # on SIGTERM each instance gets this long to finish its step (keep < TimeoutStopSec)
    "log_max_mb": 10.0,            # rotate audit.log and the instance logs at this size ...
    "log_keep": 5,                 # ... keeping this many old files
    "min_free_gb": 1.0,            # the watchdog stops pinging (systemd restarts the service) below this much free disk
    **storage.DEFAULTS,            # disk budget, quotas, build cleanup, shared caches (docs/SERVER.md, "Disk budget")
}

EXAMPLE = """# AgentDesk Server gateway settings. Restart the gateway after a change.
host = "0.0.0.0"          # address to listen on (the LAN)
port = 8080
# tls_cert = "/etc/agentdesk/cert.pem"   # both set: HTTPS (cookies get the Secure flag)
# tls_key = "/etc/agentdesk/key.pem"
# allowed_hosts = ["agentdesk.lan", "192.168.1.5:8080"]
session_hours = 12
session_idle_minutes = 120
idle_stop_minutes = 30      # stop idle user instances (runs, chats and builds keep them alive)
autostart_instances = true  # after a reboot, start the instances of users who were active so their queues go on
# shutdown_grace_seconds = 25   log_max_mb = 10   log_keep = 5   min_free_gb = 1
run_as_users = false        # true: each instance runs as OS user agentdesk-<username> (see README)
# run_as = "sudo"           # or "systemd-run"

# Disk budget (docs/SERVER.md "Disk budget"): the whole disk, what must stay free, soft / hard limits, per-user quota
disk_total_gb = 512
disk_reserve_gb = 40
soft_limit_pct = 80         # warn and clean automatically
hard_limit_pct = 92         # refuse new runs, clones, imports and uploads (running work goes on)
quota_gb = 50               # per user; `agentdesk-server user quota NAME GB` sets one user's
# build_stale_days = 2   build_max_target_gb = 20   build_max_project_gb = 60   worktree_ttl_hours = 24   max_worktrees = 6
# clone_depth = 0   partial_clone = false   max_upload_mb = 200   shared_target = false   cache_dedupe = false
# [cache_caps_gb] is a table: cache_caps_gb = { cargo = 30, npm = 15, pip = 10, uv = 15, go = 10, gradle = 20 }
"""


def server_home(arg: str | None = None) -> Path:
    """--home, else AGENTDESK_SERVER_HOME, else ~/agentdesk-server."""
    raw = arg or os.environ.get("AGENTDESK_SERVER_HOME") or str(Path.home() / "agentdesk-server")
    return Path(raw).expanduser().resolve()


def config_path(home: Path) -> Path:
    """AGENTDESK_SERVER_CONFIG (the Debian package: /etc/agentdesk-server/server.toml), else <home>/server.toml."""
    env = os.environ.get("AGENTDESK_SERVER_CONFIG")
    return Path(env).expanduser() if env else home / "server.toml"


def load(home: Path) -> dict:
    """DEFAULTS overlaid with server.toml (written with the example on first start). Unknown keys are ignored;
    a value of the wrong type is an error (better than silently listening somewhere else)."""
    f = config_path(home)
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(EXAMPLE, encoding="utf-8")
    with f.open("rb") as fh:
        raw = tomllib.load(fh)
    cfg = dict(DEFAULTS)
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue
        want = type(DEFAULTS[k])
        if want is float and isinstance(v, int) and not isinstance(v, bool):
            v = float(v)
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            raise SystemExit(f"agentdesk gateway: {f}: {k} must be {want.__name__}")
        cfg[k] = v
    if bool(cfg["tls_cert"]) != bool(cfg["tls_key"]):
        raise SystemExit(f"agentdesk gateway: {f}: set both tls_cert and tls_key (or neither)")
    bad = storage.validate(cfg)
    if bad:
        raise SystemExit(f"agentdesk gateway: {f}: {bad}")
    if cfg["run_as"] not in ("sudo", "systemd-run"):
        raise SystemExit(f"agentdesk gateway: {f}: run_as must be \"sudo\" or \"systemd-run\"")
    return cfg
