"""The live Coworker: one persistent session, streamed entries, background tasks, subagents, wake-ups, daily cap."""

import asyncio
import subprocess
import time

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agentdesk import core, server


class LiveFake:
    """Stands in for ClaudeSDKClient in streaming mode: each query plays back the next batch of `script`."""
    made: list = []
    script: list = []

    def __init__(self, options):
        self.options, self.sent, self.interrupted, self.closed = options, [], False, False
        self.q: asyncio.Queue = asyncio.Queue()
        LiveFake.made.append(self)

    async def connect(self):
        pass

    async def query(self, text):
        self.sent.append(text)
        for m in (LiveFake.script.pop(0) if LiveFake.script else []):
            await self.q.put(m)

    async def receive_messages(self):
        while True:
            m = await self.q.get()
            if m is None:
                return
            yield m

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.closed = True
        await self.q.put(None)


def result(total, sid="s1", subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=subtype != "success", num_turns=1, session_id=sid,
                         total_cost_usd=total, usage={"input_tokens": 10, "output_tokens": 5})


def say(text, parent=None):
    return AssistantMessage(content=[TextBlock(text=text)], model="sonnet", parent_tool_use_id=parent)


def wait(cond, t=4.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def chat(project, monkeypatch):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                   cwd=project.repo, check=True)
    core.set_chat_mode(project, "coworker")
    LiveFake.made, LiveFake.script = [], []
    monkeypatch.setattr(server, "ClaudeSDKClient", LiveFake)
    c = server.Chat(server.LOOP, project)
    yield c
    c.reset()


def entries(chat, kind="live"):
    return [m for m in chat.history if m.get("kind") == kind]


def test_live_is_the_default_for_full_access_coworker(chat):
    assert chat.is_live() and chat.info()["live"] is True


def test_not_live_when_guarded_or_switched_off(chat, monkeypatch):
    orig = core.chat_settings
    monkeypatch.setattr(core, "chat_settings", lambda m: {**orig(m), "full_access": False} if m == "coworker" else orig(m))
    assert not chat.is_live()
    monkeypatch.setattr(core, "chat_settings", lambda m: {**orig(m), "live": False} if m == "coworker" else orig(m))
    assert not chat.is_live()


def test_one_persistent_process_and_entries_stream_in(chat):
    LiveFake.script = [
        [say("I'll look."),
         AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git status"})], model="sonnet"),
         UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="clean", is_error=False)]),
         say("All clean."), result(0.10)],
        [say("Again fine."), result(0.25)],
    ]
    assert chat.start("check git") == {"started": True, "live": True}
    assert wait(lambda: not chat.busy and len(entries(chat)) >= 4)
    texts = [m["text"] for m in entries(chat) if m["text"]]
    assert texts[:2] == ["I'll look.", "All clean."]
    tools = [t for m in entries(chat) for t in m.get("tools", [])]
    assert tools and tools[0]["name"] == "Bash" and tools[0]["output"] == "clean"
    assert all(m["seq"] for m in entries(chat))
    cost = entries(chat)[-1]
    assert cost["cost"] == pytest.approx(0.10)
    chat.start("and now?")
    assert wait(lambda: not chat.busy and any(m["text"] == "Again fine." for m in entries(chat)))
    assert len(LiveFake.made) == 1 and LiveFake.made[0].sent == ["check git", "and now?"]  # the same process
    assert entries(chat)[-1]["cost"] == pytest.approx(0.15)  # the CLI's running total becomes a per-burst cost
    assert chat.usage()["spent_usd"] == pytest.approx(0.25) and chat.daily_spent() == pytest.approx(0.25)


def test_can_write_while_it_works(chat):
    LiveFake.script = [[say("working")], [say("noted"), result(0.2)]]
    chat.start("long job")
    assert wait(lambda: any(m["text"] == "working" for m in entries(chat)))
    assert chat.busy  # no result yet
    assert chat.start("also this").get("live")  # accepted at once, not "still answering"
    assert wait(lambda: not chat.busy)
    assert LiveFake.made[0].sent == ["long job", "also this"]


def test_background_tasks_and_subagents_are_shown(chat):
    LiveFake.script = [[
        TaskStartedMessage(subtype="task_started", data={}, task_id="b1", description="npm run build", uuid="u", session_id="s", tool_use_id="x", task_type="bash"),
        say("explorer found it", parent="tool1"),
        TaskNotificationMessage(subtype="task_notification", data={}, task_id="b1", status="completed", output_file="", summary="build ok",
                                uuid="u2", session_id="s", tool_use_id="x", usage=None),
        result(0.05)]]
    chat.start("go")
    assert wait(lambda: not chat.busy and entries(chat, "progress"))
    notes = [m["text"] for m in entries(chat, "progress")]
    assert any("Background task started: npm run build" in n for n in notes)
    assert any("✓ Background task completed: build ok" in n for n in notes)
    assert any(m["text"].startswith("↳ explorer found it") for m in entries(chat))


