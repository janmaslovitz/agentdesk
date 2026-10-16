"""Chat Planner mode: draft plans (never run before approval), approve and start, cancel, edit, progress."""

import asyncio
import json
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, PermissionResultAllow, PermissionResultDeny, ResultMessage, TextBlock

from agentdesk import core, server


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, server.LOOP).result(5)


PLAN = [
    {"title": "Add the parser", "goal": "Parse the config. Done when tests pass.", "files": ["cfg.py"], "tier": "standard"},
    {"title": "Use the parser", "goal": "Call it from main.", "files": ["main.py"], "depends": ["d1"]},
]


class FakeRunner:
    """Records starts instead of spawning `python -m agentdesk run …`."""

    def __init__(self):
        self.started, self.stopped, self.busy = [], 0, False

    def install(self, monkeypatch):
        monkeypatch.setattr(server.Runner, "start", lambda _r, args: self.started.append(args) or setattr(self, "busy", True))
        monkeypatch.setattr(server.Runner, "running", lambda _r: self.busy)
        monkeypatch.setattr(server.Runner, "stop", lambda _r: None if self.busy and not setattr(self, "busy", False) else "nothing is running")


@pytest.fixture
def proj(project, monkeypatch):
    core.set_chat_mode(project, "planner")
    core.save_account_usage(project.account["name"], {})  # no usage readings left over from other tests
    monkeypatch.setattr(server, "PLAN_POLL_S", 3600)  # tests call plan_tick themselves
    return project


@pytest.fixture
def runner(monkeypatch):
    r = FakeRunner()
    r.install(monkeypatch)
    return r


@pytest.fixture
def chat(proj):
    c = server.Chat(server.LOOP, proj)
    yield c
    c.approvals.deny_all()


def notes(chat):
    return [m["text"] for m in chat.history if m.get("kind") == "progress"]


def set_status(p, tid, status, **extra):
    store = core.Store(p)
    state = store.load()
    state["tasks"].setdefault(tid, {}).update(status=status, **extra)
    store.save(state)


# ----------------------------------------------------------------------------- saving while someone reads

def test_save_waits_for_a_reader(proj):
    """On Windows a rename onto an open file fails; the plan watcher and the dashboard read state.json
    while a run writes it, so the save retries for a moment instead of failing."""
    store = core.Store(proj)
    store.save({"tasks": {}, "spent_usd": 0.0})
    f = proj.path("state.json").open("rb")
    threading.Timer(0.3, f.close).start()
    store.save({"tasks": {"001": {"status": "done"}}, "spent_usd": 1.0})
    assert store.load()["tasks"]["001"]["status"] == "done"
    assert not list(proj.dir.glob("*.tmp"))


# ----------------------------------------------------------------------------- settings and mode

def test_planner_settings_and_mode(proj, monkeypatch):
    s = tomllib.loads(core.DEFAULT_SETTINGS)["chat"]["planner"]
    assert s == {"model": "sonnet", "effort": "medium", "budget_usd": 2.0}
    shipped = tomllib.loads((Path(__file__).resolve().parent.parent / "data" / "settings.toml").read_text(encoding="utf-8"))
    assert shipped["chat"]["planner"] == s and shipped["chat"]["coworker"]["budget_usd"] == 2.0
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"model": "haiku"}})  # an older settings.toml
    cs = core.chat_settings("planner")
    assert (cs["model"], cs["effort"], cs["budget_usd"]) == ("sonnet", "medium", 2.0)
    monkeypatch.undo()
    assert core.chat_mode(core.project(proj.slug)) == "planner"
    assert tomllib.loads(proj.path("project.toml").read_text(encoding="utf-8"))["chat_mode"] == "planner"


# ----------------------------------------------------------------------------- drafts are not in the queue

def test_drafts_are_not_tasks(proj, monkeypatch):
    core.add_task(proj, "001", "Existing", "x")
    plan = core.propose_plan(proj, "Config parser", PLAN)
    assert plan["status"] == "draft" and [t["key"] for t in plan["tasks"]] == ["d1", "d2"]
    files = sorted(f.name for f in core.drafts_dir(proj).glob("*.md"))
    assert files == ["d1.md", "d2.md"]
    text = (core.drafts_dir(proj) / "d2.md").read_text(encoding="utf-8")
    assert 'status = "draft"' in text and 'depends = ["d1"]' in text and "main.py" in text
    assert [t.id for t in core.load_tasks(proj)] == ["001"]  # Run next / Run all only see this
    assert core.next_task_id(proj) == "002"
    # A draft-marked file directly in tasks/ is skipped too.
    (proj.tasks_dir / "050.md").write_text('+++\nid = "050"\ntitle = "Draft"\nstatus = "draft"\n+++\n\nx\n', encoding="utf-8")
    assert [t.id for t in core.load_tasks(proj)] == ["001"]

    ran = []

    async def fake_do_task(_p, _store, task, _meter):
        ran.append(task.id)

    monkeypatch.setattr(core, "do_task", fake_do_task)
    asyncio.run(core.run_queue(proj))  # Run all
    asyncio.run(core.run_queue(proj, max_tasks=1))  # Run next
    asyncio.run(core.run_queue(proj, only="d1"))  # a draft by id
    assert ran == ["001", "001"]


