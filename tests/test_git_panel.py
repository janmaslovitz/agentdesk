import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agentdesk import core, server


def g(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text)
    g(repo, "add", name)
    g(repo, "commit", "-q", "-m", f"edit {name}")


@pytest.fixture(autouse=True)
def ident(monkeypatch, tmp_path):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "nogc"), "GIT_CONFIG_SYSTEM": str(tmp_path / "nogc")}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def remote(project, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    g(project.repo, "remote", "add", "origin", str(bare))
    g(project.repo, "push", "-q", "-u", "origin", "main")
    return bare


def test_local_only(project):
    st = core.git_status(project)
    assert st["branch"] == "main" and st["clean"] and st["remote"] is None and st["upstream"] is None
    assert "local-only" in core.git_pull(project)["message"]
    assert "local-only" in core.git_push(project)["message"]
    assert not core.git_fetch(project)["ok"]


def test_status_dirty_ahead_autos(project, remote):
    commit(project.repo, "a.txt", "1")
    (project.repo / "u.txt").write_text("x")
    st = core.git_status(project)
    assert st["ahead"] == 1 and st["behind"] == 0 and not st["clean"] and st["remote_url"] == str(remote)
    g(project.repo, "checkout", "-q", "-b", "auto/002")
    commit(project.repo, "b.txt", "2")
    g(project.repo, "checkout", "-q", "main")
    g(project.repo, "branch", "auto/001")
    assert core.git_status(project)["autos"] == [{"branch": "auto/002", "ahead": 1}]


def test_push_pull_fetch(project, remote, tmp_path):
    commit(project.repo, "a.txt", "1")
    assert core.git_push(project)["ok"]
    assert g(remote, "rev-parse", "main") == g(project.repo, "rev-parse", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    commit(other, "c.txt", "3")
    g(other, "push", "-q", "origin", "main")
    assert core.git_fetch(project)["ok"]
    assert core.git_status(project)["behind"] == 1
    assert core.git_pull(project)["ok"] and (project.repo / "c.txt").exists()
    assert core.git_status(project)["behind"] == 0
    # diverged: ff-only pull refuses and changes nothing
    commit(project.repo, "d.txt", "4")
    commit(other, "e.txt", "5")
    g(other, "push", "-q", "origin", "main")
    head = g(project.repo, "rev-parse", "HEAD")
    core.git_fetch(project)
    r = core.git_pull(project)
    assert not r["ok"] and g(project.repo, "rev-parse", "HEAD") == head
    # push is never forced: rejected
    assert not core.git_push(project)["ok"]


def test_merge_ok_and_conflict(project):
    commit(project.repo, "f.txt", "base")
    g(project.repo, "checkout", "-q", "-b", "auto/001")
    commit(project.repo, "f.txt", "branch")
    g(project.repo, "checkout", "-q", "main")
    g(project.repo, "checkout", "-q", "-b", "auto/002")
    commit(project.repo, "n.txt", "new")
    g(project.repo, "checkout", "-q", "main")
    commit(project.repo, "f.txt", "main")
    head = g(project.repo, "rev-parse", "HEAD")
    r = core.git_merge(project, "auto/001")
    assert not r["ok"] and r["conflicts"] == ["f.txt"] and "f.txt" in r["message"]
    assert g(project.repo, "rev-parse", "HEAD") == head and g(project.repo, "status", "--porcelain") == ""
    assert (project.repo / "f.txt").read_text() == "main"
    assert core.git_merge(project, "auto/002")["ok"] and (project.repo / "n.txt").exists()
    assert not core.git_merge(project, "nope")["ok"]


def test_merge_refuses_dirty_or_wrong_branch(project):
    g(project.repo, "checkout", "-q", "-b", "auto/001")
    commit(project.repo, "a.txt", "1")
    assert "not on the base" in core.git_merge(project, "auto/001")["message"]
    g(project.repo, "checkout", "-q", "main")
    (project.repo / "z.txt").write_text("z")
    assert "uncommitted" in core.git_merge(project, "auto/001")["message"]


def test_http(project, remote, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/api/p/{project.slug}/git"

    def call(path="", body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        assert call()["branch"] == "main"
        commit(project.repo, "a.txt", "1")
        assert not call("/push", {})["ok"]  # needs confirm
        assert g(remote, "rev-parse", "main") != g(project.repo, "rev-parse", "main")
        r = call("/push", {"confirm": True})
        assert r["ok"] and r["status"]["ahead"] == 0
        monkeypatch.setattr(server.Runner, "running", lambda self: True)
        r = call("/pull", {"confirm": True})
        assert not r["ok"] and "in progress" in r["message"]
    finally:
        srv.shutdown()
