"""A worker that stops for a decision (or changes nothing without saying why) leaves the task waiting for the user:
status needs_decision, never done; what builds on it waits; the user's answer puts it back in the queue."""

import asyncio
import subprocess

import pytest

from agentdesk import core, dag, server, service

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


@pytest.fixture
def proj(project):
    subprocess.run(["git", "config", "user.name", "t"], cwd=project.repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=project.repo, check=True)
    (project.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project.repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=project.repo, check=True)
    return project


def fake_worker(monkeypatch, summary, edit=None):
    """core.work replaced: optionally edits the worktree, then reports success with `summary`."""
    async def work(p, store, task, wt, tier, brief, meter):
        if edit:
            (wt / f"{task.id}-{edit}").write_text("changed\n", encoding="utf-8")
        return True, summary
    async def route(p, store, task, meter):
        return {"tier": "standard", "why": "test", "brief": task.body}
    monkeypatch.setattr(core, "work", work)
    monkeypatch.setattr(core, "route", route)


def run_task(p, tid):
    asyncio.run(core.run_queue(p, only=tid))
    return core.Store(p).load()["tasks"][tid]


def test_explicit_question_stops_the_task(proj, monkeypatch):
    core.add_task(proj, "042", "Russian UI", "port ru.tsv")
    fake_worker(monkeypatch, "Looked around.\nNEEDS_DECISION: Where do Russian strings come from: our ru.tsv or craft-i18n? "
                             "I recommend ru.tsv.\nNOTES: none")
    e = run_task(proj, "042")
    assert e["status"] == "needs_decision" and "ru.tsv or craft-i18n" in e["question"] and "NOTES" not in e["question"]
    assert not e.get("commit")
    assert "Needs your decision" in (proj.path("reports") / "042.md").read_text()
    assert "needs a decision" in proj.path("agentdesk.log").read_text()


def test_question_wins_even_with_changes(proj, monkeypatch):
    core.add_task(proj, "1", "Half", "x")
    fake_worker(monkeypatch, "Did part.\nNEEDS_DECISION: keep A or B?", edit="a.txt")
    assert run_task(proj, "1")["status"] == "needs_decision"


def test_silent_no_change_is_not_done(proj, monkeypatch):
    core.add_task(proj, "042", "Russian UI", "port ru.tsv")
    fake_worker(monkeypatch, "The source file is gone after the merge; the rest depends on that choice.\nNOTES: none")
    e = run_task(proj, "042")
    assert e["status"] == "needs_decision" and "without changing anything" in e["question"] and "source file is gone" in e["question"]


def test_explained_no_change_is_done(proj, monkeypatch):
    core.add_task(proj, "051", "Final check", "verify")
    fake_worker(monkeypatch, "All checks pass.\nNO_CHANGES_NEEDED: verification only, everything already in place.\nNOTES: none")
    assert run_task(proj, "051")["status"] == "done"


def test_normal_change_is_done(proj, monkeypatch):
    core.add_task(proj, "1", "Edit", "x")
    fake_worker(monkeypatch, "Changed a.txt.\nNOTES: none", edit="a.txt")
    e = run_task(proj, "1")
    assert e["status"] == "done" and e.get("commit")


def test_dependents_wait_and_answer_requeues(proj, monkeypatch):
    core.add_task(proj, "1", "Decide", "x")
    core.add_task(proj, "2", "After", "y", None, ["1"])
    fake_worker(monkeypatch, "NEEDS_DECISION: A or B?")
    asyncio.run(core.run_dag(proj, max_parallel=2))
    st = core.Store(proj).load()
    assert st["tasks"]["1"]["status"] == "needs_decision" and "2" not in st["tasks"]
    nodes = dag.nodes_from_tasks(core.load_tasks(proj))
    assert dag.stuck(nodes, st) == {"2": "1"}
    assert {n["id"]: n["state"] for n in dag.view(nodes, st)["nodes"]} == {"1": "needs_decision", "2": "stuck"}
    items = core.attention_items(proj.slug, core.load_tasks(proj), st)
    assert any(i["kind"] == "decision" and "A or B?" in i["reason"] for i in items)
    assert "summary" not in st  # (the run summary isn't stored)

    service.call("answer", {"project": proj.slug, "id": "1", "answer": "Take B."})
    st = core.Store(proj).load()
    e = st["tasks"]["1"]
    assert e["status"] == "pending" and "question" not in e and e["decisions"][0]["answer"] == "Take B."
    body = next(t for t in core.load_tasks(proj) if t.id == "1").body
    assert "## Decision" in body and "Take B." in body and "Question: A or B?" in body

    fake_worker(monkeypatch, "Did B.\nNOTES: none", edit="b.txt")
    asyncio.run(core.run_dag(proj, max_parallel=2))
    st = core.Store(proj).load()
    assert st["tasks"]["1"]["status"] == "done" and st["tasks"]["2"]["status"] == "done"


def test_answer_rules(proj):
    core.add_task(proj, "1", "x", "y")
    with pytest.raises(ValueError, match="empty"):
        core.answer_task(proj, "1", "  ")
    with pytest.raises(KeyError):
        core.answer_task(proj, "nope", "a")
    st = core.Store(proj).load()
    st["tasks"]["1"] = {"status": "done"}
    core.Store(proj).save(st)
    with pytest.raises(ValueError, match="done"):
        core.answer_task(proj, "1", "a")
    assert service.rest("POST", ["projects", proj.slug, "tasks", "1", "answer"], {}, {"answer": "a"})[0] == 400


def test_chat_side(proj, monkeypatch):
    core.add_task(proj, "7", "Ask", "x")
    st = core.Store(proj).load()
    st["tasks"]["7"] = {"status": "needs_decision", "question": "Keep the old API?", "finished": "2026-11-12T12:00:00"}
    core.Store(proj).save(st)
    text = server.run_finished_prompt(proj, "2026-11-12T11:00:00")
    assert "7 needs_decision" in text and "QUESTION FOR THE USER: Keep the old API?" in text and "answer_task" in text
    c = server.Chat(server.LOOP, proj)
    try:
        tool = next(t for t in c.tools(approval=True) if t.name == "answer_task")
        r = asyncio.run_coroutine_threadsafe(tool.handler({"id": "7", "answer": "Yes, keep it."}), server.LOOP).result(5)
        assert not r.get("is_error")
        assert core.Store(proj).load()["tasks"]["7"]["decisions"][0]["by"] == "user via chat"
    finally:
        c.approvals.deny_all()


def test_worker_prompt_explains_the_markers():
    assert "NEEDS_DECISION:" in core.WORKER_PROMPT and "NO_CHANGES_NEEDED:" in core.WORKER_PROMPT and "## Decision" in core.WORKER_PROMPT