def test_run_tools_refuse_draft_ids(chat, runner):
    core.propose_plan(chat.p, "", PLAN)
    t = next(t for t in chat.tools() if t.name == "run_task")
    r = run(t.handler({"id": "d1"}))
    assert r.get("is_error") and runner.started == []


def test_plan_checks(proj):
    with pytest.raises(ValueError, match="at least one"):
        core.propose_plan(proj, "", [])
    with pytest.raises(ValueError, match="depends on 'd9'"):
        core.propose_plan(proj, "", [{"title": "a", "goal": "x", "depends": ["d9"]}])
    with pytest.raises(ValueError, match="cycle"):
        core.propose_plan(proj, "", [{"title": "a", "goal": "x", "depends": ["d2"]}, {"title": "b", "goal": "y", "depends": ["d1"]}])
    with pytest.raises(ValueError, match="tier"):
        core.propose_plan(proj, "", [{"title": "a", "goal": "x", "tier": "huge"}])
    assert core.load_plan(proj) is None and not core.drafts_dir(proj).exists()
    with pytest.raises(ValueError, match="no draft"):
        core.update_plan(proj, None, PLAN)
    core.propose_plan(proj, "", PLAN)
    with pytest.raises(ValueError, match="update_plan"):
        core.propose_plan(proj, "", PLAN)


# ----------------------------------------------------------------------------- edit and cancel

def test_edit_updates_drafts(proj):
    core.propose_plan(proj, "first", PLAN)
    plan = core.update_plan(proj, None, [{"title": "Only one", "goal": "All of it.", "tier": "mechanical"}])
    assert plan["version"] == 2 and plan["summary"] == "first"
    assert sorted(f.name for f in core.drafts_dir(proj).glob("*.md")) == ["d1.md"]
    assert "Only one" in (core.drafts_dir(proj) / "d1.md").read_text(encoding="utf-8")
    v = core.plan_view(proj)
    assert [(t["id"], t["status"]) for t in v["tasks"]] == [("d1", "draft")]
    assert v["estimate"][0] > 0 and v["estimate"][1] >= 1.0  # mechanical budget 1.0 + router


def test_cancel_removes_drafts(chat, runner):
    core.add_task(chat.p, "001", "Existing", "x")
    core.propose_plan(chat.p, "", PLAN)
    assert chat.cancel_plan() == {"ok": True, "removed": 2}
    assert not core.drafts_dir(chat.p).exists() and core.load_plan(chat.p) is None
    assert [t.id for t in core.load_tasks(chat.p)] == ["001"] and runner.started == []
    assert notes(chat) == ["Plan cancelled: 2 draft task(s) removed."]


# ----------------------------------------------------------------------------- approve and start

def test_approve_starts_queue_in_dependency_order(chat, runner):
    p = chat.p
    core.add_task(p, "001", "Existing", "x")
    # d1 depends on d2 and on the queued 001: d2 must get the lower id so the single Run all pass meets it first.
    core.propose_plan(p, "Two steps", [{"title": "Second", "goal": "b", "depends": ["d2", "001"]}, {"title": "First", "goal": "a", "tier": "hard"}])
    assert runner.started == []  # nothing runs before Approve
    out = chat.approve_plan()
    assert out["ids"] == {"d2": "002", "d1": "003"}
    tasks = {t.id: t for t in core.load_tasks(p)}
    assert tasks["002"].title == "First" and tasks["002"].tier == "hard"
    assert tasks["003"].depends == ["002", "001"]
    assert not core.drafts_dir(p).exists()
    assert runner.started == [["run", p.slug]]  # the same command as "Run all"
    plan = core.load_plan(p)
    assert plan["status"] == "approved" and plan["was_running"]
    assert notes(chat)[0] == "Plan approved: tasks 002, 003 are queued." and "Started the queue" in notes(chat)[1]
    assert [t["id"] for t in core.plan_view(p)["tasks"]] == ["002", "003"]
    with pytest.raises(ValueError, match="no draft"):
        chat.approve_plan()
    # One approved plan per project at a time.
    with pytest.raises(ValueError, match="one approved plan"):
        core.propose_plan(p, "", PLAN)
    with pytest.raises(ValueError, match="approved"):
        core.cancel_plan(p)


