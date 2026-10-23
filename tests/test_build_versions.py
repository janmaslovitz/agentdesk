"""Versioned builds: the version list, the finished-task checklist and build/<version> integration branches."""

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agentdesk import core, server

OLD, NEW = "2000-01-01T00:00:00", "2999-01-01T00:00:00"  # finished long before / after the tag


@pytest.fixture(autouse=True)
def ident(monkeypatch, tmp_path):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "nogc"), "GIT_CONFIG_SYSTEM": str(tmp_path / "nogc")}.items():
        monkeypatch.setenv(k, v)


def g(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, msg):
    (repo / name).write_text(text, encoding="utf-8")
    g(repo, "add", name)
    g(repo, "commit", "-q", "-m", msg)


def branch(repo, name, file, text):
    g(repo, "checkout", "-q", "-b", name, "main")
    commit(repo, file, text, name)
    g(repo, "checkout", "-q", "main")


def set_cmd(p, script):
    cmd = f'"{sys.executable}" -c "{script}"'
    text = core._project_text(p).split("\n[build]")[0] + "\n[build]\ncommand = " + core.toml_str(cmd) + "\n"
    core._write_project(p, text)


def wait(job, secs=30):
    end = time.time() + secs
    while job.running() and time.time() < end:
        time.sleep(0.1)
    assert not job.running()


@pytest.fixture
def repo_tasks(project):
    """main: a.txt, tag v1.0.0; auto/001 merged after the tag; auto/002 unmerged (finished after the tag);
    auto/003 unmerged (finished before it); auto/006 conflicts with main on a.txt; 004 done without a branch;
    005 pending with a branch."""
    p, repo = project, project.repo
    commit(repo, "a.txt", "one\n", "a")
    g(repo, "tag", "v1.0.0")
    for tid, file, text in [("001", "f1.txt", "1"), ("002", "b.txt", "b"), ("003", "c.txt", "c"), ("005", "e.txt", "e"),
                            ("006", "a.txt", "theirs\n")]:
        branch(repo, f"auto/{tid}", file, text)
    g(repo, "merge", "-q", "--no-ff", "--no-edit", "auto/001")
    commit(repo, "a.txt", "ours\n", "main changes a")
    for tid in ("001", "002", "003", "004", "005", "006"):
        core.add_task(p, tid, f"Task {tid}", "body")
    tasks = {tid: {"status": "done", "branch": f"auto/{tid}", "finished": NEW} for tid in ("001", "002", "004", "006")}
    tasks["003"] = {"status": "done", "branch": "auto/003", "finished": OLD}
    tasks["005"] = {"status": "pending"}
    core.Store(p).save({"tasks": tasks, "spent_usd": 0.0})
    return p


