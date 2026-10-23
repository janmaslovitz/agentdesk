"""`python -m agentdesk gateway ...`

    gateway [--home DIR] [--host H] [--port N]          run the gateway (settings: <home>/server.toml)
    gateway [--home DIR] user list
    gateway [--home DIR] user add NAME [--admin] [--password-stdin]
    gateway [--home DIR] user passwd NAME [--password-stdin]
    gateway [--home DIR] user disable|enable|remove NAME
    gateway [--home DIR] user role NAME admin|user
    gateway [--home DIR] user assign NAME [--project SLUG]... [--account NAME]... [--clear]

The server home is --home, else AGENTDESK_SERVER_HOME, else ~/agentdesk-server. Passwords are asked for on the
terminal (or read as one line from stdin with --password-stdin); they are never taken as arguments.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import threading

from . import config


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentdesk gateway", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="server home folder (users.json, server.toml, users/<name>/, audit.log)")
    ap.add_argument("--host", help="listen address (overrides server.toml)")
    ap.add_argument("--port", type=int, help="port (overrides server.toml; 0 = a free one)")
    sub = ap.add_subparsers(dest="cmd")
    u = sub.add_parser("user", help="manage users").add_subparsers(dest="action", required=True)
    u.add_parser("list")
    a = u.add_parser("add")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--password-stdin", action="store_true")
    pw = u.add_parser("passwd")
    pw.add_argument("name")
    pw.add_argument("--password-stdin", action="store_true")
    for n in ("disable", "enable", "remove"):
        u.add_parser(n).add_argument("name")
    r = u.add_parser("role")
    r.add_argument("name")
    r.add_argument("role", choices=["admin", "user"])
    asg = u.add_parser("assign")
    asg.add_argument("name")
    asg.add_argument("--project", action="append", default=[])
    asg.add_argument("--account", action="append", default=[])
    asg.add_argument("--clear", action="store_true", help="replace the lists (default: add to them)")
    return ap


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    p1 = getpass.getpass("Password: ")
    if getpass.getpass("Again: ") != p1:
        raise SystemExit("the passwords differ")
    return p1


def cmd_user(home, args) -> None:
    from .users import UserStore
    store = UserStore(home)
    by = f"cli:{getpass.getuser()}"
    try:
        if args.action == "list":
            for u in store.list():
                print(json.dumps(u, ensure_ascii=False))
        elif args.action == "add":
            u = store.add(args.name, read_password(args.password_stdin), "admin" if args.admin else "user", by)
            print(f"added {u['username']} ({u['role']}), data folder {u['home']}")
        elif args.action == "passwd":
            store.set_password(args.name, read_password(args.password_stdin), by)
            print(f"password of {args.name} changed (its sessions end)")
        elif args.action in ("disable", "enable"):
            store.set_disabled(args.name, args.action == "disable", by)
            print(f"{args.name} {args.action}d")
        elif args.action == "remove":
            store.remove(args.name, by)
            print(f"removed {args.name} (data folder kept: {store.user_home(args.name)})")
        elif args.action == "role":
            store.set_role(args.name, args.role, by)
            print(f"{args.name} is now {args.role}")
        elif args.action == "assign":
            cur = store.get(args.name)
            if cur is None:
                raise KeyError(f"no user {args.name!r}")
            projects = args.project if args.clear else cur["projects"] + args.project
            accounts = args.account if args.clear else cur["accounts"] + args.account
            u = store.assign(args.name, projects, accounts, by)
            print(f"{args.name}: projects {u['projects']}, accounts {u['accounts']}")
    except (KeyError, ValueError, RuntimeError) as e:
        raise SystemExit(f"agentdesk gateway: {e.args[0] if e.args else e}")


def start(argv: list[str], instances=None):
    """Bind and start serving (for main() and tests). Returns (gateway, server, stop)."""
    from .app import Gateway
    args = build_parser().parse_args(argv)
    home = config.server_home(args.home)
    cfg = config.load(home)
    if args.host is not None:
        cfg["host"] = args.host
    if args.port is not None:
        cfg["port"] = args.port
    gw = Gateway(home, cfg, instances)
    if gw.users.count() == 0:
        print(f"AgentDesk gateway: no users yet. Add one: python -m agentdesk gateway --home {home} user add NAME --admin",
              flush=True)
    srv = gw.make_server()
    quit_ev = threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True, name="gateway-http")
    thread.start()
    threading.Thread(target=gw.supervise, args=(quit_ev,), daemon=True, name="gateway-supervisor").start()
    host, port = srv.server_address[:2]
    gw.audit.log("gateway started", host=str(host), port=port, tls=gw.ssl_ctx is not None)
    print(f"AgentDesk gateway listening on {gw.scheme}://{host}:{port}  (home: {home})", flush=True)

    def stop() -> None:
        quit_ev.set()
        srv.shutdown()
        srv.server_close()
        gw.instances.stop_all()
        gw.audit.log("gateway stopped")

    srv.thread = thread  # type: ignore[attr-defined]
    return gw, srv, stop


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.cmd == "user":
        cmd_user(config.server_home(args.home), args)
        return
    _gw, srv, stop = start(argv)
    done = threading.Event()

    def on_signal(signum, _frame):
        print(f"AgentDesk gateway: signal {signum}, shutting down", flush=True)
        done.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if os.name != "nt" and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, on_signal)
    try:
        while not done.wait(0.5) and srv.thread.is_alive():
            pass
    finally:
        stop()
    print("AgentDesk gateway stopped", flush=True)
