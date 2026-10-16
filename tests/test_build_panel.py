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


@pytest.fixture(autouse=True)
def ident(monkeypatch, tmp_path):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "nogc"), "GIT_CONFIG_SYSTEM": str(tmp_path / "nogc")}.items():
        monkeypatch.setenv(k, v)


def set_cmd(p, script):
    """Point `[build] command` at a fake build script (the only place a command can come from)."""
    cmd = f'"{sys.executable}" -c "{script}"'
    text = core._project_text(p).split("\n[build]")[0] + "\n[build]\ncommand = " + core.toml_str(cmd) + "\n"
    core._write_project(p, text)


def wait(job, secs=30):
    end = time.time() + secs
    while job.running() and time.time() < end:
        time.sleep(0.1)
    assert not job.running()


def test_build_ok_and_failed(project):
    set_cmd(project, "print('line one'); print('line two')")
    job = core.build_job(project)
    assert job.info()["status"] == "idle"
    assert job.start("custom", "") is None
    wait(job)
    i = job.info()
    assert i["status"] == "ok" and i["exit"] == 0 and "line two" in i["log"] and i["duration"] is not None
    assert (job.wt / ".git").exists() and project.repo != job.wt
    set_cmd(project, "print('boom'); raise SystemExit(1)")
    assert job.start("custom", "main") is None
    wait(job)
    i = job.info()
    assert i["status"] == "failed" and i["exit"] == 1 and "boom" in i["log"]


def test_bad_requests(project):
    job = core.build_job(project)
    assert "no [build] command" in job.start("custom", "")
    assert "unknown" in job.start("rm -rf", "")
    set_cmd(project, "pass")
    assert "not a branch" in job.start("custom", "nope")
    assert job.start("custom", "--evil")
    # no build_exe.ps1 in the scratch repo
    assert job.start("build", "") is None
    wait(job)
    assert job.info()["status"] == "failed" and "does not exist" in job.info()["log"]


def test_one_at_a_time_and_stop_kills_tree(project):
    marker = project.dir / "child.pid"
    child = "import time; time.sleep(60)"
    script = ("import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','%s']); "
              "open(r'%s','w').write(str(c.pid)); print('started', flush=True); time.sleep(60)" % (child, marker))
    set_cmd(project, script)
    job = core.build_job(project)
    assert job.start("custom", "") is None
    assert "already running" in job.start("custom", "")
    end = time.time() + 20
    while not marker.exists() and time.time() < end:
        time.sleep(0.1)
    time.sleep(0.3)
    pid = int(marker.read_text())
    assert job.stop() is None
    wait(job)
    assert job.info()["status"] == "stopped"
    assert job.stop() == "no build is running"
    # the grandchild must be gone too
    end = time.time() + 10
    alive = True
    while alive and time.time() < end:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True).stdout.decode("ascii", "ignore")
            alive = str(pid) in out
        else:
            import os
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            time.sleep(0.2)
    assert not alive


def test_clean_recreates_worktree(project):
    set_cmd(project, "open('artifact.txt','w').write('x')")
    job = core.build_job(project)
    assert job.start("custom", "") is None
    wait(job)
    assert (job.wt / "artifact.txt").exists()
    # clean wipes the worktree first; no build_exe.ps1 here, so it fails after recreating it
    assert job.start("clean", "") is None
    wait(job)
    assert "clean: removing" in job.info()["log"] and not (job.wt / "artifact.txt").exists()


def test_http(project, monkeypatch):
    set_cmd(project, "import time; print('hello'); time.sleep(60)")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/api/p/{project.slug}/build"

    def call(path="", body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        assert call()["status"] == "idle"
        assert not call("", {"kind": "custom"})["ok"]  # needs confirm
        monkeypatch.setattr(server.Runner, "running", lambda self: True)
        r = call("", {"kind": "custom", "confirm": True})
        assert not r["ok"] and "task run is in progress" in r["message"]
        assert call()["status"] == "idle"
        monkeypatch.undo()
        r = call("", {"kind": "custom", "command": "calc.exe", "confirm": True})  # request text is never a command
        assert r["ok"]
        end = time.time() + 20
        while "hello" not in call()["log"] and time.time() < end:
            time.sleep(0.2)
        s = call()
        assert s["status"] == "running" and "hello" in s["log"]
        assert not call("", {"kind": "custom", "confirm": True})["ok"]  # one at a time
        assert not call("/stop", {})["ok"]  # needs confirm
        assert call("/stop", {"confirm": True})["ok"]
        wait(core.build_job(project))
        assert call()["status"] == "stopped"
    finally:
        core.build_job(project).stop()
        srv.shutdown()
