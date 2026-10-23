"""The gateway's HTTP side: login / logout pages, sessions, CSRF checks, and the proxy to the user's instance.

Routes of the gateway itself: /login, /logout, /gateway/health, /gateway/me. Every other path of a logged-in user goes
unchanged to that user's instance (the instance is looked up from the server-side session only).
"""

from __future__ import annotations

import hmac
import html
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .instances import GATEWAY_HEADER, InstanceError, InstanceManager
from .sessions import SessionTable, Throttle
from .users import Audit, UserStore

CSRF_HEADER = "X-AgentDesk-CSRF"
USER_HEADER = "X-AgentDesk-User"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer",
       "trailers", "transfer-encoding", "upgrade"}
# Never passed to an instance: the gateway's cookie, our own control headers (a client must not set them), and the
# browser's Origin / Referer (replaced: the instance only accepts its own loopback origin).
DROP_REQUEST = HOP | {"host", "cookie", "origin", "referer", "content-length", GATEWAY_HEADER.lower(), USER_HEADER.lower(),
                      CSRF_HEADER.lower(), "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded"}
DROP_RESPONSE = HOP | {"set-cookie", "server", "date"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CHUNK = 64 * 1024


def safe_next(target: str | None) -> str:
    """Only a local path may be the place to go after login (no //host, no scheme, no backslash tricks)."""
    t = (target or "").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or any(ord(c) < 32 for c in t):
        return "/"
    return t


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - AgentDesk</title>
<style>
body{{font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f4f5f7;color:#1d2330;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
@media (prefers-color-scheme:dark){{body{{background:#16181d;color:#e6e8ec}}form,.box{{background:#20232a!important}}
input{{background:#16181d;color:#e6e8ec;border-color:#3a3f4b!important}}}}
form,.box{{background:#fff;padding:28px 32px;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.12);width:300px}}
h1{{font-size:20px;margin:0 0 18px}} label{{display:block;margin:12px 0 4px;font-size:13px}}
input{{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #c8ccd4;border-radius:6px;font:inherit}}
button{{margin-top:20px;width:100%;padding:9px;border:0;border-radius:6px;background:#2f6feb;color:#fff;font:inherit;cursor:pointer}}
.err{{color:#c62828;font-size:13px;margin:10px 0 0}} .muted{{color:#778;font-size:13px}}
</style></head><body>{body}</body></html>"""


def login_page(error: str = "", next_url: str = "/", username: str = "") -> bytes:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    body = (f'<form method="post" action="/login"><h1>AgentDesk</h1>'
            f'<label for="u">User name</label><input id="u" name="username" autocomplete="username" autofocus required '
            f'value="{html.escape(username)}">'
            f'<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>'
            f'<input type="hidden" name="next" value="{html.escape(safe_next(next_url))}">{err}'
            f'<button type="submit">Sign in</button></form>')
    return PAGE.format(title="Sign in", body=body).encode("utf-8")


def logout_page(username: str, csrf: str) -> bytes:
    body = (f'<form method="post" action="/logout"><h1>Sign out</h1><p class="muted">Signed in as {html.escape(username)}.</p>'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}"><button type="submit">Sign out</button></form>')
    return PAGE.format(title="Sign out", body=body).encode("utf-8")


def message_page(title: str, text: str) -> bytes:
    return PAGE.format(title=html.escape(title), body=f'<div class="box"><h1>{html.escape(title)}</h1>'
                       f'<p>{html.escape(text)}</p><p><a href="/">Try again</a></p></div>').encode("utf-8")


class Gateway:
    def __init__(self, home: Path, cfg: dict, instances: InstanceManager | None = None, clock=time.time):
        self.home, self.cfg, self.clock = home, cfg, clock
        self.audit = Audit(home / "audit.log")
        self.users = UserStore(home, self.audit)
        self.sessions = SessionTable(float(cfg["session_hours"]) * 3600, float(cfg["session_idle_minutes"]) * 60, clock)
        w = float(cfg["login_window_minutes"]) * 60
        self.login_lock = threading.Lock()  # one password check at a time: parallel guesses cannot all pass the throttle
        self.throttle = Throttle(int(cfg["login_max_failures"]), int(cfg["login_ip_max_failures"]), w,
                                 float(cfg["login_lockout_minutes"]) * 60, clock)
        self.instances = instances or InstanceManager(home, cfg, self.audit)
        self.ssl_ctx: ssl.SSLContext | None = None
        if cfg.get("tls_cert"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cfg["tls_cert"], cfg["tls_key"])
            self.ssl_ctx = ctx
        self.cookie = "__Host-agentdesk_session" if self.ssl_ctx else "agentdesk_session"
        self.max_body = int(cfg["max_body_mb"]) * 1024 * 1024
        self.proxy_timeout = 600.0

    @property
    def scheme(self) -> str:
        return "https" if self.ssl_ctx else "http"

    def current_user(self, sess) -> dict | None:
        """The session's user if it still exists, is enabled, and its password hasn't changed since the login.
        Raises RuntimeError when users.json can't be read (the session is kept: that is no reason to sign out)."""
        u = self.users.get(sess.username)
        if u is None or u["disabled"] or float(u.get("sessions_after") or 0) > sess.created:
            return None
        return u

    def make_server(self) -> ThreadingHTTPServer:
        handler = type("BoundHandler", (Handler,), {"gw": self})
        srv = GatewayServer((self.cfg["host"], int(self.cfg["port"])), handler)
        return srv

    def supervise(self, stop: threading.Event, every: float = 5.0) -> None:
        while not stop.wait(every):
            try:
                self.instances.tick()
                self.sessions.sweep()
            except Exception as e:  # never let supervision die
                self.audit.log("supervisor error", error=repr(e)[:300])


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # a client that went away is not worth a traceback
        import sys
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, ssl.SSLError, socket.timeout, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    gw: Gateway
    timeout = 120  # per socket operation: a stalled client can't hold a thread forever
    server_version = "AgentDesk-Gateway"
    sys_version = ""

    def setup(self):
        if self.gw.ssl_ctx is not None:  # TLS handshake here, in the request's thread, not in the accept loop
            self.request.settimeout(self.timeout)
            self.request = self.gw.ssl_ctx.wrap_socket(self.request, server_side=True)
        super().setup()

    def log_message(self, *_):
        pass

    # -- small helpers
    @property
    def ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def redirect(self, to: str, headers: dict | None = None) -> None:
        self.send(303, b"", "text/plain", {"Location": to, **(headers or {})})

    def cookie_value(self) -> str | None:
        from http.cookies import CookieError, SimpleCookie
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
        except CookieError:
            return None
        m = c.get(self.gw.cookie)
        return m.value if m else None

    def set_cookie(self, value: str, max_age: int) -> str:
        parts = [f"{self.gw.cookie}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Strict"]
        if self.gw.ssl_ctx is not None:
            parts.append("Secure")
        return "; ".join(parts)

    def read_small_body(self, limit: int = 16 * 1024) -> bytes | None:
        n = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or not ((n or "0").isascii() and (n or "0").isdigit()) or int(n or 0) > limit:
            return None
        return self.rfile.read(int(n or 0))

    def form(self) -> dict | None:
        raw = self.read_small_body()
        if raw is None:
            return None
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                d = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return None
            return {k: v for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else None
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def wants_json(self) -> bool:
        return "json" in (self.headers.get("Content-Type") or "") or "application/json" in (self.headers.get("Accept") or "")

    # -- origin / CSRF
    def host_ok(self) -> bool:
        allowed = [h.lower() for h in self.gw.cfg.get("allowed_hosts") or []]
        if not allowed:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return host in allowed or host.rsplit(":", 1)[0] in allowed

    def same_origin(self, origin: str) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and origin.strip().lower().rstrip("/") == f"{self.gw.scheme}://{host}"

    def origin_ok(self, strict: bool) -> bool:
        """Origin (or Referer) must be this site. strict: one of them must be present."""
        origin = self.headers.get("Origin")
        if origin is not None:
            return self.same_origin(origin)
        ref = self.headers.get("Referer")
        if ref:
            u = urllib.parse.urlsplit(ref)
            return self.same_origin(f"{u.scheme}://{u.netloc}")
        return not strict

    def csrf_ok(self, sess, form_token: str | None = None) -> bool:
        """State-changing requests: the session's CSRF token (header or form field), or a same-site Origin/Referer."""
        token = self.headers.get(CSRF_HEADER) or form_token
        if token and hmac.compare_digest(token.encode("utf-8", "replace"), sess.csrf.encode("utf-8")):
            return True
        return self.origin_ok(strict=True)

    # -- dispatch
    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_PATCH(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def do_OPTIONS(self):
        self.route()

    def route(self) -> None:
        if not self.path.startswith("/"):
            return self.send(400, b"bad request", "text/plain")
        if not self.host_ok():
            return self.send(403, b"forbidden host", "text/plain")
        path = urllib.parse.urlsplit(self.path).path
        if path == "/gateway/health":
            return self.send(200, b"ok", "text/plain")
        if path == "/login":
            return self.login_post() if self.command == "POST" else self.send(200, login_page(next_url=self.query("next")))
        sid = self.cookie_value()
        sess = self.gw.sessions.get(sid)
        try:
            user = self.gw.current_user(sess) if sess else None
        except RuntimeError as e:
            return self.store_error(e)
        if sess and user is None:  # disabled, removed or password changed since: that login is over
            self.gw.sessions.drop(sid)
            sess = None
        if path == "/logout":
            return self.logout(sid, sess)
        if sess is None:
            if self.command in ("GET", "HEAD") and not path.startswith("/api/"):
                return self.redirect("/login" + ("" if path == "/" else "?next=" + urllib.parse.quote(self.path)))
            return self.json({"error": "login required", "login": "/login"}, 401)
        if path == "/gateway/me":
            return self.json({k: user[k] for k in ("username", "role", "projects", "accounts")} | {"csrf": sess.csrf})
        if path.startswith("/gateway/"):
            return self.send(404, b"not found", "text/plain")
        if self.command in UNSAFE and not self.csrf_ok(sess):
            self.gw.audit.log("csrf refused", user=sess.username, ip=self.ip, path=path[:200])
            return self.json({"error": "cross-site request refused"}, 403)
        return self.proxy(user)

    def store_error(self, e: Exception) -> None:
        self.gw.audit.log("user store error", ip=self.ip, error=str(e)[:300])
        return self.send(503, message_page("Not available", "The user list can't be read right now; tell the administrator."))

    def query(self, name: str) -> str:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get(name, [""])[0]

    # -- login / logout
    def login_post(self) -> None:
        as_json = self.wants_json()
        if not self.origin_ok(strict=False):
            return self.json({"error": "cross-site request refused"}, 403) if as_json else self.send(403, message_page("Refused", "Cross-site sign-in refused."))
        d = self.form()
        if d is None:
            return self.send(400, b"bad request", "text/plain")
        name = str(d.get("username", "")).strip().lower()[:64]
        password = str(d.get("password", ""))
        nxt = safe_next(d.get("next"))
        with self.gw.login_lock:
            return self._login_checked(d, name, password, nxt, as_json)

    def _login_checked(self, d, name, password, nxt, as_json) -> None:
        wait = self.gw.throttle.blocked_for(name, self.ip)
        if wait > 0:
            self.gw.audit.log("login throttled", user=name, ip=self.ip)
            msg = f"Too many failed sign-ins. Try again in {max(1, int(wait // 60) + 1)} minutes."
            if as_json:
                return self.send(429, json.dumps({"error": msg}).encode(), "application/json", {"Retry-After": str(int(wait) + 1)})
            return self.send(429, login_page(msg, nxt, name), headers={"Retry-After": str(int(wait) + 1)})
        try:
            user = self.gw.users.authenticate(name, password)
        except RuntimeError as e:
            return self.store_error(e)
        if user is None:
            locked = self.gw.throttle.failure(name, self.ip)
            self.gw.audit.log("login failed", user=name, ip=self.ip, **({"locked": True} if locked else {}))
            msg = "Wrong user name or password."
            return self.json({"error": msg}, 401) if as_json else self.send(401, login_page(msg, nxt, name))
        self.gw.throttle.success(name, self.ip)
        self.gw.sessions.drop(self.cookie_value())  # never keep a session id from before the login
        sid, sess = self.gw.sessions.create(user["username"], self.ip)
        self.gw.audit.log("login", user=user["username"], ip=self.ip)
        cookie = self.set_cookie(sid, int(self.gw.sessions.max_age_s))
        if as_json:
            return self.send(200, json.dumps({"ok": True, "username": user["username"], "csrf": sess.csrf}).encode(),
                             "application/json", {"Set-Cookie": cookie})
        return self.redirect(nxt, {"Set-Cookie": cookie})

    def logout(self, sid: str | None, sess) -> None:
        if self.command != "POST":
            if sess is None:
                return self.redirect("/login")
            return self.send(200, logout_page(sess.username, sess.csrf))
        if sess is not None:
            d = self.form() or {}
            if not self.csrf_ok(sess, d.get("csrf")):
                return self.json({"error": "cross-site request refused"}, 403)
            self.gw.sessions.drop(sid)
            self.gw.audit.log("logout", user=sess.username, ip=self.ip)
        clear = self.set_cookie("", 0)
        if self.wants_json():
            return self.send(200, b'{"ok": true}', "application/json", {"Set-Cookie": clear})
        return self.redirect("/login", {"Set-Cookie": clear})

    # -- proxy
    def proxy(self, user: dict) -> None:
        if (self.headers.get("Transfer-Encoding") or "").strip():
            return self.send(411, b"a Content-Length is required", "text/plain")
        n_raw = self.headers.get("Content-Length") or "0"
        if not (n_raw.strip().isascii() and n_raw.strip().isdigit()):
            return self.send(400, b"bad Content-Length", "text/plain")
        n = int(n_raw)
        if n > self.gw.max_body:
            self.close_connection = True
            return self.send(413, b"request body too large", "text/plain")
        mgr, name, home = self.gw.instances, user["username"], Path(user["home"])
        try:
            inst = mgr.acquire(name, home)
        except InstanceError as e:
            return self.send(503, message_page("Not available", str(e)))
        held = True
        try:
            for attempt in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", inst.port, timeout=self.gw.proxy_timeout)
                try:
                    conn.connect()
                    break
                except OSError:  # it died since the last look: start a fresh one once (the body is still unread)
                    conn.close()
                    mgr.release(inst)
                    held = False
                    mgr.mark_dead(inst)
                    if attempt:
                        return self.send(502, message_page("Not available", "Your AgentDesk instance does not answer."))
                    try:
                        inst = mgr.acquire(name, home)
                        held = True
                    except InstanceError as e:
                        return self.send(503, message_page("Not available", str(e)))
            self.forward(conn, inst, name, n)
        finally:
            if held:
                mgr.release(inst)

    def forward(self, conn: http.client.HTTPConnection, inst, username: str, n: int) -> None:
        self.close_connection = True
        try:
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for k, v in self.headers.items():
                if k.lower() not in DROP_REQUEST:
                    conn.putheader(k, v)
            conn.putheader("Host", f"127.0.0.1:{inst.port}")
            if self.headers.get("Origin") is not None:  # already checked above; the instance accepts only its own
                conn.putheader("Origin", f"http://127.0.0.1:{inst.port}")
            conn.putheader(GATEWAY_HEADER, inst.token)
            conn.putheader(USER_HEADER, username)
            conn.putheader("X-Forwarded-For", self.ip)
            conn.putheader("X-Forwarded-Proto", self.gw.scheme)
            if n or self.command in UNSAFE:
                conn.putheader("Content-Length", str(n))
            conn.putheader("Connection", "close")
            conn.endheaders()
            left = n
            while left > 0:
                chunk = self.rfile.read(min(CHUNK, left))
                if not chunk:
                    raise ConnectionError("the client stopped sending")
                conn.send(chunk)
                left -= len(chunk)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            conn.close()
            return self.send(502, message_page("Not available", "Your AgentDesk instance did not answer the request."))
        try:
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in DROP_RESPONSE:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:  # streamed as it arrives (chat output, export zips, ...)
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                inst.last_active = self.gw.clock()
        except (OSError, http.client.HTTPException):
            pass  # the browser went away, or the instance broke off mid-answer
        finally:
            conn.close()
