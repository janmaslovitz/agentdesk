"""`python -m agentdesk server ...` / `agentdesk-server`: console administration of an AgentDesk Server, over SSH, no browser.

    agentdesk-server [--home DIR] init [--admin NAME] [--admin-password-file FILE] [--host H] [--port N]
    agentdesk-server [--home DIR] user add NAME [--admin] [--password-file FILE]
    agentdesk-server [--home DIR] user list | passwd NAME [--password-file FILE] | enable NAME | disable NAME | delete NAME [--purge]
    agentdesk-server [--home DIR] status
    agentdesk-server [--home DIR] config get [KEY] | set KEY VALUE...      (host, port, tls, idle_timeout, run_as_users, ...)
    agentdesk-server [--home DIR] doctor

The server home is --home, else AGENTDESK_SERVER_HOME, else ~/agentdesk-server. The settings file is
AGENTDESK_SERVER_CONFIG, else <home>/server.toml. Passwords are asked on the terminal (twice) or read from a file
(--password-file / --admin-password-file; first line); they are never arguments. Stdlib only.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import config

# console name -> (server.toml key, kind)
SETTINGS = {
    "host": ("host", "str"),
    "port": ("port", "int"),
    "idle_timeout": ("idle_stop_minutes", "float"),
    "run_as_users": ("run_as_users", "bool"),
    "run_as": ("run_as", "str"),
    "tls_cert": ("tls_cert", "str"),
    "tls_key": ("tls_key", "str"),
    "session_hours": ("session_hours", "float"),
    "session_idle_minutes": ("session_idle_minutes", "float"),
    "allowed_hosts": ("allowed_hosts", "list"),
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentdesk-server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="server home folder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create the server home, server.toml and the first admin")
    i.add_argument("--admin", default="admin", help="name of the first admin (default admin)")
    i.add_argument("--admin-password-file", help="read the admin password from this file instead of asking")
    i.add_argument("--host")
    i.add_argument("--port", type=int)
    u = sub.add_parser("user", help="manage users").add_subparsers(dest="action", required=True)
    u.add_parser("list")
    a = u.add_parser("add")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--password-file")
    pw = u.add_parser("passwd")
    pw.add_argument("name")
    pw.add_argument("--password-file")
    for n in ("disable", "enable"):
        u.add_parser(n).add_argument("name")
    d = u.add_parser("delete")
    d.add_argument("name")
    d.add_argument("--purge", action="store_true", help="also delete the user's data folder (default: keep it)")
    sub.add_parser("status")
    c = sub.add_parser("config", help="show or change server.toml").add_subparsers(dest="action", required=True)
    g = c.add_parser("get")
    g.add_argument("key", nargs="?")
    s = c.add_parser("set", help="keys: " + ", ".join(SETTINGS) + ", tls (CERT KEY | off)")
    s.add_argument("key")
    s.add_argument("value", nargs="+")
    sub.add_parser("doctor", help="check git, the claude CLI and its login, the port, permissions, disk")
    return ap


def die(msg: str) -> SystemExit:
    return SystemExit(f"agentdesk-server: {msg}")


def read_password(file: str | None) -> str:
    if file:
        try:
            lines = Path(file).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise die(f"can't read the password file: {e}")
        if not lines or not lines[0]:
            raise die(f"{file} is empty")
        return lines[0]
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    p1 = getpass.getpass("Password: ")
    if getpass.getpass("Again: ") != p1:
        raise die("the passwords differ")
    return p1


def store(home: Path):
    from .users import UserStore
    return UserStore(home)


def secure(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# ----------------------------------------------------------------------------- settings file

def render(kind: str, raw: str) -> str:
    if kind == "str":
        return json.dumps(raw)
    if kind == "bool":
        v = raw.strip().lower()
        if v not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
            raise die(f"{raw!r} is not true/false")
        return "true" if v in ("true", "yes", "on", "1") else "false"
    try:
        n = int(raw) if kind == "int" else float(raw)
    except ValueError:
        raise die(f"{raw!r} is not a number")
    return str(n)


def set_key(text: str, key: str, value: str) -> str:
    """Replace the (uncommented) `key = ...` line, else uncomment a `# key = ...` example line, else append."""
    line = f"{key} = {value}"
    m = re.search(rf"(?m)^{re.escape(key)}[ \t]*=[^#\n]*?([ \t]*#.*)?$", text)
    if m:
        return text[:m.start()] + line + (m.group(1) or "") + text[m.end():]
    m = re.search(rf"(?m)^#[ \t]*{re.escape(key)}[ \t]*=.*$", text)
    if m:
        return text[:m.start()] + line + text[m.end():]
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def change_settings(home: Path, changes: dict[str, str]) -> None:
    f = config.config_path(home)
    config.load(home)  # creates the file with the example if missing
    old = f.read_text(encoding="utf-8")
    text = old
    for k, v in changes.items():
        text = set_key(text, k, v)
    f.write_text(text, encoding="utf-8")
    try:
        config.load(home)
    except SystemExit as e:  # wrong type / half-set TLS: put the old file back
        f.write_text(old, encoding="utf-8")
        raise die(f"not changed: {e}")


def cmd_config(home: Path, args) -> None:
    if args.action == "get":
        cfg = config.load(home)
        if args.key:
            key = SETTINGS.get(args.key, (args.key, ""))[0]
            if key not in cfg:
                raise die(f"unknown setting {args.key!r}")
            print(cfg[key] if isinstance(cfg[key], str) else json.dumps(cfg[key]))
        else:
            for name, (key, _k) in SETTINGS.items():
                print(f"{name} = {json.dumps(cfg[key])}")
            print(f"tls = {'on' if cfg['tls_cert'] else 'off'}")
            print(f"# file: {config.config_path(home)}")
        return
    key, vals = args.key, args.value
    if key == "tls":
        if vals == ["off"]:
            changes = {"tls_cert": render("str", ""), "tls_key": render("str", "")}
        elif len(vals) == 2:
            for p in vals:
                if not Path(p).is_file():
                    raise die(f"{p}: no such file")
            changes = {"tls_cert": render("str", vals[0]), "tls_key": render("str", vals[1])}
        else:
            raise die("tls takes CERT_FILE KEY_FILE, or off")
    elif key in SETTINGS:
        toml_key, kind = SETTINGS[key]
        if kind == "list":
            changes = {toml_key: "[" + ", ".join(json.dumps(v) for v in vals) + "]"}
        elif len(vals) != 1:
            raise die(f"{key} takes one value")
        else:
            changes = {toml_key: render(kind, vals[0])}
    else:
        raise die(f"unknown setting {key!r} (known: {', '.join(SETTINGS)}, tls)")
    change_settings(home, changes)
    print("saved; restart the service to apply:  sudo systemctl restart agentdesk-server")


# ----------------------------------------------------------------------------- commands

def cmd_init(home: Path, args) -> None:
    home.mkdir(parents=True, exist_ok=True)
    secure(home, 0o750)
    f = config.config_path(home)
    existed = f.exists()
    cfg = config.load(home)  # writes the commented defaults when missing
    changes = {}
    if args.host is not None:
        changes["host"] = render("str", args.host)
    if args.port is not None:
        changes["port"] = render("int", str(args.port))
    if changes:
        change_settings(home, changes)
        cfg = config.load(home)
    secure(f, 0o640)
    print(f"{'kept' if existed else 'created'} {f}")
    st = store(home)
    if st.count() == 0:
        pw = read_password(args.admin_password_file)
        try:
            u = st.add(args.admin, pw, "admin", f"cli:{getpass.getuser()}")
        except ValueError as e:
            raise die(str(e))
        print(f"first admin '{u['username']}' created")
    else:
        print(f"{st.count()} user(s) exist already; no admin created")
    secure(home / "users.json", 0o600)
    print(f"server home: {home}\nnext: start the service, then open http://<this-server>:{cfg['port']}/ and sign in.\n"
          "Run `agentdesk-server doctor` to check the machine (git, claude login, ...).")


def cmd_user(home: Path, args) -> None:
    st, by = store(home), f"cli:{getpass.getuser()}"
    try:
        if args.action == "list":
            users = st.list()
            if not users:
                print("no users")
            for u in users:
                flags = u["role"] + (", disabled" if u["disabled"] else "")
                print(f"{u['username']:<22} {flags:<16} projects: {len(u['projects'])}  home: {u['home']}")
        elif args.action == "add":
            u = st.add(args.name, read_password(args.password_file), "admin" if args.admin else "user", by)
            print(f"added {u['username']} ({u['role']}), data folder {u['home']}")
        elif args.action == "passwd":
            st.set_password(args.name, read_password(args.password_file), by)
            print(f"password of {args.name} changed (its sessions end)")
        elif args.action in ("disable", "enable"):
            st.set_disabled(args.name, args.action == "disable", by)
            print(f"{args.name} {args.action}d")
        elif args.action == "delete":
            st.remove(args.name, by, delete_data=args.purge)
            print(f"deleted {args.name} ({'data folder deleted' if args.purge else 'data folder kept: ' + str(st.user_home(args.name))})")
    except (KeyError, ValueError, RuntimeError) as e:
        raise die(str(e.args[0] if e.args else e))


def listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1" if host in ("0.0.0.0", "") else host, port), timeout=1):
            return True
    except OSError:
        return False


def fetch_health(cfg: dict) -> dict | None:
    """The gateway's /gateway/health answer (no auth needed), or None when it doesn't answer."""
    import http.client
    import ssl
    host = "127.0.0.1" if cfg["host"] in ("0.0.0.0", "", "::") else cfg["host"]
    allowed = cfg.get("allowed_hosts") or []
    try:
        if cfg["tls_cert"]:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            c: http.client.HTTPConnection = http.client.HTTPSConnection(host, cfg["port"], timeout=3, context=ctx)
        else:
            c = http.client.HTTPConnection(host, cfg["port"], timeout=3)
        c.request("GET", "/gateway/health", headers={"Host": allowed[0]} if allowed else {})
        r = c.getresponse()
        data = json.loads(r.read(65536) or b"{}")
        c.close()
        return data if r.status == 200 and isinstance(data, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def health_text(h: dict | None) -> str:
    if not h:
        return "no answer from /gateway/health"
    i = h.get("instances") or {}
    return f"{'ok' if h.get('ok') else 'NOT ok'}, version {h.get('version', '?')}, instances {i.get('up', 0)} up / {i.get('down', 0)} down"


def systemd_state() -> str:
    if not shutil.which("systemctl"):
        return "n/a"
    try:
        r = subprocess.run(["systemctl", "is-active", "agentdesk-server"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def cmd_status(home: Path, _args) -> None:
    cfg = config.load(home)
    users = store(home).list()
    scheme = "https" if cfg["tls_cert"] else "http"
    reach = "accepting connections" if listening(cfg["host"], cfg["port"]) else "not reachable"
    print(f"home:      {home}\nsettings:  {config.config_path(home)}")
    print(f"listen:    {scheme}://{cfg['host']}:{cfg['port']}  ({reach})")
    print(f"service:   {systemd_state()}")
    print(f"health:    {health_text(fetch_health(cfg))}")
    print(f"users:     {len(users)} ({sum(1 for u in users if u['role'] == 'admin')} admin, {sum(1 for u in users if u['disabled'])} disabled)")
    inst = home / "instances"
    print(f"instances: {len(list(inst.glob('*'))) if inst.is_dir() else 0} record(s) in {inst}")
    print(f"disk free: {shutil.disk_usage(home).free / 2**30:.1f} GiB")
    print(f"run_as_users: {cfg['run_as_users']}")


def claude_login_hint() -> tuple[bool, str]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, "ANTHROPIC_API_KEY is set"
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for p in (base / ".credentials.json", Path.home() / ".claude.json"):
        try:
            if p.is_file() and "oauth" in p.read_text(encoding="utf-8", errors="replace").lower():
                return True, f"login found in {p}"
        except OSError:
            pass
    return False, f"no login found for {getpass.getuser()} (HOME={Path.home()})"


def cmd_doctor(home: Path, _args) -> int:
    bad = 0

    def line(level: str, msg: str) -> None:
        nonlocal bad
        bad += level == "FAIL"
        print(f"[{level:<4}] {msg}")

    v = sys.version_info
    line("ok" if v >= (3, 11) else "FAIL", f"python {v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 11) else " (3.11 or newer needed)"))
    git = shutil.which("git")
    line("ok" if git else "FAIL", f"git: {git or 'not found (apt install git)'}")
    claude = shutil.which("claude")
    if claude:
        line("ok", f"claude CLI: {claude}")
        ok, why = claude_login_hint()
        line("ok" if ok else "WARN", f"claude login: {why}" + ("" if ok else " (run `claude` once as this user and sign in, or use an API-key account)"))
    else:
        line("WARN", "claude CLI not found (npm install -g @anthropic-ai/claude-code; needs nodejs/npm)")
    try:
        cfg = config.load(home)
    except SystemExit as e:
        line("FAIL", str(e))
        print("1 problem(s) found")
        return 1
    line("ok", f"settings {config.config_path(home)} parse")
    port = cfg["port"]
    if port and listening(cfg["host"], port):
        line("ok" if systemd_state() == "active" else "WARN", f"port {port} is in use (the server itself if it runs, else another program)")
    elif port:
        try:
            with socket.socket() as s:
                s.bind((cfg["host"], port))
            line("ok", f"port {port} is free to bind on {cfg['host']}")
        except OSError as e:
            line("WARN", f"can't bind {cfg['host']}:{port}: {e}")
    if cfg["host"] not in ("127.0.0.1", "localhost", "::1") and not cfg["tls_cert"]:
        line("WARN", "listening on the network without TLS: passwords cross the LAN in clear text (set tls, or put nginx/Caddy in front)")
    if home.is_dir():
        w = os.access(home, os.W_OK)
        line("ok" if w else "FAIL", f"server home {home} " + ("is writable" if w else "is not writable by this user"))
        uj = home / "users.json"
        if uj.exists() and os.name != "nt" and uj.stat().st_mode & 0o077:
            line("WARN", f"{uj} is readable by others (chmod 600)")
    else:
        line("FAIL", f"server home {home} does not exist (run `agentdesk-server init`)")
    h = fetch_health(cfg) if port else None
    if h:
        line("ok" if h.get("ok") else "FAIL", f"health: {health_text(h)}")
    elif port and listening(cfg["host"], port):
        line("FAIL", "the port is open but /gateway/health does not answer (hung? systemd's watchdog restarts it)")
    if shutil.which("systemctl"):
        wd = subprocess.run(["systemctl", "show", "agentdesk-server", "-p", "WatchdogUSec", "--value"], capture_output=True, text=True).stdout.strip()
        if wd and wd not in ("0", "infinity"):
            line("ok", f"systemd watchdog: {wd}")
        elif systemd_state() != "n/a":
            line("WARN", "no systemd watchdog on agentdesk-server.service (WatchdogSec=60 in the packaged unit)")
    free = shutil.disk_usage(home if home.exists() else Path.home()).free / 2**30
    line("ok" if free >= 5 else ("WARN" if free >= 1 else "FAIL"), f"disk free: {free:.1f} GiB")
    if cfg["run_as_users"] and cfg["run_as"] == "sudo" and not shutil.which("sudo"):
        line("FAIL", "run_as_users with run_as=sudo, but sudo is not installed")
    print("all good" if not bad else f"{bad} problem(s) found")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    home = config.server_home(args.home)
    rc = 0
    if args.cmd == "init":
        cmd_init(home, args)
    elif args.cmd == "user":
        cmd_user(home, args)
    elif args.cmd == "status":
        cmd_status(home, args)
    elif args.cmd == "config":
        cmd_config(home, args)
    elif args.cmd == "doctor":
        rc = cmd_doctor(home, args)
    if rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