def test_a_turn_the_cli_starts_by_itself_marks_it_busy_and_is_shown(chat):
    LiveFake.script = [[result(0.01)]]
    chat.start("hi")
    assert wait(lambda: not chat.busy)
    # later, a finished background job makes the CLI speak without a query
    asyncio.run_coroutine_threadsafe(LiveFake.made[0].q.put(say("the build finished")), server.LOOP).result(2)
    assert wait(lambda: chat.busy and any(m["text"] == "the build finished" for m in entries(chat)))
    asyncio.run_coroutine_threadsafe(LiveFake.made[0].q.put(result(0.02)), server.LOOP).result(2)
    assert wait(lambda: not chat.busy)


def test_daily_cap_blocks_new_work(chat):
    chat._record_usage(30.0, {})
    out = chat.start("more")
    assert "daily budget" in out["error"] and not LiveFake.made
    assert chat.daily_spent() == 30.0


def test_burst_over_budget_and_daily_cap_notes(chat):
    chat._record_usage(24.0, {})
    LiveFake.script = [[say("big job"), result(6.0)]]
    chat.start("go")
    assert wait(lambda: not chat.busy and entries(chat)[-1].get("cost"))
    note = entries(chat)[-1]["text"]
    assert "over the $5.00 per-burst budget" in note and "daily budget of $25.00" in note


def test_spend_limit_error_reconnects_on_next_message(chat):
    LiveFake.script = [[result(1.0, subtype="error_max_budget_usd")], [say("back"), result(0.1, sid="s2")]]
    chat.start("go")
    assert wait(lambda: chat.client is None and not chat.busy)
    chat.start("again")
    assert wait(lambda: any(m["text"] == "back" for m in entries(chat)))
    assert len(LiveFake.made) == 2 and LiveFake.made[1].options.resume == "s1"


def test_inject_wakes_the_session_with_a_note(chat):
    LiveFake.script = [[say("on it"), result(0.01)]]
    chat.inject("[Run finished] all green", "⏱ The run ended")
    assert wait(lambda: LiveFake.made and LiveFake.made[0].sent == ["[Run finished] all green"])
    assert any("The run ended" in m["text"] for m in entries(chat, "progress"))
    assert wait(lambda: not chat.busy)


def test_interrupt_only_when_busy(chat):
    assert chat.interrupt() is False
    LiveFake.script = [[say("working")]]
    chat.start("go")
    assert wait(lambda: chat.busy)
    assert chat.interrupt() is True and wait(lambda: LiveFake.made[0].interrupted)


def test_options_agents_and_tools(chat):
    o = chat._live_options()
    assert set(o.agents) == {"explorer", "reviewer", "implementer"} and o.agents["explorer"].model == "haiku"
    assert o.cwd == str(chat.p.repo) and o.disallowed_tools == [] and o.max_budget_usd == 25.0
    assert o.can_use_tool == chat.coworker_permission
    assert "live session" in o.system_prompt and "$25.00 per day" in o.system_prompt
    names = {t.name for t in chat.tools(approval=False)}
    assert {"checkup", "watch_run", "schedule_checkup", "run_task", "add_task"} <= names
    assert not {"checkup", "watch_run", "schedule_checkup"} & {t.name for t in chat.tools(approval=True)}


def test_checkup_report_covers_the_project(chat):
    r = chat.checkup_report()
    for part in ("Run: idle", "Tasks:", "Needs attention:", "Coworker spending today: $0.00 of $25.00", "Recent log:"):
        assert part in r


def test_watch_run_only_when_a_run_is_going(chat, monkeypatch):
    tool = next(t for t in chat.tools(approval=False) if t.name == "watch_run")
    out = asyncio.run_coroutine_threadsafe(tool.handler({}), server.LOOP).result(3)
    assert "no run is going" in out["content"][0]["text"]
    started = []
    monkeypatch.setattr(server.runner(chat.p).__class__, "running", lambda self: True)
    monkeypatch.setattr(chat, "watch_run_bg", lambda: started.append(1))
    out = asyncio.run_coroutine_threadsafe(tool.handler({}), server.LOOP).result(3)
    assert started and "watching" in out["content"][0]["text"]


def test_schedule_checkup_validates_minutes(chat, monkeypatch):
    tool = next(t for t in chat.tools(approval=False) if t.name == "schedule_checkup")
    got = []
    monkeypatch.setattr(chat, "schedule_checkup", lambda m: got.append(m))
    for bad in ({"minutes": 1}, {"minutes": 5000}, {"minutes": "x"}, {}):
        out = asyncio.run_coroutine_threadsafe(tool.handler(bad), server.LOOP).result(3)
        assert out.get("is_error")
    out = asyncio.run_coroutine_threadsafe(tool.handler({"minutes": 30}), server.LOOP).result(3)
    assert got == [30.0] and not out.get("is_error")


def test_http_routes_expose_live_state_and_interrupt(chat):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    server.CHATS[chat.p.slug] = chat
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/api/p/{chat.p.slug}"
    try:
        st = json.load(urllib.request.urlopen(base + "/state"))
        assert st["chat_live"] is True and st["chat_busy"] is False
        r = json.load(urllib.request.urlopen(urllib.request.Request(base + "/chat/interrupt", data=b"{}", headers={"Content-Type": "application/json"})))
        assert r == {"ok": False}
    finally:
        srv.shutdown()
