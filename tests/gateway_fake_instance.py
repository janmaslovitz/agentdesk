"""A stand-in for `agentdesk serve` in the gateway tests: same command line, checks the gateway token, and serves
files from its --data folder, echoes bodies, streams, reports the headers it got and a switchable busy flag."""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("serve", nargs="?")
ap.add_argument("--host")
ap.add_argument("--port", type=int)
ap.add_argument("--data")
ap.add_argument("--auth-token-file")
ap.add_argument("--trust-proxy-auth", action="store_true")
args = ap.parse_args()
TOKEN = Path(args.auth_token_file).read_text(encoding="utf-8").strip()
DATA = Path(args.data)
DATA.mkdir(parents=True, exist_ok=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body: bytes, ctype="application/octet-stream", length=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "instance=1")  # must not reach the browser
        self.end_headers()
        self.wfile.write(body)

    def ok(self):
        if self.headers.get("X-AgentDesk-Gateway-Token") != TOKEN:
            self.reply(401, b"no token")
            return False
        return True

    def do_GET(self):
        if not self.ok():
            return
        if self.path == "/api/busy":
            return self.reply(200, json.dumps({"busy": (DATA / "busy").exists()}).encode(), "application/json")
        if self.path == "/page":
            return self.reply(200, b"<html><body>fake page</body></html>", "text/html")
        if self.path == "/whoami":
            return self.reply(200, str(DATA).encode(), "text/plain")
        if self.path == "/headers":
            return self.reply(200, json.dumps(dict(self.headers.items())).encode(), "application/json")
        if self.path.startswith("/file/"):
            f = DATA / self.path[6:]
            return self.reply(200, f.read_bytes()) if f.exists() else self.reply(404, b"none")
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            for i in range(5):
                self.wfile.write(f"part{i}\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            return
        if self.path == "/exit":
            self.reply(200, b"bye")
            sys.stdout.flush()
            import os
            os._exit(3)
        self.reply(200, b"<html>fake index</html>", "text/html")

    def do_POST(self):
        if not self.ok():
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path.startswith("/file/"):
            (DATA / self.path[6:]).write_bytes(body)
            return self.reply(200, b"saved")
        self.reply(200, body, self.headers.get("Content-Type") or "application/octet-stream")


srv = ThreadingHTTPServer((args.host, args.port), H)
print(f"fake instance on {args.port} data {DATA}", flush=True)
srv.serve_forever()
