"""Headless server: Host/Origin rules, gateway token, refusal to bind publicly, a real start."""
import http.client
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from agentdesk import server, serve

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "s3cret-token-0123456789"


def test_default_guard_is_loopback_only():
    g = server.Guard()
    assert g.check("127.0.0.1:8765", None, None, False) is None
    assert g.check("localhost:1", "http://localhost:1", None, True) is None
    assert g.check("evil.example:8765", None, None, False)[0] == 403  # DNS rebinding
    assert g.check("127.0.0.1:8765", "http://evil.example", None, True)[0] == 403
    assert g.check("127.0.0.1:8765", "null", None, True)[0] == 403
    assert g.check("", None, None, False)[0] == 403


def test_lan_host_and_origin_allowed_foreign_rejected():
    g = server.Guard(server.default_allowed("192.168.1.5", 8765, ["agentdesk.lan"]))
    assert g.check("192.168.1.5:8765", "http://192.168.1.5:8765", None, True) is None
    assert g.check("agentdesk.lan", "http://agentdesk.lan:99", None, True) is None
    assert g.check("192.168.1.6:8765", None, None, False)[0] == 403
    assert g.check("192.168.1.5:8765", "http://192.168.1.6:8765", None, True)[0] == 403
    assert g.check("192.168.1.5:9999", None, None, False)[0] == 403  # a different port is a different site


def test_wildcard_bind_allows_only_loopback_by_default():
    assert "0.0.0.0:8765" not in server.default_allowed("0.0.0.0", 8765)
    assert server.Guard(server.default_allowed("0.0.0.0", 8765)).check("10.0.0.1:8765", None, None, False)[0] == 403


def test_token_rules():
    g = server.Guard(None, TOKEN)
    assert g.check("127.0.0.1:1", None, None, False)[0] == 401
    assert g.check("127.0.0.1:1", None, "wrong", False)[0] == 401
    assert g.check("127.0.0.1:1", None, TOKEN, False) is None
    assert g.check("evil:1", None, TOKEN, False)[0] == 403


def test_is_loopback():
    assert all(server.is_loopback(h) for h in ("127.0.0.1", "localhost", "::1", "127.0.0.5"))
    assert not any(server.is_loopback(h) for h in ("0.0.0.0", "192.168.1.5", "::", "example.com"))


def test_refuses_public_bind_without_token(capsys):
    with pytest.raises(SystemExit) as e:
        serve.start(["--host", "0.0.0.0", "--port", "0"])
    assert "refusing" in str(e.value) and "--auth-token-file" in str(e.value)
    with pytest.raises(SystemExit):
        serve.start(["--trust-proxy-auth", "--port", "0"])


def test_short_token_file_rejected(tmp_path):
    f = tmp_path / "t"
    f.write_text("short")
    with pytest.raises(SystemExit):
        serve.start(["--host", "0.0.0.0", "--port", "0", "--auth-token-file", str(f)])


def test_in_process_start_with_token(tmp_path, monkeypatch):
    f = tmp_path / "t"
    f.write_text(TOKEN + "\n")
    monkeypatch.setattr(server, "GUARD", server.GUARD)
    srv, stop = serve.start(["--port", "0", "--auth-token-file", str(f), "--pid-file", str(tmp_path / "pid")])
    try:
        port = srv.server_address[1]
        assert (tmp_path / "pid").exists()
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/api/version")
        assert c.getresponse().status == 401
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/api/version", headers={server.GATEWAY_HEADER: TOKEN})
        r = c.getresponse()
        assert r.status == 200 and "version" in json.loads(r.read())
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/api/version", headers={server.GATEWAY_HEADER: TOKEN, "Host": "evil.example"})
        assert c.getresponse().status == 403
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("POST", "/api/nothing", b"{}", {server.GATEWAY_HEADER: TOKEN, "Origin": "http://evil.example"})
        assert c.getresponse().status == 403
    finally:
        stop()
        server.GUARD = server.Guard()
    assert not (tmp_path / "pid").exists()


def test_serve_subprocess_port_zero_no_gui(tmp_path):
    code = ("import sys; sys.argv=['agentdesk','serve','--port','0','--data',%r]; import runpy; "
            "import threading, agentdesk.serve as s; srv, stop = s.start(sys.argv[2:]); "
            "assert not any(m in sys.modules for m in ('webview','pystray')), 'GUI imported'; stop()" % str(tmp_path / "d"))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "listening on http://127.0.0.1:" in r.stdout
    assert (tmp_path / "d").is_dir()  # --data became AGENTDESK_HOME


def test_python_m_agentdesk_serve_answers(tmp_path):
    p = subprocess.Popen([sys.executable, "-m", "agentdesk", "serve", "--port", "0", "--data", str(tmp_path / "d")],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = p.stdout.readline()
        assert "listening on" in line, line
        url = line.split("listening on ")[1].split()[0]
        with urllib.request.urlopen(url + "/api/version", timeout=10) as r:
            assert r.status == 200
    finally:
        p.terminate()
        p.wait(timeout=20)
