"""New project (server): a fresh repo made by AgentDesk (git init + first commit), optionally a GitHub repository
created with gh, and the server refusing client-chosen folder paths."""

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from agentdesk import core, server

TOKEN = "ghp_" + "T0kenT0kenT0kenT0kenT0ken12"


def g(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    (tmp_path / "nogc").write_text("", encoding="utf-8")
    for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.setenv(k, str(tmp_path / "nogc"))
    core._gh_forget()
    yield
    core._gh_forget()


@pytest.fixture
def fake_gh(monkeypatch):
    """gh is never run: `repo create` answers what the test sets, the token comes from a fake `gh auth token`."""
    calls = {"gh": [], "push": [], "create": (0, "https://github.com/alice/x")}

    def _gh(args, cwd=None, token=None, host=None, timeout=30, raw=False):
        calls["gh"].append({"args": list(args), "token": token})
        if args[:2] == ["repo", "create"]:
            return calls["create"]
        return 0, ""

    def push(p, branch=None):
        calls["push"].append(branch)
        return calls.get("push_result", {"ok": True, "message": "pushed"})

    monkeypatch.setattr(core, "_gh", _gh)
    monkeypatch.setattr(core, "gh_token", lambda login, host="github.com": (TOKEN, None))
    monkeypatch.setattr(core, "git_push", push)
    return calls


def test_new_project_is_a_fresh_repo_with_a_first_commit():
    p = core.create_new_project("Brand New App", "trunk", "none", "default")
    repo = p.repo
    assert repo.parent == core.DATA / "repos" and repo.name == "Brand-New-App"
    assert g(repo, "symbolic-ref", "--short", "HEAD") == "trunk" and p.cfg["repo"]["base"] == "trunk"
    assert g(repo, "rev-list", "--count", "HEAD") == "1"
    assert set(g(repo, "ls-files").split()) == {".gitignore", "README.md"}
    assert g(repo, "remote") == ""  # local only: nothing pushed, no remote
    assert core.repo_state(str(repo)) == "ready"
    with pytest.raises(ValueError, match="exists"):  # the name is taken now
        core.create_new_project("Brand New App")


def test_new_project_bad_branch_leaves_nothing():
    before = sorted(x.name for x in (core.DATA / "repos").iterdir()) if (core.DATA / "repos").exists() else []
    for bad in ("-x", "a b", "a..b"):
        with pytest.raises(ValueError, match="branch"):
            core.create_new_project("Bad Branch", bad)
    after = sorted(x.name for x in (core.DATA / "repos").iterdir()) if (core.DATA / "repos").exists() else []
    assert before == after
    assert core.create_new_project("Bad Branch", "").cfg["repo"]["base"] == "main"  # empty: main


@pytest.mark.parametrize("raw,want", [("My App", "My-App"), ("Проект", "project"), ("a..b c", "a..b-c"), ("..x..", "x")])
def test_gh_repo_name(raw, want):
    assert core.gh_repo_name(raw) == want


def test_github_repo_created_origin_set_and_pushed(fake_gh):
    p = core.create_new_project("Gh Ok", "main")
    r = core.gh_create_repo(p, "alice", private=True)
    assert r["ok"] and r["created"] and r["pushed"] and r["repo"] == "alice/Gh-Ok"
    create = next(c for c in fake_gh["gh"] if c["args"][:2] == ["repo", "create"])
    assert create["args"] == ["repo", "create", "alice/Gh-Ok", "--private"]
    assert create["token"] == TOKEN  # through the environment only
    assert all(TOKEN not in " ".join(c["args"]) for c in fake_gh["gh"]) and TOKEN not in json.dumps(r)
    assert g(p.repo, "remote", "get-url", "origin") == "https://github.com/alice/Gh-Ok.git"
    assert fake_gh["push"] == ["main"]
    assert core.git_account(core.project(p.slug)) == "alice"
    core.gh_create_repo(core.create_new_project("Gh Pub"), "alice")
    assert [c["args"][-1] for c in fake_gh["gh"] if c["args"][:2] == ["repo", "create"]][-1] == "--public"  # the default


def test_github_create_failure_keeps_the_local_project(fake_gh):
    fake_gh["create"] = (1, f"HTTP 422: name already exists on this account {TOKEN}")
    p = core.create_new_project("Gh Fail", "main")
    r = core.gh_create_repo(p, "alice")
    assert not r["ok"] and not r["created"] and "already exists" in r["message"] and TOKEN not in r["message"]
    assert g(p.repo, "remote") == "" and fake_gh["push"] == []
    assert core.project(p.slug).repo == p.repo and core.repo_state(str(p.repo)) == "ready"


def test_github_push_failure_is_reported_and_origin_kept(fake_gh):
    fake_gh["push_result"] = {"ok": False, "message": "remote rejected"}
    p = core.create_new_project("Gh Push Fail")
    r = core.gh_create_repo(p, "alice")
    assert r["created"] and not r["pushed"] and not r["ok"] and "remote rejected" in r["message"]
    assert g(p.repo, "remote", "get-url", "origin") == "https://github.com/alice/Gh-Push-Fail.git"


def test_github_without_login_is_a_message(monkeypatch):
    monkeypatch.setattr(core, "gh_token", lambda login, host="github.com": (None, "gh has no login for bob"))
    p = core.create_new_project("Gh No Login")
    r = core.gh_create_repo(p, "bob")
    assert not r["ok"] and "no login" in r["message"] and g(p.repo, "remote") == ""


@pytest.fixture
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST", headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r = e
        return json.loads(r.read())
    yield call
    srv.shutdown()


def test_server_rejects_a_client_folder_path(http, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_WINDOW", None)  # the server / a browser
    repo = tmp_path / "mine"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    g(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "s")
    r = http("/api/projects", {"name": "Sneaky", "repo": str(repo), "base": "main"})
    assert not r.get("ok") and "automatically" in r["error"] and not (core.DATA / "projects" / "sneaky").exists()
    r = http("/api/projects", {"name": "Sneaky", "repo_url": "https://example.invalid/x.git", "repo": str(tmp_path / "into")})
    assert not r.get("ok") and not (tmp_path / "into").exists()  # nor a folder to clone into
    r = http("/api/projects", {"name": "Sneaky", "base": "main"})
    assert not r.get("ok") and not (core.DATA / "projects" / "sneaky").exists()
    monkeypatch.setattr(server, "_WINDOW", object())  # the desktop window: a local folder is fine
    r = http("/api/projects", {"name": "Desk Folder", "repo": str(repo), "base": "main"})
    assert r["ok"] and Path(r["repo"]) == repo


def test_server_new_project_route(http, monkeypatch, fake_gh):
    monkeypatch.setattr(server, "_WINDOW", None)
    r = http("/api/projects", {"new": True, "name": "Http New", "base": "dev", "preset": "none", "account": "default",
                               "repo": "/etc"})  # a path is ignored: the server chooses the folder
    assert r["ok"] and Path(r["repo"]).parent == core.DATA / "repos" and r["base"] == "dev" and "github" not in r
    assert g(r["repo"], "symbolic-ref", "--short", "HEAD") == "dev"
    assert http("/api/projects", {"new": True, "name": "Http New"})["field"] == "name"
    assert http("/api/projects", {"new": True, "name": "Http Bad", "base": "a b"})["field"] == "base"
    assert http("/api/projects", {"new": True, "name": "Http Gh", "github": {"account": "-x"}})["field"] == "gh"
    fake_gh["create"] = (1, "HTTP 401: Bad credentials")
    r = http("/api/projects", {"new": True, "name": "Http Gh", "github": {"account": "alice", "private": True}})
    assert r["ok"] and r["github"]["ok"] is False and "Bad credentials" in r["github"]["message"]
    assert core.project(r["slug"]).repo == Path(r["repo"])  # usable locally
    fake_gh["create"] = (0, "https://github.com/alice/Http-Gh2")
    r = http("/api/projects", {"new": True, "name": "Http Gh2", "github": {"account": "alice"}})
    assert r["github"]["ok"] and r["github"]["repo"] == "alice/Http-Gh2"
    assert [c["args"][-1] for c in fake_gh["gh"] if c["args"][:2] == ["repo", "create"]] == ["--private", "--public"]


def test_gh_accounts_route(http, monkeypatch):
    monkeypatch.setattr(core, "gh_accounts", lambda refresh=False: {"ok": True, "gh": True, "message": "", "accounts": [
        {"host": "github.com", "login": "alice", "active": True, "ok": True, "protocol": "https", "error": ""},
        {"host": "github.com", "login": "stale", "active": False, "ok": False, "protocol": "https", "error": "expired"},
        {"host": "ghe.corp", "login": "corp", "active": True, "ok": True, "protocol": "https", "error": ""}]})
    assert [a["login"] for a in http("/api/gh-accounts")["accounts"]] == ["alice"]


def test_page_new_project_flow_on_the_server():
    """Source check only (the JS isn't run by pytest): server = no native window, so no folder pickers there."""
    html = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")
    assert 'id="pTab_new"' in html and "Import existing repo" in html and "Create a GitHub repository" in html
    assert '$("pTab_dir").style.display = desk ? "" : "none"' in html and '$("pAdv").style.display = desk ? "" : "none"' in html
    assert '$("xModeGit").style.display = ov?.desktop ? "" : "none"' in html
    assert "ov?.desktop ? {repo: $(\"pClone\").value.trim()} : {}" in html  # the clone folder is never sent from a browser
    assert "r.available ? (r.message" in html  # a failing Browse in the desktop window shows the real error
