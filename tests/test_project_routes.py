"""Dashboard routes for project settings and delete, through the HTTP handler."""

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agentdesk import core, server


@pytest.fixture
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(path, body=None):
        headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{srv.server_port}"}
        if body is not None:
            headers["Origin"] = base
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST", headers=headers)
        try:
            r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r = e
        return r.status, json.loads(r.read())
    yield call
    srv.shutdown()


@pytest.fixture(autouse=True)
def not_desktop(monkeypatch):
    monkeypatch.setattr(server, "_WINDOW", None)


def test_get_settings(http, project):
    code, r = http(f"/api/p/{project.slug}/settings")
    assert code == 200 and r["slug"] == project.slug and r["desktop"] is False and "toml" in r


def test_post_settings_valid_and_invalid(http, project):
    code, r = http(f"/api/p/{project.slug}/settings", {"name": "Renamed Thing", "quick": ["echo hi"], "timeout_s": 120})
    assert r["ok"] is True
    _, v = http(f"/api/p/{project.slug}/settings")
    assert v["name"] == "Renamed Thing" and v["quick"] == ["echo hi"] and v["timeout_s"] == 120
    _, r = http(f"/api/p/{project.slug}/settings", {"name": "   "})
    assert r["ok"] is False and r["field"] == "name"


def test_post_raw_toml_and_repo_change_refused(http, project):
    _, v = http(f"/api/p/{project.slug}/settings")
    text = v["toml"].replace(f'name = "{project.name}"', 'name = "Via Toml"')
    _, r = http(f"/api/p/{project.slug}/settings", {"toml": text})
    assert r["ok"] is True and core.project(project.slug).name == "Via Toml"
    cur = str(project.cfg["repo"]["path"])
    changed = text.replace(cur, cur + "-other")
    assert changed != text
    _, r = http(f"/api/p/{project.slug}/settings", {"toml": changed})
    assert r["ok"] is False and r["field"] == "repo"


def test_settings_refused_while_running(http, project, monkeypatch):
    monkeypatch.setattr(server.Runner, "running", lambda self: True)
    _, r = http(f"/api/p/{project.slug}/settings", {"name": "Nope"})
    assert r["ok"] is False and "message" in r


def test_delete_refused_without_confirm_wrong_name_or_running(http, project, monkeypatch):
    path = f"/api/p/{project.slug}/delete"
    code, r = http(path, {"name": project.slug})
    assert code == 400 and not r["ok"]
    code, r = http(path, {"confirm": True, "name": "other"})
    assert code == 400 and not r["ok"]
    monkeypatch.setattr(server.Runner, "running", lambda self: True)
    code, r = http(path, {"confirm": True, "name": project.slug})
    assert r["ok"] is False and r["busy"] is True
    assert project.dir.exists()


def test_delete_project(http, project):
    code, r = http(f"/api/p/{project.slug}/delete", {"confirm": True, "name": project.slug})
    assert code == 200 and r["ok"] is True and r["repo_deleted"] is False
    assert not project.dir.exists() and project.repo.exists()
    code, _ = http(f"/api/p/{project.slug}/state")
    assert code == 404
    assert project.slug not in server.CHATS and project.slug not in server.RUNNERS


def test_delete_repo_only_under_data_repos(http, project):
    code, r = http(f"/api/p/{project.slug}/delete", {"confirm": True, "name": project.slug, "delete_repo": True})
    assert r["ok"] is True and r["repo_deleted"] is False and project.repo.exists()  # outside <DATA>/repos
    p = core.create_new_project("Routes Inside Repo", "main", "none", "default")
    repo = p.repo
    assert repo.parent == core.DATA / "repos"
    code, r = http(f"/api/p/{p.slug}/delete", {"confirm": True, "name": p.slug, "delete_repo": True})
    assert r["ok"] is True and r["repo_deleted"] is True and not repo.exists() and not p.dir.exists()
