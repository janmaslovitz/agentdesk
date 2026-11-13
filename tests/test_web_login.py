"""Signing an account in from the dashboard with no terminal: `claude auth login` in the background, its link shown,
the code from the sign-in page handed to it."""

import json
import sys
import textwrap

import pytest

from agentdesk import core, server

FAKE_CLI = textwrap.dedent("""\
    import sys
    assert sys.argv[1:3] == ["auth", "login"], sys.argv
    print("Opening browser to sign in…")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc", flush=True)
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good#abc":
        print("Login successful."); sys.exit(0)
    print("Login failed: Request failed with status code 400"); sys.exit(1)
""")


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "claude_fake.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    launcher = tmp_path / "claude"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(core, "bundled_cli", lambda: str(launcher))
    monkeypatch.setattr(server, "auth_status", lambda a, fresh=False: {"logged_in": True})
    yield
    for name in list(server.LOGINS):
        server.cancel_login(name)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a shell launcher")
def test_sign_in_with_the_code(fake_cli):
    r = server.start_login("default")
    assert r["ok"] and r["url"].startswith("https://claude.com/cai/oauth/authorize?")
    done = server.finish_login("default", "good#abc")
    assert done["ok"] and "successful" in done["message"] and done["status"] == {"logged_in": True}
    assert "default" not in server.LOGINS


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a shell launcher")
def test_wrong_code_and_no_flow(fake_cli):
    assert server.start_login("default")["ok"]
    bad = server.finish_login("default", "nope")
    assert bad["ok"] is False and "400" in bad["message"]
    assert "no sign-in is waiting" in server.finish_login("default", "good#abc")["error"]
    assert server.start_login("default")["ok"]
    assert "paste the code" in server.finish_login("default", "  ")["error"]
    assert server.cancel_login("default") == {"ok": True} and "default" not in server.LOGINS


def test_api_key_accounts_dont_sign_in(monkeypatch):
    monkeypatch.setattr(core, "account", lambda name: {"name": name, "kind": "api_key", "key_env": "X"})
    assert "API-key accounts" in server.start_login("k")["error"]
