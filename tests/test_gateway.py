"""Gateway: password hashing, user store, sessions and throttle, login/logout, CSRF, the proxy (bodies, binary,
streaming, headers), isolation between users, and the instance lifecycle (fake and real `agentdesk serve`)."""
import http.client
import json
import sys
import time
from pathlib import Path

import pytest

from agentdesk.gateway import app, cli, config, users
from agentdesk.gateway.instances import GATEWAY_HEADER, InstanceManager
from agentdesk.gateway.sessions import SessionTable, Throttle

FAKE = Path(__file__).resolve().parent / "gateway_fake_instance.py"
PW = "correct horse battery"


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(users, "SCRYPT_N", 2 ** 10)


def fake_command(username, home, port, token_arg):
    return [sys.executable, str(FAKE), "serve", "--host", "127.0.0.1", "--port", str(port), "--data", str(home),
            "--auth-token-file", token_arg]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Client:
    """A browser stand-in: keeps the session cookie, sends a same-site Origin on POST unless told otherwise."""

    def __init__(self, port):
        self.port, self.cookie = port, None

    def req(self, method, path, body=b"", headers=None, origin=True):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if origin and method == "POST":
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        sc = r.getheader("Set-Cookie")
        if sc and sc.startswith("agentdesk_session="):
            val = sc.split(";")[0]
            self.cookie = None if val == "agentdesk_session=" else val
        return r.status, r, data

    def login(self, name, password=PW):
        return self.req("POST", "/login", f"username={name}&password={password.replace(' ', '+')}".encode(),
                        {"Content-Type": "application/x-www-form-urlencoded"})


@pytest.fixture
def gw(tmp_path):
    home = tmp_path / "srv"
    home.mkdir()
    (home / "server.toml").write_text('host = "127.0.0.1"\nport = 0\nidle_stop_minutes = 30\n', encoding="utf-8")
    mgr = InstanceManager(home, config.load(home), command=fake_command)
    g, srv, stop = cli.start(["--home", str(home)], instances=mgr)
    mgr.audit = g.audit
    g.users.add("alice", PW)
    g.users.add("bob", PW, "admin")
    g.port = srv.server_address[1]
    try:
        yield g
    finally:
        stop()


# ----------------------------------------------------------------------------- passwords and users

def test_password_hash_salted_and_verified():
    a, b = users.hash_password("pw-123456"), users.hash_password("pw-123456")
    assert a != b and a.startswith("scrypt$") and "pw-123456" not in a
    assert users.verify_password("pw-123456", a) and users.verify_password("pw-123456", b)
    assert not users.verify_password("pw-123457", a)
    assert not users.verify_password("pw-123456", "garbage") and not users.verify_password("x", "scrypt$1$2$3$!!$!!")


