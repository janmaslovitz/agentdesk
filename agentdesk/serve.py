"""`python -m agentdesk serve` / `agentdesk-server`: the dashboard as a headless server (no window, tray or browser).

    python -m agentdesk serve [--host 127.0.0.1] [--port 8765] [--data DIR] [--allowed-host H[:PORT]]...
                              [--auth-token-file FILE [--trust-proxy-auth]] [--pid-file FILE]

Binding anything but loopback needs --auth-token-file: a shared secret that every request must carry as the header
X-AgentDesk-Gateway-Token (this is how the gateway talks to an instance). This module must not import core (or
anything GUI) before --data has been put into AGENTDESK_HOME.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

MIN_TOKEN_LEN = 16


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentdesk serve", description="Run the AgentDesk dashboard server without a GUI.")
    ap.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1; non-loopback needs --auth-token-file)")
    ap.add_argument("--port", type=int, default=8765, help="port (0 = pick a free one)")
    ap.add_argument("--data", help="data folder (sets AGENTDESK_HOME)")
    ap.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                    help="extra Host / Origin accepted (repeatable), e.g. 192.168.1.5:8765 or myserver")
    ap.add_argument("--auth-token-file", help="file holding the shared secret every request must send as X-AgentDesk-Gateway-Token")
    ap.add_argument("--trust-proxy-auth", action="store_true",
                    help="requests are authenticated by the gateway in front (requires --auth-token-file)")
    ap.add_argument("--pid-file", help="write the process id here (removed on exit)")
    return ap


def read_token(path: str) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"agentdesk serve: cannot read the token file: {e}")
    if len(token) < MIN_TOKEN_LEN:
        raise SystemExit(f"agentdesk serve: the token in {path} must be at least {MIN_TOKEN_LEN} characters")
    return token


def prescan_data(argv: list[str]) -> None:
    """Set AGENTDESK_HOME from --data before core is imported (core reads it at import time)."""
    for i, a in enumerate(argv):
        if a == "--data" and i + 1 < len(argv):
            os.environ["AGENTDESK_HOME"] = str(Path(argv[i + 1]).expanduser().resolve())
        elif a.startswith("--data="):
            os.environ["AGENTDESK_HOME"] = str(Path(a[7:]).expanduser().resolve())


def start(argv: list[str]):
    """Validate, bind and start serving. Returns (server, stop) where stop() shuts everything down."""
    args = build_parser().parse_args(argv)
    prescan_data(argv)
    if args.trust_proxy_auth and not args.auth_token_file:
        raise SystemExit("agentdesk serve: --trust-proxy-auth needs --auth-token-file")
    from . import server  # imports core; no GUI module is imported anywhere on this path
    loopback = server.is_loopback(args.host)
    if not loopback and not args.auth_token_file:
        raise SystemExit(f"agentdesk serve: refusing to listen on {args.host}: only loopback addresses are allowed without "
                         "authentication. Pass --auth-token-file FILE (the gateway then sends its secret as "
                         f"{server.GATEWAY_HEADER}), or bind 127.0.0.1.")
    token = read_token(args.auth_token_file) if args.auth_token_file else None
    from http.server import ThreadingHTTPServer
    srv = server.quiet_errors(ThreadingHTTPServer((args.host, args.port), server.Handler))
    port = srv.server_address[1]
    server.GUARD = server.Guard(server.default_allowed(args.host, port, args.allowed_host), token)
    core = server.core
    core.settings()  # creates the data folder and default settings on first start
    quit_ev = server.QUIT
    quit_ev.clear()
    pidf = Path(args.pid_file) if args.pid_file else None
    if pidf:
        pidf.write_text(str(os.getpid()), encoding="utf-8")
    thread = threading.Thread(target=server.serve_supervised, args=(srv, quit_ev), daemon=True, name="http")
    thread.start()
    for fn, name in ((server.autorun_loop, "autorunner"), (server.usage_loop, "usage-reader"),
                     (server.checkup_loop, "checkups"), (server.hygiene_loop, "disk-hygiene"), (server._load_sdk, "sdk-preload")):
        threading.Thread(target=fn, daemon=True, name=name).start()
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"AgentDesk server listening on http://{shown}:{port}  (bound {args.host}; data: {core.DATA}; "
          f"{'gateway token required' if token else 'no token'})", flush=True)

    def stop() -> None:
        quit_ev.set()
        if thread.is_alive():
            srv.shutdown()
        srv.server_close()
        if pidf:
            pidf.unlink(missing_ok=True)

    srv.thread = thread  # type: ignore[attr-defined]
    return srv, stop


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    prescan_data(argv)
    srv, stop = start(argv)
    done = threading.Event()

    def on_signal(signum, _frame):
        print(f"AgentDesk server: signal {signum}, shutting down", flush=True)
        done.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        while not done.wait(0.5) and srv.thread.is_alive():
            pass
    finally:
        stop()
    print("AgentDesk server stopped", flush=True)


if __name__ == "__main__":
    main()
