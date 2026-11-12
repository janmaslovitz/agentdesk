"""Pipeline hardening: the reviewer sends unfinished work back, runs get a budget from the plan, a budget stop is
reported as a pause, and paused work needs someone to go on."""

import asyncio
import subprocess

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from agentdesk import core, server

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def result(cost=0.1):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                         total_cost_usd=cost, usage={})


class Worker:
    """A fake worker session: every query answers with the next summary; records the messages it was sent."""
    summaries: list = []
    sent: list = []

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):
        Worker.sent.append(text)

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text=Worker.summaries.pop(0))], model="sonnet")
        yield result()


@pytest.fixture
def proj(project):
    (project.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project.repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=project.repo, check=True)
    return project


def setup(proj, monkeypatch, summaries, verdicts):
    Worker.summaries, Worker.sent = list(summaries), []
    monkeypatch.setattr(core, "ClaudeSDKClient", Worker)
    monkeypatch.setattr(core, "run_gates", lambda *a, **k: (True, ""))
    monkeypatch.setattr(core, "changed_crates", lambda *a: [])
    real = core.settings().get("review", {})
    monkeypatch.setattr(core, "review_cfg", lambda: {"enabled": True, "model": "sonnet", "effort": "low", "max_turns": 5,
                                                     "budget_usd": 0.5, "rounds": 1})
    seen = []

    async def review(p, store, task, wt, summary, meter):
        seen.append(summary)
        return verdicts.pop(0)
    monkeypatch.setattr(core, "review_attempt", review)
    core.add_task(proj, "1", "Add feature", "Do the thing.\n\n## Done when\n- tests pass")
    task = next(t for t in core.load_tasks(proj) if t.id == "1")
    store = core.Store(proj)
    st = store.load()
    st["tasks"]["1"] = {"title": task.title}
    meter = core.Meter(proj, store, st)
    wt = core.ensure_worktree(proj, store, task, st)
    return task, store, meter, wt, seen


def test_reviewer_sends_unfinished_work_back(proj, monkeypatch):
    task, store, meter, wt, seen = setup(proj, monkeypatch, ["stubbed it", "really done"],
                                         [{"verdict": "fix", "problems": ["a.py: feature is a TODO stub"], "instructions": "implement it"}])
    ok, summary = asyncio.run(core.work(proj, store, task, wt, "standard", "brief", meter))
    assert ok and summary == "really done"
    assert len(Worker.sent) == 2 and "a.py: feature is a TODO stub" in Worker.sent[1] and "implement it" in Worker.sent[1]
    assert "feature is a TODO stub" in meter.state["tasks"]["1"]["last_review"]
    assert seen == ["stubbed it"]  # one review round per tier: the second attempt isn't reviewed again


def test_reviewer_pass_and_ask(proj, monkeypatch):
    task, store, meter, wt, _ = setup(proj, monkeypatch, ["done"], [{"verdict": "pass", "problems": []}])
    assert asyncio.run(core.work(proj, store, task, wt, "standard", "b", meter)) == (True, "done")


def test_reviewer_question_becomes_a_decision(proj, monkeypatch):
    task, store, meter, wt, _ = setup(proj, monkeypatch, ["done"], [{"verdict": "ask", "problems": [], "question": "Keep v1 API?"}])
    ok, summary = asyncio.run(core.work(proj, store, task, wt, "standard", "b", meter))
    assert ok and "NEEDS_DECISION: Keep v1 API?" in summary
    assert core.decision_needed(summary, wt, "x").startswith("Keep v1 API?")


def test_review_out_of_attempts_escalates(proj, monkeypatch):
    monkeypatch.setitem(core.settings()["limits"], "retries_per_tier", 1)
    real = core.settings
    monkeypatch.setattr(core, "settings", lambda: {**real(), "limits": {**real()["limits"], "retries_per_tier": 1}})
    task, store, meter, wt, _ = setup(proj, monkeypatch, ["stub"], [{"verdict": "fix", "problems": ["stub"], "instructions": "do it"}])
    ok, _ = asyncio.run(core.work(proj, store, task, wt, "standard", "b", meter))
    assert not ok  # do_task escalates to the next tier


