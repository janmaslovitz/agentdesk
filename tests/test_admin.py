"""Admin cabinet: every /admin route refuses a normal user (403), and the user / account / project / instance
operations work for an admin; /account password change; the gateway bar is injected for the right people."""
import json
import subprocess
import tomllib

import pytest

from agentdesk import core
from agentdesk.gateway import admin as admin_mod
from agentdesk.gateway import app as gwapp
from test_gateway import PW, Client, cheap_scrypt, fake_command, gw  # noqa: F401  (fixtures)


def login(gw, name):
    c = Client(gw.port)
    assert c.login(name)[0] == 303
    return c


def call(c, method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body if body is not None else {}).encode() if method == "POST" else b"")
    st, _r, out = c.req(method, path, data, {"Content-Type": ctype} if method == "POST" else None)
    try:
        return st, json.loads(out)
    except ValueError:
        return st, out


ADMIN_ROUTES = [
    ("GET", "/admin"), ("GET", "/admin/"), ("GET", "/admin/anything"),
    ("GET", "/admin/api/users"), ("POST", "/admin/api/users"),
    ("POST", "/admin/api/users/alice/password"), ("POST", "/admin/api/users/alice/disable"),
    ("POST", "/admin/api/users/alice/enable"), ("POST", "/admin/api/users/alice/delete"),
    ("POST", "/admin/api/users/alice/role"), ("POST", "/admin/api/users/alice/accounts"),
    ("GET", "/admin/api/users/alice/projects"), ("POST", "/admin/api/users/alice/projects/git"),
    ("POST", "/admin/api/users/alice/projects/zip"), ("POST", "/admin/api/users/alice/projects/x/remove"),
    ("GET", "/admin/api/accounts"), ("POST", "/admin/api/accounts"), ("POST", "/admin/api/accounts/a/delete"),
    ("GET", "/admin/api/instances"), ("POST", "/admin/api/instances/alice/start"),
    ("POST", "/admin/api/instances/alice/stop"), ("POST", "/admin/api/instances/alice/restart"),
    ("GET", "/admin/api/instances/alice/log"), ("GET", "/admin/api/health"), ("GET", "/admin/api/audit"),
    ("GET", "/admin/api/nothing"),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_need_admin(gw, method, path):
    anon = Client(gw.port)
    st, _r, _ = anon.req(method, path, b"{}" if method == "POST" else b"")
    assert st in (303, 401)  # not signed in: never served
    alice = login(gw, "alice")
    st, _ = call(alice, method, path, {} if method == "POST" else None)
    assert st == 403
    assert gw.users.get("alice") is not None and gw.users.count() == 2  # nothing happened


def test_admin_page_and_json_for_admin(gw):
    bob = login(gw, "bob")
    st, _r, out = bob.req("GET", "/admin")
    assert st == 200 and b"AgentDesk Admin" in out and bob.cookie.split("=")[1].encode() not in out
    st, j = call(bob, "GET", "/admin/api/users")
    assert st == 200 and {u["username"] for u in j["users"]} == {"alice", "bob"}
    assert "password" not in json.dumps(j) and "scrypt" not in json.dumps(j)


def test_user_lifecycle(gw):
    bob = login(gw, "bob")
    assert call(bob, "POST", "/admin/api/users", {"username": "carol", "role": "user", "password": "tiny"})[0] == 400
    assert call(bob, "POST", "/admin/api/users", {"username": "carol", "role": "user", "password": "carol-secret-1"})[0] == 201
    assert call(bob, "POST", "/admin/api/users", {"username": "carol", "password": "carol-secret-1"})[0] == 400  # exists
    carol = Client(gw.port)
    assert carol.login("carol", "carol-secret-1")[0] == 303
    # reset password ends carol's session and the old password stops working
    assert call(bob, "POST", "/admin/api/users/carol/password", {"password": "new-secret-22"})[0] == 200
    assert carol.req("GET", "/gateway/me")[0] in (303, 401)
    assert Client(gw.port).login("carol", "carol-secret-1")[0] == 401
    assert Client(gw.port).login("carol", "new-secret-22")[0] == 303
    # disable / enable
    assert call(bob, "POST", "/admin/api/users/carol/disable")[0] == 200
    assert Client(gw.port).login("carol", "new-secret-22")[0] == 401
    assert call(bob, "POST", "/admin/api/users/carol/enable")[0] == 200
    assert Client(gw.port).login("carol", "new-secret-22")[0] == 303
    # delete needs the name typed; the data folder stays unless asked
    home = gw.users.user_home("carol")
    (home / "keep.txt").write_text("x")
    assert call(bob, "POST", "/admin/api/users/carol/delete", {"confirm": "nope"})[0] == 400
    assert call(bob, "POST", "/admin/api/users/carol/delete", {"confirm": "carol"})[0] == 200
    assert gw.users.get("carol") is None and (home / "keep.txt").exists()
    gw.users.add("dave", PW)
    (gw.users.user_home("dave") / "f.txt").write_text("x")
    assert call(bob, "POST", "/admin/api/users/dave/delete", {"confirm": "dave", "delete_data": "1"})[0] == 200
    assert not gw.users.user_home("dave").exists()
    events = [json.loads(line)["event"] for line in gw.audit.path.read_text().splitlines()]
    assert "user removed" in events and "password changed" in events


def test_admin_cannot_lock_self_out(gw):
    bob = login(gw, "bob")
    for action, body in (("disable", {}), ("delete", {"confirm": "bob"}), ("role", {"role": "user"})):
        assert call(bob, "POST", f"/admin/api/users/bob/{action}", body)[0] == 400
    assert gw.users.get("bob")["role"] == "admin" and not gw.users.get("bob")["disabled"]
    gw.users.set_role("alice", "admin")
    assert call(bob, "POST", "/admin/api/users/alice/role", {"role": "user"})[0] == 200
    assert call(bob, "POST", "/admin/api/users/nobody/disable")[0] == 404
    assert call(bob, "POST", "/admin/api/users/..%2Fx/disable")[0] == 404


def test_csrf_still_applies_to_admin_posts(gw):
    bob = login(gw, "bob")
    st, _r, _ = bob.req("POST", "/admin/api/users", b"{}", {"Origin": "http://evil.example"}, origin=False)
    assert st == 403


def test_accounts_are_assigned_without_showing_keys(gw, monkeypatch):
    monkeypatch.setenv("TEAM_KEY_VAR", "sk-ant-SECRETVALUE1234567890")
    bob = login(gw, "bob")
    assert call(bob, "POST", "/admin/api/accounts", {"name": "team", "kind": "api_key", "key_env": "sk-ant-abc def"})[0] == 400
    assert call(bob, "POST", "/admin/api/accounts", {"name": "team", "kind": "api_key", "key_env": "TEAM_KEY_VAR"})[0] == 200
    assert call(bob, "POST", "/admin/api/accounts", {"name": "login1", "kind": "subscription", "config_dir": "/tmp/cc1"})[0] == 200
    st, j = call(bob, "GET", "/admin/api/accounts")
    assert {a["name"] for a in j["accounts"]} == {"team", "login1"} and "SECRETVALUE" not in json.dumps(j)
    assert call(bob, "POST", "/admin/api/users/alice/accounts", {"accounts": "team,missing"})[0] == 400
    assert call(bob, "POST", "/admin/api/users/alice/accounts", {"accounts": "team,login1"})[0] == 200
    assert gw.users.get("alice")["accounts"] == ["login1", "team"]
    toml = (gw.users.user_home("alice") / "settings.toml").read_text()
    assert 'key_env = "TEAM_KEY_VAR"' in toml and "SECRETVALUE" not in toml
    assert [a["name"] for a in tomllib.loads(toml)["accounts"]] == ["login1", "team"]
    # a user sees only names
    alice = login(gw, "alice")
    me = call(alice, "GET", "/gateway/me")[1]
    assert me["accounts"] == ["login1", "team"] and "key" not in json.dumps(me).lower()
    # deleting an account takes it from the user and from the user's settings
    assert call(bob, "POST", "/admin/api/accounts/team/delete")[0] == 200
    assert gw.users.get("alice")["accounts"] == ["login1"]
    assert "team" not in (gw.users.user_home("alice") / "settings.toml").read_text()
    assert call(bob, "POST", "/admin/api/users/alice/accounts", {"accounts": ""})[0] == 200
    assert "accounts" not in (gw.users.user_home("alice") / "settings.toml").read_text()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "a.txt").write_text("hello")
    for cmd in (["init", "-b", "main"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "first"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    return repo


def test_projects_from_git_and_zip(gw, source_repo, project):
    bob = login(gw, "bob")
    base = "/admin/api/users/alice/projects"
    assert call(bob, "GET", base) == (200, {"ok": True, "projects": []})
    assert call(bob, "POST", base + "/git", {"url": "--upload-pack=x"})[0] == 400
    st, j = call(bob, "POST", base + "/git", {"url": str(source_repo), "name": "Cloned One"})
    assert st == 201 and j["slug"] == "cloned-one", j
    home = gw.users.user_home("alice")
    assert (home / "projects" / "cloned-one" / "project.toml").exists() and (home / "repos" / "cloned-one" / "a.txt").exists()
    assert call(bob, "POST", base + "/git", {"url": str(source_repo), "name": "Cloned One"})[0] == 400  # exists
    st, j = call(bob, "POST", base + "/zip?name=From+Zip", raw=core.export_project(project), ctype="application/zip")
    assert st == 201 and j["slug"] == "from-zip", j
    j = call(bob, "GET", base)[1]
    assert {p["slug"] for p in j["projects"]} == {"cloned-one", "from-zip"}
    assert all(p["size"] > 0 for p in j["projects"])
    assert call(bob, "POST", base + "/zip", raw=b"not a zip", ctype="application/zip")[0] == 400
    assert not (gw.users.user_home("bob") / "projects").exists()  # other users' folders are untouched
    # remove: the project goes; the cloned repo only when asked
    assert call(bob, "POST", base + "/cloned-one/remove", {})[0] == 200
    assert not (home / "projects" / "cloned-one").exists() and (home / "repos" / "cloned-one").exists()
    assert call(bob, "POST", base + "/from-zip/remove", {"delete_repo": "1"})[0] == 200
    assert not (home / "repos" / "from-zip").exists()
    assert call(bob, "POST", base + "/ghost/remove", {})[0] == 400
    assert call(bob, "GET", base)[1]["projects"] == []


def test_instances_start_stop_log_health_audit(gw):
    bob = login(gw, "bob")
    st, j = call(bob, "GET", "/admin/api/instances")
    assert {i["username"]: i["state"] for i in j["instances"]} == {"alice": "stopped", "bob": "stopped"}
    assert call(bob, "POST", "/admin/api/instances/alice/start")[0] == 200
    row = {i["username"]: i for i in call(bob, "GET", "/admin/api/instances")[1]["instances"]}["alice"]
    assert row["state"] in ("idle", "running") and row["port"] and row["uptime"] is not None
    assert call(bob, "POST", "/admin/api/instances/alice/restart")[0] == 200
    assert "fake instance" in call(bob, "GET", "/admin/api/instances/alice/log?lines=5")[1]["log"]
    assert call(bob, "POST", "/admin/api/instances/alice/stop")[0] == 200
    assert {i["username"]: i["state"] for i in call(bob, "GET", "/admin/api/instances")[1]["instances"]}["alice"] == "stopped"
    gw.users.set_disabled("alice", True)
    assert call(bob, "POST", "/admin/api/instances/alice/start")[0] == 400
    h = call(bob, "GET", "/admin/api/health")[1]
    assert h["disk_free"] > 0 and h["version"]
    ev = call(bob, "GET", "/admin/api/audit?n=3")[1]["events"]
    assert 0 < len(ev) <= 3 and ev[0]["event"]
    assert "password" not in json.dumps(ev)


def test_log_redaction():
    out = admin_mod.redact("key sk-ant-abc123 tok XYZ", "XYZ")
    assert "sk-ant-abc123" not in out and "XYZ" not in out


def test_account_page_password_change(gw):
    alice = login(gw, "alice")
    st, _r, out = alice.req("GET", "/account")
    assert st == 200 and b"Change password" in out
    assert call(alice, "POST", "/account/password", {"current": "wrong-password", "new": "another-pass-1"})[0] == 403
    assert call(alice, "POST", "/account/password", {"current": PW, "new": "short"})[0] == 400
    assert call(alice, "POST", "/account/password", {"current": PW, "new": "another-pass-1"})[0] == 200
    assert alice.req("GET", "/gateway/me")[0] in (303, 401)
    assert Client(gw.port).login("alice", PW)[0] == 401
    assert Client(gw.port).login("alice", "another-pass-1")[0] == 303
    assert Client(gw.port).req("GET", "/account")[0] == 303  # not signed in


def test_gateway_bar_injected_for_html_only():
    page = b"<html><body class='x'><p>hi</p></body></html>"
    admin_page = gwapp.inject_bar(page, "bob", True)
    user_page = gwapp.inject_bar(page, "al<ice", False)
    assert b'href="/admin"' in admin_page and b'href="/admin"' not in user_page
    assert b"al&lt;ice" in user_page and admin_page.index(b"agentdesk-gateway-bar") > admin_page.index(b"<body")
    assert gwapp.inject_bar(b"no body here", "bob", True) == b"no body here"


def test_bar_in_proxied_dashboard_page(gw):
    for name, has_admin in (("alice", False), ("bob", True)):
        c = login(gw, name)
        st, r, out = c.req("GET", "/page")
        assert st == 200 and b"fake page" in out and b"agentdesk-gateway-bar" in out
        assert (b'href="/admin"' in out) is has_admin
        assert int(r.getheader("Content-Length")) == len(out)