def test_versions_and_next(project):
    repo = project.repo
    assert core.next_version(project) == "0.1.0"
    commit(repo, "x", "x", "x")
    g(repo, "tag", "v1.2.0")
    g(repo, "tag", "v1.10.1")
    g(repo, "tag", "not-a-version")
    assert [t["version"] for t in core.release_tags(project)] == ["1.10.1", "1.2.0"]
    assert core.next_version(project) == "1.10.2"
    (repo / "pkg").mkdir()
    commit(repo, "pkg/__init__.py", '__version__ = "2.0.0"  # bumped\n', "v")
    assert core.next_version(project) == "2.0.1"
    commit(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## 2.1.0 - 2026-01-01\n", "cl")
    assert core.next_version(project) == "2.1.1"
    plan = core.build_plan(project)
    assert plan["versions"][0] == {"version": "2.1.1", "kind": "next", "ref": "main", "proposed": "2.1.1"}
    assert [v["version"] for v in plan["versions"][1:]] == ["1.10.1", "1.2.0"]
    assert plan["version"] == "2.1.1" and plan["ref"] == "main"


def test_task_listing_flags(repo_tasks):
    p = repo_tasks
    plan = core.build_plan(p)
    rows = {t["id"]: t for t in plan["tasks"]}
    assert list(rows) == ["006", "003", "002", "001"]  # done with a branch only, newest first
    assert rows["001"]["merged"] and rows["001"]["in_base"] and rows["001"]["checked"] and not rows["001"]["default_checked"]
    assert not rows["002"]["merged"] and not rows["002"]["in_base"] and rows["002"]["default_checked"]
    assert not rows["003"]["default_checked"] and not rows["003"]["checked"]  # finished before the last tag
    assert rows["002"]["title"] == "Task 002"
    old = core.build_plan(p, "1.0.0")  # the tag predates the merge of 001
    assert old["ref"] == "v1.0.0" and old["version"] == "1.0.0"
    o = {t["id"]: t for t in old["tasks"]}
    assert o["001"]["merged"] and not o["001"]["in_base"]


def test_selection_validation_and_memory(repo_tasks):
    p = repo_tasks
    for bad in ("x", "--1", "1.2.3; rm -rf /", "", None, "1"):
        sel, why = core.check_build_selection(p, bad, [])
        assert sel is None and "version" in why
    assert "list" in core.check_build_selection(p, "1.0.1", "002")[1]
    assert "list" in core.check_build_selection(p, "1.0.1", [2])[1]
    assert "005" in core.check_build_selection(p, "1.0.1", ["005"])[1]  # not finished
    assert "004" in core.check_build_selection(p, "1.0.1", ["004"])[1]  # no branch left
    assert "999" in core.check_build_selection(p, "1.0.1", ["999"])[1]
    sel, why = core.check_build_selection(p, "v1.0.1", ["002", "001", "002"])
    assert why is None and sel["version"] == "1.0.1" and sel["branch"] == "build/1.0.1" and sel["ref"] == "main"
    assert sel["tasks"] == ["001", "002"] and sel["merge"] == ["002"]
    core.save_build_selection(p, "1.5.0", ["003"], sel["seen"])
    plan = core.build_plan(p)
    assert plan["version"] == "1.5.0" and plan["versions"][0]["version"] == "1.5.0"  # the edited next version is kept
    rows = {t["id"]: t for t in plan["tasks"]}
    assert rows["003"]["checked"] and not rows["002"]["checked"] and rows["001"]["checked"]


def test_integration_clean_merge(repo_tasks):
    p, repo = repo_tasks, repo_tasks.repo
    set_cmd(p, "import os; print('HAS_B', os.path.exists('b.txt'), os.path.exists('c.txt'))")
    main0, t2 = g(repo, "rev-parse", "main"), g(repo, "rev-parse", "auto/002")
    sel, _ = core.check_build_selection(p, "1.0.1", ["001", "002", "003"])
    job = core.build_job(p)
    assert job.start("custom", "", None, sel) is None
    wait(job)
    i = job.info()
    assert i["status"] == "ok", i["log"]
    assert i["version"] == "1.0.1" and i["branch"] == "build/1.0.1" and i["included"] == ["002", "003"]
    assert "HAS_B True True" in i["log"] and "already in" in i["log"]
    assert g(job.wt, "rev-parse", "--abbrev-ref", "HEAD") == "build/1.0.1"
    assert g(repo, "rev-parse", "main") == main0 and g(repo, "rev-parse", "auto/002") == t2
    assert g(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and g(repo, "status", "--porcelain") == ""
    # building again resets the branch to the base: only what is chosen now is in it
    sel, _ = core.check_build_selection(p, "1.0.1", ["002"])
    assert job.start("custom", "", None, sel) is None
    wait(job)
    assert job.info()["status"] == "ok" and "HAS_B True False" in job.info()["log"]
    # the old ref build still works on the same worktree
    assert job.start("custom", "auto/003") is None
    wait(job)
    assert job.info()["status"] == "ok" and job.info()["version"] is None and "HAS_B False True" in job.info()["log"]


def test_integration_conflict_leaves_base_alone(repo_tasks):
    p, repo = repo_tasks, repo_tasks.repo
    marker = p.dir / "built.txt"
    set_cmd(p, f"open(r'{marker}', 'w').write('x')")
    before = {b: g(repo, "rev-parse", b) for b in ("main", "auto/002", "auto/006")}
    sel, _ = core.check_build_selection(p, "1.0.1", ["002", "006"])
    job = core.build_job(p)
    assert job.start("custom", "", None, sel) is None
    wait(job)
    i = job.info()
    assert i["status"] == "failed" and i["conflict"]["task"] == "006" and i["conflict"]["files"] == ["a.txt"]
    assert i["included"] == ["002"] and "merge conflict" in i["log"] and "unchanged" in i["log"]
    assert not marker.exists()  # nothing was built
    assert {b: g(repo, "rev-parse", b) for b in before} == before
    assert g(repo, "status", "--porcelain") == "" and (repo / "a.txt").read_text() == "ours\n"
    assert g(job.wt, "status", "--porcelain") == ""  # the merge was aborted in the build worktree too


def test_http_validation(repo_tasks):
    p = repo_tasks
    set_cmd(p, "print('built')")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/api/p/{p.slug}/build"

    def call(path="", body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req)
            return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        code, s = call()
        assert code == 200 and s["status"] == "idle" and s["versions"][0]["kind"] == "next"
        assert {t["id"] for t in s["tasks"]} == {"001", "002", "003", "006"}
        assert set(s["tasks"][0]) >= {"id", "title", "merged", "in_base", "default_checked"}
        assert "versions" not in call("?lite=1")[1]
        assert call("?version=1.0.0")[1]["ref"] == "v1.0.0"
        for body, word in [({"version": "nope", "tasks": []}, "version"), ({"version": "1.0.1", "tasks": "002"}, "list"),
                           ({"version": "1.0.1", "tasks": ["005"]}, "005"), ({"version": "1.0.1", "tasks": ["../x"]}, "../x")]:
            code, r = call("", {**body, "kind": "custom", "confirm": True})
            assert code == 400 and word in r["message"] and not r["ok"]
        assert core.build_job(p).info()["status"] == "idle"
        code, r = call("", {"kind": "custom", "version": "1.0.1", "tasks": ["002"], "confirm": True})
        assert code == 200 and r["ok"]
        wait(core.build_job(p))
        s = call()[1]
        assert s["status"] == "ok" and s["version"] == "1.0.1" and s["included"] == ["002"]
        assert core.build_selection(p)["tasks"] == ["002"]
        rows = {t["id"]: t for t in s["tasks"]}
        assert rows["002"]["checked"] and not rows["006"]["checked"]  # remembered, not the default
        assert core.branch_merged(p, "auto/002") is False  # nothing went into main
    finally:
        core.build_job(p).stop()
        srv.shutdown()
