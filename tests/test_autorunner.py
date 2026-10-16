"""Auto-runner: pure functions with fake state, and dashboard passes with a fake run starter."""

import subprocess
from unittest import mock

from agentdesk import core, server
from agentdesk.core import Task

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def T(i, deps=()):
    return Task(id=i, title=f"task {i}", body="", depends=list(deps))


def S(**tasks):
    return {"tasks": {k: dict(v) for k, v in tasks.items()}}


# ---- pure functions

def test_next_startable():
    tasks = [T("001"), T("002", ["001"]), T("003")]
    assert core.next_startable(tasks, S(), run_going=True) is None
    assert core.next_startable(tasks, S(), False).id == "001"
    st = S(**{"001": {"status": "done"}})
    assert core.next_startable(tasks, st, False).id == "002"
    st = S(**{"001": {"status": "working"}})  # 002 waits on 001; 003 is free
    assert core.next_startable(tasks, st, False).id == "003"
    st = S(**{"001": {"status": "blocked"}, "003": {"status": "done"}})
    assert core.next_startable(tasks, st, False) is None
    st = S(**{"001": {"crashes": 2, "last_failure": "worker exited unexpectedly (code 1)"}})
    assert core.next_startable([T("001")], st, False, retry_crashed=1) is None
    st = S(**{"001": {"crashes": 1}})
    assert core.next_startable([T("001")], st, False, retry_crashed=1).id == "001"
    st = S(**{"001": {"last_failure": "$ pytest\nFAILED"}})
    assert core.next_startable([T("001")], st, False) is None


def test_recover_stale():
    st = S(a={"status": "working"}, b={"status": "done"}, c={"status": "routing", "crashes": 1})
    assert core.recover_stale(st, alive=True) == []
    assert st["tasks"]["a"]["status"] == "working"
    assert core.recover_stale(st, alive=False, exit_code=129) == ["a", "c"]
    assert st["tasks"]["a"] == {"status": "pending", "last_failure": "worker exited unexpectedly (code 129)", "crashes": 1}
    assert st["tasks"]["c"]["crashes"] == 2 and st["tasks"]["b"]["status"] == "done"
    st = S(a={"status": "working"})
    core.recover_stale(st, False)
    assert st["tasks"]["a"]["last_failure"] == "worker exited unexpectedly"


def test_classify_failure():
    assert core.classify_failure("worker exited unexpectedly (code 129)") == "crash"
    assert core.classify_failure("$ cargo test\nerror") == "gate"
    assert core.classify_failure("", "blocked") == "task"
    assert core.classify_failure("", "working", 129) == "crash"
    assert core.classify_failure("", "pending") == "none"


def test_attention_items():
    tasks = [T("001"), T("002"), T("003"), T("004"), T("005")]
    st = S(**{"001": {"status": "blocked", "last_failure": "$ pytest\nboom"},
              "002": {"status": "pending", "crashes": 2, "last_failure": "worker exited unexpectedly (code 1)"},
              "003": {"status": "done", "branch": "auto/003"},
              "004": {"status": "done", "branch": "auto/004", "reviewed": True},
              "005": {"status": "done", "branch": "auto/005", "merge": "conflict"}})
    items = core.attention_items("p", tasks, st, {"003": False, "004": False, "005": True})
    kinds = {(i["task"], i["kind"]) for i in items}
    assert kinds == {("001", "gate"), ("002", "repeated"), ("003", "unmerged"), ("005", "conflict")}
    assert all(i["project"] == "p" and i["reason"] for i in items)
    assert core.attention_items("p", tasks[:1], S(), {}) == []


# ---- settings

def test_new_project_is_auto_managed_without_keys(project):
    assert "autorunner" not in project.cfg
    assert "autorunner" not in (project.dir / "project.toml").read_text(encoding="utf-8")
    assert core.autorunner_cfg(project) == {"enabled": True, "interval_s": 30, "retry_crashed": 1}
    assert core.settings()["autorunner"]["enabled"] is True
    with mock.patch.object(core, "usage_paused", return_value=None):
        assert server.autorun_status(project) == "Auto: on"


def test_per_project_toggle(project):
    core.set_autorunner(project, False)
    assert core.autorunner_cfg(core.project(project.slug))["enabled"] is False
    assert server.autorun_status(core.project(project.slug)) == "Auto: off"
    core.set_autorunner(core.project(project.slug), True)
    assert core.autorunner_cfg(core.project(project.slug))["enabled"] is True


# ---- dashboard pass with a fake starter