def test_user_store_never_returns_hashes(tmp_path):
    s = users.UserStore(tmp_path)
    u = s.add("carol", "a-long-password", "user", by="test")
    assert u["home"] == str(tmp_path / "users" / "carol") and (tmp_path / "users" / "carol").is_dir()
    assert "password" not in u and all("password" not in x for x in s.list())
    raw = json.loads((tmp_path / "users.json").read_text())
    assert raw["users"]["carol"]["password"].startswith("scrypt$")
    assert "scrypt$" not in (tmp_path / "audit.log").read_text()
    assert s.authenticate("carol", "a-long-password")["username"] == "carol"
    assert s.authenticate("carol", "wrong-password") is None and s.authenticate("nobody", "x") is None
    s.assign("carol", ["proj-a"], ["default"])
    assert s.get("carol")["projects"] == ["proj-a"] and s.get("carol")["accounts"] == ["default"]
    s.set_disabled("carol", True)
    assert s.authenticate("carol", "a-long-password") is None
    for bad in ("Carol", "../x", "a b", "", "x" * 30):
        with pytest.raises(ValueError):
            s.add(bad, "a-long-password")
    with pytest.raises(ValueError):
        s.add("dave", "short")
    with pytest.raises(ValueError):
        s.add("carol", "a-long-password")
    events = [json.loads(line)["event"] for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert events[:3] == ["user added", "assignments changed", "user disabled"]


def test_corrupt_user_store_is_refused_not_reset(tmp_path):
    (tmp_path / "users.json").write_text("{not json")
    with pytest.raises(RuntimeError):
        users.UserStore(tmp_path).list()


# ----------------------------------------------------------------------------- sessions and throttle

def test_session_expiry_idle_and_absolute():
    clk = Clock()
    t = SessionTable(max_age_s=100, idle_s=30, clock=clk)
    sid, s = t.create("alice")
    assert t.get(sid) is s and t.get("made-up") is None and t.get(None) is None
    clk.t += 29
    assert t.get(sid) is s  # used: the idle timer starts again
    clk.t += 31
    assert t.get(sid) is None  # idle too long
    sid, _ = t.create("alice")
    for _ in range(4):
        clk.t += 25
        assert t.get(sid)
    clk.t += 25
    assert t.get(sid) is None  # past the absolute limit although in use
    sid, _ = t.create("bob")
    assert t.drop_user("bob") == 1 and t.get(sid) is None


def test_throttle_per_user_and_ip():
    clk = Clock()
    th = Throttle(max_failures=3, ip_max_failures=10, window_s=60, lockout_s=120, clock=clk)
    for _ in range(2):
        assert not th.failure("alice", "1.1.1.1")
    assert th.blocked_for("alice", "1.1.1.1") == 0
    assert th.failure("alice", "1.1.1.1")
    assert th.blocked_for("alice", "1.1.1.1") > 0
    assert th.blocked_for("alice", "2.2.2.2") == 0 and th.blocked_for("bob", "1.1.1.1") == 0
    clk.t += 121
    assert th.blocked_for("alice", "1.1.1.1") == 0
    th.failure("bob", "3.3.3.3")
    th.failure("bob", "3.3.3.3")
    th.success("bob", "3.3.3.3")
    assert not th.failure("bob", "3.3.3.3")  # the success reset the count
    for i in range(10):  # many names from one address
        th.failure(f"u{i}", "9.9.9.9")
    assert th.blocked_for("anyone", "9.9.9.9") > 0


def test_safe_next():
    assert app.safe_next("/p/x?y=1") == "/p/x?y=1"
    for bad in ("//evil.com", "http://evil.com", "/\\evil.com", "", None, "/a\nb"):
        assert app.safe_next(bad) == "/"


# ----------------------------------------------------------------------------- login, logout, CSRF

def test_login_logout_and_session_cookie(gw):
    c = Client(gw.port)
    st, r, _ = c.req("GET", "/")
    assert st == 303 and r.getheader("Location") == "/login"
    st, r, body = c.req("GET", "/login")
    assert st == 200 and b"<form" in body and b"http" not in body.split(b"<body>")[1].replace(b"http-equiv", b"")
    assert c.req("GET", "/api/overview")[0] == 401
    assert c.req("POST", "/api/p/x/run", b"{}")[0] == 401
    st, _, body = c.login("alice", "wrong-password")
    assert st == 401 and b"Wrong user name or password" in body and c.cookie is None
    st, r, _ = c.login("alice")
    assert st == 303 and r.getheader("Location") == "/"
    flags = r.getheader("Set-Cookie")
    assert "HttpOnly" in flags and "SameSite=Strict" in flags and "Secure" not in flags
    st, _, body = c.req("GET", "/")
    assert st == 200 and body == b"<html>fake index</html>"
    st, _, body = c.req("GET", "/gateway/me")
    me = json.loads(body)
    assert me["username"] == "alice" and me["role"] == "user" and me["csrf"] and "password" not in me
    assert c.req("POST", "/logout", origin=False)[0] == 403  # no Origin, no token
    st, r, _ = c.req("POST", "/logout")
    assert st == 303 and r.getheader("Location") == "/login" and c.cookie is None
    assert c.req("GET", "/")[0] == 303
    events = [json.loads(x)["event"] for x in (gw.home / "audit.log").read_text().splitlines()]
    assert "login failed" in events and "login" in events and "logout" in events
    assert "scrypt$" not in (gw.home / "audit.log").read_text() and PW not in (gw.home / "audit.log").read_text()


def test_old_cookie_useless_after_logout(gw):
    c = Client(gw.port)
    c.login("alice")
    old = c.cookie
    c.req("POST", "/logout")
    c.cookie = old
    assert c.req("GET", "/api/x")[0] == 401


def test_cross_site_login_and_posts_refused(gw):
    c = Client(gw.port)
    st, _, _ = c.req("POST", "/login", f"username=alice&password={PW}".encode(),
                     {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil.example"}, origin=False)
    assert st == 403 and c.cookie is None
    c.login("alice")
    assert c.req("POST", "/echo", b"x", {"Origin": "http://evil.example"}, origin=False)[0] == 403
    assert c.req("POST", "/echo", b"x", {"Referer": "http://evil.example/page"}, origin=False)[0] == 403
    assert c.req("POST", "/echo", b"x", origin=False)[0] == 403
    csrf = json.loads(c.req("GET", "/gateway/me")[2])["csrf"]
    st, _, body = c.req("POST", "/echo", b"x", {"X-AgentDesk-CSRF": csrf}, origin=False)
    assert st == 200 and body == b"x"
    assert c.req("POST", "/echo", b"x", {"X-AgentDesk-CSRF": "wrong"}, origin=False)[0] == 403


def test_login_throttle(gw):
    c = Client(gw.port)
    for _ in range(5):
        assert c.login("alice", "wrong-password")[0] == 401
    st, r, _ = c.login("alice")  # right password, but locked out
    assert st == 429 and int(r.getheader("Retry-After")) > 0 and c.cookie is None
    assert Client(gw.port).login("bob")[0] == 303  # other users are not affected
    assert "login throttled" in (gw.home / "audit.log").read_text()


def test_disabled_user_and_password_change_end_sessions(gw):
    a, b = Client(gw.port), Client(gw.port)
    a.login("alice")
    b.login("bob")
    gw.users.set_disabled("alice", True)
    assert a.req("GET", "/api/x")[0] == 401 and a.login("alice")[0] == 401
    time.sleep(0.01)
    gw.users.set_password("bob", "a-new-password")
    assert b.req("GET", "/api/x")[0] == 401
    assert b.login("bob", "a-new-password")[0] == 303 and b.req("GET", "/whoami")[0] == 200


# ----------------------------------------------------------------------------- proxy and isolation

def test_isolation_between_users(gw):
    a, b = Client(gw.port), Client(gw.port)
    a.login("alice")
    b.login("bob")
    assert a.req("POST", "/file/secret.txt", b"alice's secret")[0] == 200
    assert a.req("GET", "/file/secret.txt")[2] == b"alice's secret"
    assert b.req("GET", "/file/secret.txt")[0] == 404
    assert (gw.home / "users" / "alice" / "secret.txt").exists()
    assert not (gw.home / "users" / "bob" / "secret.txt").exists()
    assert a.req("GET", "/whoami")[2].decode() == str(gw.home / "users" / "alice")
    # Nothing a client sends chooses the instance: forged user headers and a stolen-looking cookie don't help.
    st, _, body = b.req("GET", "/whoami", headers={"X-AgentDesk-User": "alice", GATEWAY_HEADER: "x"})
    assert body.decode() == str(gw.home / "users" / "bob")
    st, _, body = b.req("GET", "/headers", headers={"X-AgentDesk-User": "alice", "X-Forwarded-For": "6.6.6.6"})
    seen = {k.lower(): v for k, v in json.loads(body).items()}
    assert seen["x-agentdesk-user"] == "bob" and seen["x-forwarded-for"] == "127.0.0.1"
    ports = {i["username"]: i["port"] for i in gw.instances.status()}
    assert set(ports) == {"alice", "bob"} and ports["alice"] != ports["bob"]


def test_proxy_headers(gw):
    c = Client(gw.port)
    c.login("alice")
    st, r, body = c.req("POST", "/headers", b"", {"Content-Type": "text/plain"})
    st, r, body = c.req("GET", "/headers")
    seen = {k.lower(): v for k, v in json.loads(body).items()}
    inst = gw.instances.instances["alice"]
    assert "cookie" not in seen  # the gateway's session cookie never reaches the instance
    assert seen[GATEWAY_HEADER.lower()] == inst.token
    assert seen["host"] == f"127.0.0.1:{inst.port}"
    assert r.getheader("Set-Cookie") is None  # the instance can't set cookies on the gateway's site
    st, _, body = c.req("POST", "/echo", b"{}", {"Content-Type": "application/json"})
    assert st == 200


def test_proxy_post_bodies_binary_and_streaming(gw):
    c = Client(gw.port)
    c.login("bob")
    blob = bytes(range(256)) * 4000  # ~1 MB, every byte value
    st, r, body = c.req("POST", "/echo", blob, {"Content-Type": "application/zip"})
    assert st == 200 and body == blob and r.getheader("Content-Type") == "application/zip"
    st, _, body = c.req("POST", "/echo", "привет".encode(), {"Content-Type": "text/plain; charset=utf-8"})
    assert body.decode() == "привет"
    c.req("POST", "/file/up.bin", blob)
    assert c.req("GET", "/file/up.bin")[2] == blob
    st, _, body = c.req("GET", "/stream")  # no Content-Length upstream: passed through to the end
    assert st == 200 and body == b"".join(f"part{i}\n".encode() for i in range(5))
    st, _, _ = c.req("POST", "/echo", b"x", {"Transfer-Encoding": "chunked"})
    assert st in (400, 411)


def test_instance_restarted_after_crash(gw):
    c = Client(gw.port)
    c.login("alice")
    assert "alice" not in gw.instances.instances  # started lazily, on the first proxied request
    c.req("GET", "/whoami")
    first = gw.instances.instances["alice"]
    c.req("GET", "/exit")
    first.proc.wait(10)
    st, _, body = c.req("GET", "/whoami")  # the dead instance is noticed and a new one started
    assert st == 200 and body.decode().endswith("alice")
    assert gw.instances.instances["alice"] is not first


# ----------------------------------------------------------------------------- instance lifecycle

def test_instance_lifecycle_idle_busy_crash(tmp_path):
    clk = Clock()
    cfg = dict(config.DEFAULTS, idle_stop_minutes=1.0)
    m = InstanceManager(tmp_path, cfg, command=fake_command, clock=clk)
    home = tmp_path / "users" / "u1"
    try:
        inst = m.acquire("u1", home)
        assert inst.alive() and (tmp_path / "instances" / "u1.token").read_text() == inst.token
        assert m.ensure("u1", home) is inst  # one instance per user
        clk.t += 120
        m.tick()
        assert inst.alive()  # a request in progress keeps it
        m.release(inst)
        (home / "busy").write_text("1")
        clk.t += 120
        m.tick()
        assert inst.alive() and m.instances["u1"] is inst  # a run in progress keeps it
        (home / "busy").unlink()
        clk.t += 120
        m.tick()
        assert "u1" not in m.instances
        inst.proc.wait(10)
        assert not inst.alive()
        # crash while in use: the supervisor starts it again
        inst2 = m.acquire("u1", home)
        m.release(inst2)
        assert inst2 is not inst
        inst2.proc.kill()
        inst2.proc.wait(10)
        m.tick()
        inst3 = m.instances["u1"]
        assert inst3 is not inst2 and inst3.alive()
    finally:
        m.stop_all()
    assert not m.instances


def test_serve_command_variants(tmp_path):
    m = InstanceManager(tmp_path, dict(config.DEFAULTS))
    argv = m.serve_command("ann", tmp_path / "users" / "ann", 4321, "/tok")
    assert argv[1:4] == ["-m", "agentdesk", "serve"] and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--data") + 1] == str(tmp_path / "users" / "ann") and "--trust-proxy-auth" in argv
    m.cfg = dict(config.DEFAULTS, run_as_users=True)
    assert m.serve_command("ann", tmp_path, 1, "/dev/stdin")[:5] == ["sudo", "-n", "-u", "agentdesk-ann", "--"]
    assert m._token_arg("ann", "t") == "/dev/stdin"
    m.cfg = dict(config.DEFAULTS, run_as_users=True, run_as="systemd-run")
    assert "--uid=agentdesk-ann" in m.serve_command("ann", tmp_path, 1, "/dev/stdin")


def test_config(tmp_path):
    cfg = config.load(tmp_path)  # writes the example on first start
    assert (tmp_path / "server.toml").exists() and cfg["host"] == "0.0.0.0" and cfg["port"] == 8080
    (tmp_path / "server.toml").write_text('tls_cert = "a.pem"\n')
    with pytest.raises(SystemExit):
        config.load(tmp_path)
    (tmp_path / "server.toml").write_text('port = "80"\n')
    with pytest.raises(SystemExit):
        config.load(tmp_path)


def test_user_cli(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("cli-password-1\n"))
    cli.main(["--home", str(tmp_path), "user", "add", "erin", "--admin", "--password-stdin"])
    cli.main(["--home", str(tmp_path), "user", "assign", "erin", "--project", "p1", "--account", "default"])
    cli.main(["--home", str(tmp_path), "user", "list"])
    out = capsys.readouterr().out
    assert "scrypt" not in out and '"role": "admin"' in out and '"projects": ["p1"]' in out
    assert users.UserStore(tmp_path).authenticate("erin", "cli-password-1")
    with pytest.raises(SystemExit):
        cli.main(["--home", str(tmp_path), "user", "disable", "nobody"])


def test_real_instance_through_gateway(tmp_path):
    """The real `python -m agentdesk serve`, started by the gateway, answers with the user's own data folder."""
    home = tmp_path / "srv"
    home.mkdir()
    (home / "server.toml").write_text('host = "127.0.0.1"\nport = 0\n', encoding="utf-8")
    g, srv, stop = cli.start(["--home", str(home)])
    try:
        g.users.add("zed", PW)
        c = Client(srv.server_address[1])
        assert c.login("zed")[0] == 303
        st, _, body = c.req("GET", "/api/overview")
        assert st == 200, body
        assert Path(json.loads(body)["data"]) == (home / "users" / "zed").resolve()
        st, _, body = c.req("GET", "/api/busy")
        assert st == 200 and json.loads(body)["busy"] is False
        st, _, body = c.req("GET", "/")
        assert st == 200 and b"<html" in body.lower()
        st, _, body = c.req("POST", "/api/autorunner", b'{"enabled": false}', {"Content-Type": "application/json"})
        assert st == 200 and json.loads(body)["ok"] is True  # Origin rewritten: the instance accepts the POST
    finally:
        stop()

def test_tls_listener_secure_cookie(tmp_path):
    """tls_cert / tls_key: the gateway speaks HTTPS and the session cookie is Secure (__Host- prefixed)."""
    pytest.importorskip("cryptography")
    import datetime as dt
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    home = tmp_path / "srv"
    home.mkdir()
    (home / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (home / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    (home / "server.toml").write_text(f'host = "127.0.0.1"\nport = 0\ntls_cert = {json.dumps(str(home / "cert.pem"))}\n'
                                      f'tls_key = {json.dumps(str(home / "key.pem"))}\n', encoding="utf-8")
    mgr = InstanceManager(home, config.load(home), command=fake_command)
    g, srv, stop = cli.start(["--home", str(home)], instances=mgr)
    try:
        g.users.add("tina", PW)
        port = srv.server_address[1]
        ctx = ssl.create_default_context(cafile=str(home / "cert.pem"))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
        c.request("POST", "/login", f"username=tina&password={PW.replace(' ', '+')}",
                  {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"https://127.0.0.1:{port}"})
        r = c.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie")
        assert r.status == 303 and cookie.startswith("__Host-agentdesk_session=") and "; Secure" in cookie
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
        c.request("POST", "/echo", b"over tls", {"Cookie": cookie.split(";")[0], "Origin": f"https://127.0.0.1:{port}"})
        r = c.getresponse()
        assert r.status == 200 and r.read() == b"over tls"
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)  # http:// origin is another site
        c.request("POST", "/echo", b"x", {"Cookie": cookie.split(";")[0], "Origin": f"http://127.0.0.1:{port}"})
        assert c.getresponse().status == 403
    finally:
        stop()


def test_item_names_reject_dot_only_and_traversal():
    from agentdesk.gateway import users
    assert users.ITEM.fullmatch("my-proj_1.x") and not users.ITEM.fullmatch("..") and not users.ITEM.fullmatch(".")


def test_new_user_starts_with_sessions_after_set(tmp_path):
    import inspect
    from agentdesk.gateway import users
    assert '"sessions_after": time.time()' in inspect.getsource(users.UserStore.add)