def test_attempt_diff_shows_new_and_changed_files(proj):
    core.add_task(proj, "9", "t", "b")
    task = next(t for t in core.load_tasks(proj) if t.id == "9")
    wt = core.ensure_worktree(proj, core.Store(proj), task, core.Store(proj).load())
    (wt / "a.py").write_text("x = 2\n", encoding="utf-8")
    (wt / "new.py").write_text("y = 3\n", encoding="utf-8")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "a.pyc").write_bytes(b"\0")
    status, diff = core.attempt_diff(wt)
    assert "a.py" in status and "new.py" in status and "+x = 2" in diff and "+y = 3" in diff and ".pyc" not in diff


# ----------------------------------------------------------------------------- budgets and pauses

def test_plan_budget_comes_from_the_estimate(proj):
    assert core.plan_budget(proj, {"tasks": []}) == core.settings()["limits"]["run_budget_usd"]
    big = {"tasks": [{"tier": "hard"}] * 8}
    assert core.plan_budget(proj, big) == core.plan_estimate(big["tasks"])[1] > 40


def test_run_budget_override_and_pause_report(proj, monkeypatch):
    core.add_task(proj, "1", "a", "b")
    core.add_task(proj, "2", "c", "d")

    async def spend(p, store, task, meter):
        meter.state["spent_usd"] = meter.state.get("spent_usd", 0) + 3
        meter.state["tasks"].setdefault(task.id, {})["status"] = "paused"
        store.save(meter.state)
    monkeypatch.setattr(core, "do_task", spend)
    res = asyncio.run(core.run_dag(proj, max_parallel=1, budget_usd=2.5))
    assert res["stopped"] == "run budget 2.5 USD spent" and res["paused"] == ["1"]
    log = proj.path("agentdesk.log").read_text()
    assert "Run all goes on" in log and "paused: 1" in log
    items = core.attention_items(proj.slug, core.load_tasks(proj), core.Store(proj).load())
    assert any(i["kind"] == "paused" and "spending decision" in i["reason"] for i in items)


def test_paused_wakeup_asks_before_spending(proj):
    core.add_task(proj, "5", "x", "y")
    st = core.Store(proj).load()
    st["tasks"]["5"] = {"status": "paused", "started": "2026-11-12T10:00:00"}
    core.Store(proj).save(st)
    text = server.run_finished_prompt(proj, "2026-11-12T09:00:00")
    assert "PAUSED" in text and "don't continue on your own" in text


def test_cli_and_service_pass_the_budget(proj, monkeypatch):
    from agentdesk import service
    started = []
    monkeypatch.setattr(service, "spawn", lambda p, args, kind="run": started.append(args) or type("P", (), {"pid": 1})())
    service.run(proj.slug, budget_usd=55)
    assert started[-1][-2:] == ["--budget", "55"]


# ----------------------------------------------------------------------------- notifications

def test_notify_sends_only_when_configured(proj, monkeypatch):
    import urllib.request
    sent = []

    class Resp:
        def read(self):
            return b"{}"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: sent.append((req.full_url, req.data)) or Resp())
    real = core.settings
    assert core.notify(proj, "blocked", "x") is False  # nothing configured
    monkeypatch.setenv("AGENTDESK_TELEGRAM_TOKEN", "123:ABC")
    monkeypatch.setattr(core, "settings", lambda: {**real(), "notify": {**real()["notify"], "telegram_chat_id": "42",
                                                                       "webhook_url": "http://127.0.0.1:9/hook"}})
    assert core.notify(proj, "needs_decision", "❓ 042 needs your decision") is True
    import time
    for _ in range(50):
        if len(sent) == 2:
            break
        time.sleep(0.02)
    urls = sorted(u for u, _ in sent)
    assert urls == ["http://127.0.0.1:9/hook", "https://api.telegram.org/bot123:ABC/sendMessage"]
    assert all(b"042 needs your decision" in d for _, d in sent)
    assert core.notify(proj, "something_else", "x") is False  # not in events
