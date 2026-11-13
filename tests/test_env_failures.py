"""A check that can't even start (a folder it needs vanished, the command is missing) is the environment's fault:
it is tried again, and if it still can't run the attempt ends as a crash (retried later), not as a failed task that
escalates to a stronger model. Scratch folders live in the data folder, not in the service's private /tmp."""

import asyncio
import shutil

from agentdesk import core


def test_scratch_folders_live_in_the_data_folder():
    assert core.scratch_home().startswith(str(core.DATA)) and core.scratch_home().endswith(core.SCRATCH_NAME)
    assert core.work_tmp().startswith(str(core.DATA))
    assert core._is_scratch_path("/tmp/agentdesk-worker-home") and core._is_scratch_path(core.scratch_home())
    assert not core._is_scratch_path(str(core.DATA))


def test_run_recreates_vanished_scratch_folders(tmp_path):
    shutil.rmtree(core.DATA / ".scratch", ignore_errors=True)
    code, out = core.run("echo $TMPDIR; echo ok", tmp_path, shell=True)
    assert code == 0 and "ok" in out and core.work_tmp() in out


def test_environment_failure_detection():
    assert core.environment_failure(127, "[Errno 2] No such file or directory: '/tmp/agentdesk-worker-home'")
    assert core.environment_failure(127, "/bin/sh: 1: cargo: not found")
    assert not core.environment_failure(1, "error[E0599]: no method named ...")
    assert not core.environment_failure(127, "test failed: expected 1 got 2")


def test_gate_that_cannot_run_is_retried_then_reported(project, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", lambda *a, **k: calls.append(1) or (127, "/bin/sh: 1: cargo: not found"))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    project.cfg.setdefault("gates", {})["quick"] = ["cargo fmt --all --check"]
    ok, failure = core.run_gates(project, core.Store(project), project.repo, "quick", None)
    assert not ok and core.ENV_FAILURE in failure and len(calls) == 2


def test_environment_failure_is_a_crash_not_an_escalation(project, monkeypatch, tmp_path):
    seen = []

    async def route(p, store, task, meter):
        return {"tier": "standard", "why": "", "brief": "b"}

    async def work(p, store, task, wt, tier, brief, meter, effort=None):
        seen.append((tier, effort))
        raise RuntimeError("the checks could not run (the environment, not the task): cargo: not found")

    monkeypatch.setattr(core, "route", route)
    monkeypatch.setattr(core, "work", work)
    monkeypatch.setattr(core, "ensure_worktree", lambda *a: tmp_path)
    monkeypatch.setattr(core, "worktree_conflicts", lambda *a: "")
    monkeypatch.setattr(core, "git", lambda *a, **k: (0, "abc"))
    core.add_task(project, "1", "t", "b")
    asyncio.run(core.run_dag(project, max_parallel=1))
    e = core.Store(project).load()["tasks"]["1"]
    assert seen == [("standard", "medium")]          # no climb to stronger rungs
    assert e["status"] == "pending" and e["crashes"] == 1
    assert core.classify_failure(e["last_failure"], "pending") == "crash"   # the auto-runner retries it once