def make(name, tmp_path, n):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "--allow-empty", "-m", "s"], cwd=repo, check=True)
    p = core.add_project(name, str(repo), "main")
    for i in range(1, n + 1):
        core.add_task(p, f"{i:03d}", f"t{i}", "body", None, [f"{i - 1:03d}"] if i > 1 else [])
    return p


def test_two_projects_start_in_sequence_then_retry_once_then_flag(tmp_path):
    a, b = make("autoa-" + tmp_path.name, tmp_path, 2), make("autob-" + tmp_path.name, tmp_path, 1)
    started, running = [], set()

    def starter(p, args):
        started.append((p.slug, args))
        running.add(p.slug)

    def alive(p):
        return p.slug in running, None

    def finish(p, tid, status):
        s = core.Store(p).load()
        s["tasks"].setdefault(tid, {})["status"] = status
        core.Store(p).save(s)

    with mock.patch.object(core, "usage_paused", return_value=None):
        server.autorun_pass(starter, alive)
        assert [x[1] for x in started] == [["run", a.slug, "001"], ["run", b.slug, "001"]]
        server.autorun_pass(starter, alive)  # one run per project: nothing new while they run
        assert len(started) == 2
        running.clear()
        finish(a, "001", "done")
        finish(b, "001", "done")
        server.autorun_pass(starter, alive)
        assert (a.slug, ["run", a.slug, "002"]) in started and len(started) == 3
        # the run for a/002 is killed (task left working): recovered and retried once
        finish(a, "002", "working")
        running.clear()
        server.autorun_pass(starter, lambda p: (False, 129))
        assert len(started) == 4
        s = core.Store(a).load()["tasks"]["002"]
        assert s["status"] == "pending" and s["crashes"] == 1 and "code 129" in s["last_failure"]
        finish(a, "002", "working")
        running.clear()
        server.autorun_pass(starter, lambda p: (False, 129))
        assert len(started) == 4  # no second retry
        assert [(i["task"], i["kind"]) for i in server.project_attention(a) if i["kind"] == "repeated"] == [("002", "repeated")]
        assert server.autorun_status(a) == "waiting: needs attention"
        assert "needs attention: 002 (repeated)" in a.path("agentdesk.log").read_text(encoding="utf-8")


def test_usage_pause_pending_approval_and_off_block_starts(project):
    core.add_task(project, "001", "t", "b", None, [])
    started = []

    def starter(p, args):
        started.append(args)

    def idle(p):
        return False, None

    with mock.patch.object(core, "usage_paused", return_value="85% used"):
        server.autorun_project(project, starter, idle)
        assert started == [] and server.autorun_status(project) == "paused: usage limit"
    with mock.patch.object(core, "usage_paused", return_value=None):
        with mock.patch.object(server.chat(project).approvals, "pending", return_value=[{"id": "x"}]):
            server.autorun_project(project, starter, idle)
        assert started == []
        core.set_autorunner(project, False)
        server.autorun_project(core.project(project.slug), starter, idle)
        assert started == []
        core.set_autorunner(core.project(project.slug), True)
        server.autorun_project(core.project(project.slug), starter, idle)
        assert started == [["run", project.slug, "001"]]


def test_gate_failure_and_unmerged_branch_flagged_no_merge(project):
    core.add_task(project, "001", "gate", "b", None, [])
    core.add_task(project, "002", "done", "b", None, [])
    subprocess.run(["git", "checkout", "-q", "-b", "auto/002"], cwd=project.repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "--allow-empty", "-m", "w"], cwd=project.repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=project.repo, check=True)
    st = core.Store(project).load()
    st["tasks"]["001"] = {"status": "blocked", "last_failure": "$ pytest\nFAILED"}
    st["tasks"]["002"] = {"status": "done", "branch": "auto/002"}
    core.Store(project).save(st)
    calls, started = [], []
    real = core.git

    def spy(args, cwd):
        calls.append(args)
        return real(args, cwd)

    with mock.patch.object(core, "git", spy), mock.patch.object(core, "usage_paused", return_value=None):
        server.autorun_project(project, lambda p, a: started.append(a), lambda p: (False, None))
    assert {(i["task"], i["kind"]) for i in server.project_attention(project)} >= {("001", "gate"), ("002", "unmerged")}
    assert started == []
    assert calls and all(c[0] in ("rev-parse", "merge-base") for c in calls)  # read-only git only: no merge, no push
    log = project.path("agentdesk.log").read_text(encoding="utf-8")
    assert "needs attention: 001 (gate)" in log and "needs attention: 002 (unmerged)" in log
    assert server.attention_all()["count"] >= 2
