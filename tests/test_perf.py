"""Performance fixes: batched git, an overview that never makes a request wait, incremental chat, fewer writes."""

import os
import subprocess
import time

import pytest

from agentdesk import __main__ as cli, core, server


def g(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo_project(project):
    g(project.repo, "commit", "-q", "--allow-empty", "-m", "base")
    g(project.repo, "branch", "auto/001")  # at main: merged
    g(project.repo, "checkout", "-q", "-b", "auto/002")
    g(project.repo, "commit", "-q", "--allow-empty", "-m", "work")  # one commit ahead: not merged
    g(project.repo, "checkout", "-q", "main")
    core._MERGED_CACHE.clear()
    return project


def test_merge_checks_use_two_git_calls_for_all_branches(repo_project, monkeypatch):
    calls = []
    real = core.git
    monkeypatch.setattr(core, "git", lambda args, cwd: calls.append(args) or real(args, cwd))
    p = repo_project
    assert core.branch_merged(p, "auto/001") is True
    assert core.branch_merged(p, "auto/002") is False
    assert core.branch_merged(p, "auto/404") is None  # no such branch
    assert core.branch_merged(p, "main") is True
    assert len(calls) == 2  # not two per branch
    core.branch_merged(p, "auto/001")
    assert len(calls) == 2  # cached for 20 s


def test_git_status_lists_unmerged_auto_branches_in_one_call(repo_project, monkeypatch):
    calls = []
    real = core.run
    monkeypatch.setattr(core, "run", lambda cmd, cwd, **k: calls.append(cmd) or real(cmd, cwd, **k))
    st = core.git_status(repo_project)
    assert st["autos"] == [{"branch": "auto/002", "ahead": 1}]
    assert not any(c[:2] == ["git", "rev-list"] and "main..auto/002" in c for c in calls)  # no per-branch counting


def test_overview_serves_the_last_answer_while_refreshing(monkeypatch):
    n = {"calls": 0}

    def slow():
        n["calls"] += 1
        time.sleep(0.4)
        return {"v": n["calls"]}

    monkeypatch.setattr(server, "_overview", slow)
    monkeypatch.setattr(server, "_OV", (time.time() - 10, {"v": 0}))
    t = time.time()
    assert server.overview() == {"v": 0}  # stale, but answered at once
    assert time.time() - t < 0.2
    assert server.overview() == {"v": 0}  # still refreshing: no second walk is started
    end = time.time() + 5
    while time.time() < end and server._OV[1] == {"v": 0}:
        time.sleep(0.05)
    assert server._OV[1] == {"v": 1} and n["calls"] == 1


def test_chat_info_since_returns_only_new_entries(project):
    c = server.Chat(server.LOOP, project)
    c.history = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b", "seq": 1, "kind": "live"},
                 {"role": "assistant", "text": "c", "seq": 2, "kind": "live"}]
    assert len(c.info()["history"]) == 3
    assert [m["text"] for m in c.info(1)["history"]] == ["c"]
    assert c.info(2)["history"] == []


def test_streamed_entries_do_not_rewrite_the_transcript_each_time(project, monkeypatch):
    c = server.Chat(server.LOOP, project)
    saves = []
    monkeypatch.setattr(c, "save_history", lambda: saves.append(1))
    for i in range(30):
        c._live_add({"role": "assistant", "kind": "live", "text": f"line {i}"})
    assert len(saves) <= 2
    c._live_add({"role": "assistant", "kind": "live", "text": "", "cost": 0.1})  # a finished burst always saves
    assert len(saves) >= 2 and c.history[-1]["cost"] == 0.1


def test_a_page_that_goes_away_is_not_logged_as_an_error(capsys):
    from http.server import ThreadingHTTPServer
    srv = server.quiet_errors(ThreadingHTTPServer(("127.0.0.1", 0), server.Handler))
    srv.server_close()
    for exc in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        try:
            raise exc("gone")
        except exc:
            srv.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_lineage_comes_from_one_snapshot_not_a_process_per_ancestor(monkeypatch):
    monkeypatch.setattr(cli, "process_info", lambda pid: pytest.fail("must not start a process per ancestor"))
    t = time.time()
    lineage = cli.own_lineage()
    assert os.getpid() in lineage and os.getppid() in lineage and time.time() - t < 2
    assert cli.parent_map()[os.getpid()] == os.getppid()