def test_progress_notes_and_blocked(chat, runner):
    p = chat.p
    core.propose_plan(p, "", PLAN)
    chat.approve_plan()
    chat.history.clear()
    set_status(p, "001", "routing")
    assert chat.plan_tick()
    set_status(p, "001", "working")
    chat.plan_tick()
    assert notes(chat) == ["▶ Started 001: Add the parser"]
    set_status(p, "001", "done", commit="abc123")
    set_status(p, "002", "blocked", last_failure="$ pytest\nFAILED test_main.py")
    runner.busy = False  # the run ended
    chat.plan_tick()
    n = notes(chat)
    assert n[1] == "✓ Done 001: Add the parser (branch auto/001 at abc123)"
    assert n[2].startswith("✗ Blocked 002: Use the parser — $ pytest") and "waits" in n[2]
    assert n[3].startswith("The run ended: 1 of 2") and "002 (blocked)" in n[3]
    assert core.load_plan(p)["status"] == "approved" and runner.started == [["run", p.slug]]  # waits for the user
    chat.plan_tick()
    assert len(notes(chat)) == 4  # nothing new, nothing repeated
    # The user fixes it and presses Start; the plan finishes.
    set_status(p, "002", "pending")
    chat.start_plan()
    assert runner.started[-1] == ["run", p.slug]
    set_status(p, "002", "done")
    assert chat.plan_tick() is False
    assert notes(chat)[-1].startswith("Plan finished: all 2 task(s) done")
    assert core.load_plan(p)["status"] == "finished"
    chat.close_plan()
    assert core.load_plan(p) is None and [t.id for t in core.load_tasks(p)] == ["001", "002"]


def test_approve_while_a_run_is_going_waits(chat, runner):
    runner.busy = True
    core.propose_plan(chat.p, "", PLAN)
    chat.approve_plan()
    assert runner.started == [] and "start when it ends" in notes(chat)[-1]
    chat.plan_tick()
    assert runner.started == []
    runner.busy = False
    chat.plan_tick()
    assert runner.started == [["run", chat.p.slug]] and "Started the queue" in notes(chat)[-1]


def test_approve_while_paused_does_not_start(chat, runner):
    core.save_account_usage(chat.p.account["name"], {"rate_limits": {"five_hour": {
        "utilization": 0.9, "status": "allowed_warning", "resets_at": int(time.time()) + 3600, "seen": "2026-01-01T00:00:00"}}})
    core.propose_plan(chat.p, "", PLAN)
    chat.approve_plan()
    assert runner.started == [] and notes(chat)[-1].startswith("Paused:") and "90%" in notes(chat)[-1]
    assert [t.id for t in core.load_tasks(chat.p)] == ["001", "002"]  # approved and queued, not started
    with pytest.raises(ValueError):
        chat.stop_plan()  # nothing is running


def test_stop_needs_no_approval(chat, runner):
    core.propose_plan(chat.p, "", PLAN)
    chat.approve_plan()
    with pytest.raises(ValueError, match="stop it first"):
        chat.close_plan()
    assert chat.stop_plan() == {"ok": True} and not runner.busy
    assert chat.approvals.pending() == []
    chat.plan_tick()
    assert notes(chat)[-1].startswith("The run ended: 0 of 2")


# ----------------------------------------------------------------------------- permissions and the session

def test_planner_permissions(chat):
    p = chat.p
    ok = lambda name, args: isinstance(run(chat.planner_permission(name, args)), PermissionResultAllow)  # noqa: E731
    assert ok("Read", {"file_path": str(p.repo / "a.py")}) and ok("Grep", {"pattern": "x"}) and ok("Glob", {"pattern": "*"})
    for t in server.PLANNER_TOOLS:
        assert ok(server.MCP_PREFIX + t, {})
    for name, args in [("Write", {"file_path": str(p.dir / "tasks" / "009.md")}), ("Edit", {"file_path": str(p.repo / "a.py")}),
                       ("Bash", {"command": "git status"}), ("PowerShell", {"command": "dir"}), ("Bash", {"command": "git push"}),
                       (server.MCP_PREFIX + "run_all", {}), (server.MCP_PREFIX + "add_task", {}), ("WebFetch", {"url": "x"})]:
        r = run(chat.planner_permission(name, args))
        assert isinstance(r, PermissionResultDeny) and "Planner" in r.message, name
    hook = run(chat.planner_hook({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}))
    assert hook["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run(chat.planner_hook({"tool_name": "Read", "tool_input": {}})) == {}
    assert chat.approvals.pending() == []


def test_planner_tools(chat, runner):
    tools = {t.name: t for t in chat.planner_tools()}
    assert set(tools) == set(server.PLANNER_TOOLS)
    r = run(tools["propose_plan"].handler({"summary": "Parser", "tasks": PLAN}))
    text = r["content"][0]["text"]
    assert not r.get("is_error") and "d2 Use the parser" in text and "after d1" in text and "Approve" in text
    r = run(tools["propose_plan"].handler({"tasks": PLAN}))
    assert r.get("is_error") and "update_plan" in r["content"][0]["text"]
    r = run(tools["update_plan"].handler({"tasks": PLAN[:1]}))
    assert not r.get("is_error") and "v2" in r["content"][0]["text"]
    r = run(tools["cancel_plan"].handler({}))
    assert "1 draft" in r["content"][0]["text"] and core.load_plan(chat.p) is None
    assert runner.started == []


class FakeClient:
    made: list = []
    script: list = []

    def __init__(self, options):
        self.options = options
        FakeClient.made.append(self)

    async def connect(self):
        pass

    async def query(self, text):
        self.text = text

    async def receive_response(self):
        for m in FakeClient.script:
            yield m

    async def interrupt(self):
        pass

    async def disconnect(self):
        pass


def result(total, subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=subtype != "success", num_turns=1, session_id="s1",
                         total_cost_usd=total, usage={"input_tokens": 10, "output_tokens": 5})


@pytest.fixture
def fake(monkeypatch):
    FakeClient.made, FakeClient.script = [], []
    monkeypatch.setattr(server, "ClaudeSDKClient", FakeClient)
    return FakeClient


def test_planner_session(chat, fake):
    fake.script = [AssistantMessage(content=[TextBlock(text="Here's the plan")], model="sonnet"), result(0.2)]
    out = chat.ask("Add a config parser")
    assert out["text"] == "Here's the plan" and out["model"] == "sonnet"
    [c] = fake.made
    o = c.options
    assert (o.model, o.effort, o.max_budget_usd, o.cwd) == ("sonnet", "medium", 2.0, str(chat.p.dir))
    assert o.can_use_tool == chat.planner_permission and o.setting_sources == []
    assert set(o.allowed_tools) == {"Read", "Glob", "Grep"}
    assert {"Bash", "PowerShell", "Write", "Edit"} <= set(o.disallowed_tools)
    assert o.hooks["PreToolUse"][0].hooks == [chat.planner_hook]
    assert c.text.startswith("[Plan: no plan]") and c.text.endswith("Add a config parser")
    assert chat.history[-2] == {"role": "user", "text": "Add a config parser"}
    fake.script = [result(2.3, subtype="error_max_budget_usd")]
    out = chat.ask("more")
    assert "Planner budget of $2.00" in out["text"] and fake.made[1].options.resume == "s1"


def test_planner_paused(chat, fake):
    core.save_account_usage(chat.p.account["name"], {"rate_limits": {"five_hour": {
        "utilization": 0.95, "status": "allowed_warning", "resets_at": int(time.time()) + 3600, "seen": "2026-01-01T00:00:00"}}})
    out = chat.ask("plan it")
    assert out["text"].startswith("Paused:") and "The Planner" in out["text"] and fake.made == []


# ----------------------------------------------------------------------------- HTTP

def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def test_http_plan_card(proj, runner):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/api/p/{proj.slug}"
    try:
        core.set_chat_mode(proj, "assistant")
        assert post(base + "/chat/mode", {"mode": "planner"}) == {"ok": True, "mode": "planner"}
        info = get(base + "/chat")
        assert info["mode"] == "planner" and info["budget_usd"] == 2.0 and "worktree" not in info
        st = get(base + "/state")
        assert st["plan"] is None and st["project"]["chat_model"] == "sonnet (medium)"
        assert "error" in post(base + "/plan/approve", {})
        core.propose_plan(proj, "Parser", PLAN)
        st = get(base + "/state")
        assert st["plan"]["status"] == "draft" and [t["id"] for t in st["plan"]["tasks"]] == ["d1", "d2"]
        assert st["tasks"] == []  # drafts aren't in the task table
        r = post(base + "/plan/approve", {})
        assert r["ok"] and r["ids"] == {"d1": "001", "d2": "002"} and runner.started == [["run", proj.slug]]
        st = get(base + "/state")
        assert st["plan"]["status"] == "approved" and [t["id"] for t in st["tasks"]] == ["001", "002"] and st["chat_seq"] >= 2
        assert [m["text"] for m in get(base + "/chat")["history"] if m.get("kind") == "progress"][0].startswith("Plan approved")
        assert post(base + "/plan/stop", {}) == {"ok": True}
        assert post(base + "/plan/close", {}) == {"ok": True} and get(base + "/state")["plan"] is None
        core.propose_plan(proj, "Again", PLAN)
        assert post(base + "/plan/cancel", {})["removed"] == 2
        assert "error" in post(base + "/plan/bogus", {})
    finally:
        srv.shutdown()
    html = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")
    assert 'data-mode="planner"' in html and "Approve and start" in html and 'id="plan"' in html
