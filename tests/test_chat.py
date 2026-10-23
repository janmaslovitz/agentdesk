"""Chat Assistant mode: approval queue, shell allow-list, chat mode, run tools, HTTP endpoints."""

import asyncio
import json
import threading
import time
import tomllib
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from agentdesk import core, server


def start(coro):
    """Run a coroutine on the server's SDK loop, as the CLI's permission callbacks do."""
    return asyncio.run_coroutine_threadsafe(coro, server.LOOP)


def wait_pending(chat, n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        items = chat.approvals.pending()
        if len(items) >= n:
            return items
        time.sleep(0.01)
    raise AssertionError("no pending request appeared")


@pytest.fixture
def chat(project):
    c = server.Chat(server.LOOP, project)
    yield c
    c.approvals.deny_all()


def toml(p):
    return tomllib.loads(p.path("project.toml").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- approval queue

def test_shell_waits_for_allow_once(chat):
    fut = start(chat.permission("Bash", {"command": "git log -5", "description": "recent commits"}))
    [req] = wait_pending(chat)
    assert req["tool"] == "Bash" and req["detail"] == "git log -5" and req["reason"] == "recent commits"
    assert req["prefix"] == "git log"
    time.sleep(0.1)
    assert not fut.done(), "nothing may run before the user answers"
    assert chat.approvals.resolve(req["id"], "allow_once")
    assert isinstance(fut.result(2), PermissionResultAllow)
    assert chat.approvals.pending() == []
    assert core.allowed_commands(chat.p) == []  # allow once doesn't grow the allow-list
    # ... so the same command asks again
    fut = start(chat.permission("Bash", {"command": "git log -5"}))
    [req] = wait_pending(chat)
    chat.approvals.resolve(req["id"], "deny")
    assert isinstance(fut.result(2), PermissionResultDeny)


def test_always_allow_adds_prefix(chat):
    fut = start(chat.permission("PowerShell", {"command": "git status --short"}))
    [req] = wait_pending(chat)
    chat.approvals.resolve(req["id"], "always_allow")
    assert isinstance(fut.result(2), PermissionResultAllow)
    assert core.allowed_commands(chat.p) == ["git status"]
    assert toml(chat.p)["chat"]["allowed_commands"] == ["git status"]
    # Next time it runs without a card.
    assert isinstance(start(chat.permission("Bash", {"command": "git status"})).result(2), PermissionResultAllow)
    assert chat.approvals.pending() == []
    # But not chained with something else.
    fut = start(chat.permission("Bash", {"command": "git status; rm -rf x"}))
    [req] = wait_pending(chat)
    assert req["prefix"] is None  # no "Always allow" for chained commands
    chat.approvals.resolve(req["id"], "deny")
    assert isinstance(fut.result(2), PermissionResultDeny)


def test_always_allow_with_edited_prefix(chat):
    fut = start(chat.permission("Bash", {"command": "cargo test -p core"}))
    [req] = wait_pending(chat)
    chat.approvals.resolve(req["id"], "always_allow", "cargo test -p")
    assert isinstance(fut.result(2), PermissionResultAllow)
    assert core.allowed_commands(chat.p) == ["cargo test -p"]


def test_always_allow_ignores_prefix_that_does_not_match(chat):
    fut = start(chat.permission("Bash", {"command": "git log"}))
    [req] = wait_pending(chat)
    chat.approvals.resolve(req["id"], "always_allow", "rm")
    assert isinstance(fut.result(2), PermissionResultAllow)  # allowed once
    assert core.allowed_commands(chat.p) == []


def test_deny(chat):
    fut = start(chat.permission("Bash", {"command": "dir"}))
    [req] = wait_pending(chat)
    chat.approvals.resolve(req["id"], "deny")
    r = fut.result(2)
    assert isinstance(r, PermissionResultDeny) and "denied" in r.message
    assert chat.approvals.pending() == []


def test_timeout_denies(chat):
    chat.approvals.timeout = 0.2
    fut = start(chat.permission("Bash", {"command": "dir"}))
    wait_pending(chat)
    r = fut.result(3)
    assert isinstance(r, PermissionResultDeny) and "10 minutes" in r.message
    assert chat.approvals.pending() == []


def test_resolve_unknown_or_bad_action(chat):
    assert chat.approvals.resolve("nope", "deny") is False
    with pytest.raises(ValueError):
        chat.approvals.resolve("nope", "yes please")


def test_reset_denies_pending(chat):
    fut = start(chat.permission("Bash", {"command": "dir"}))
    wait_pending(chat)
    chat.history = [{"role": "user", "text": "hi"}]
    chat.reset(keep_history=True)
    assert isinstance(fut.result(2), PermissionResultDeny)
    assert chat.history == [{"role": "user", "text": "hi"}]
    chat.reset()
    assert chat.history == []


def test_guard_denied_command_is_never_auto_allowed(chat):
    core.allow_command(chat.p, "git")
    assert isinstance(start(chat.permission("Bash", {"command": "git log"})).result(2), PermissionResultAllow)
    fut = start(chat.permission("Bash", {"command": "git push origin main"}))
    [req] = wait_pending(chat)
    chat.approvals.resolve(req["id"], "deny")
    assert isinstance(fut.result(2), PermissionResultDeny)


# ----------------------------------------------------------------------------- other tools

def test_reads_allowed_writes_limited(chat):
    p = chat.p
    assert isinstance(start(chat.permission("Read", {"file_path": str(p.repo / "x.py")})).result(2), PermissionResultAllow)
    ok = start(chat.permission("Write", {"file_path": str(p.dir / "tasks" / "009.md")})).result(2)
    notes = start(chat.permission("Edit", {"file_path": str(p.dir / "notes.md")})).result(2)
    code = start(chat.permission("Write", {"file_path": str(p.repo / "main.py")})).result(2)
    assert isinstance(ok, PermissionResultAllow) and isinstance(notes, PermissionResultAllow)
    assert isinstance(code, PermissionResultDeny)
    assert chat.approvals.pending() == []


def test_add_task_and_queue_status_need_no_approval(chat):
    for t in ("add_task", "queue_status"):
        assert isinstance(start(chat.permission(server.MCP_PREFIX + t, {})).result(2), PermissionResultAllow)
    assert chat.approvals.pending() == []


@pytest.mark.parametrize("name,args", [("run_next", {}), ("run_task", {"id": "001"}), ("run_all", {}), ("plan_task", {"id": "001"}), ("stop_run", {})])
def test_run_tools_need_approval(chat, name, args):
    fut = start(chat.permission(server.MCP_PREFIX + name, args))
    [req] = wait_pending(chat)
    assert req["tool"] == name and req["prefix"] is None
    time.sleep(0.05)
    assert not fut.done()
    chat.approvals.resolve(req["id"], "allow_once")
    assert isinstance(fut.result(2), PermissionResultAllow)


def call_tool(chat, name, args):
    t = next(t for t in chat.tools() if t.name == name)
    r = start(t.handler(args)).result(5)
    return bool(r.get("is_error")), "\n".join(c["text"] for c in r["content"])


def test_tools_call_the_runner(chat, monkeypatch):
    started, stopped = [], []
    monkeypatch.setattr(server.Runner, "start", lambda self, args: started.append(args))
    monkeypatch.setattr(server.Runner, "stop", lambda self: stopped.append(True))
    err, text = call_tool(chat, "add_task", {"title": "Fix it", "body": "Do the thing."})
    assert not err and "001" in text
    assert [t.title for t in core.load_tasks(chat.p)] == ["Fix it"]
    err, text = call_tool(chat, "queue_status", {})
    assert "001 pending" in text and "idle" in text
    slug = chat.p.slug
    call_tool(chat, "run_next", {})
    call_tool(chat, "run_task", {"id": "001"})
    call_tool(chat, "run_all", {})
    call_tool(chat, "plan_task", {"id": "001"})
    assert started == [["run", slug, "--max", "1"], ["run", slug, "001"], ["run", slug], ["plan", slug, "001"]]
    err, text = call_tool(chat, "run_task", {"id": "042"})
    assert err and "no task" in text
    call_tool(chat, "stop_run", {})
    assert stopped == [True]


# ----------------------------------------------------------------------------- allow-list and chat mode in project.toml

def test_command_allowed_prefix_match():
    pre = ["git status", "git log", "cargo test -p"]
    assert core.command_allowed("git status", pre)
    assert core.command_allowed("  git log --oneline -5 ", pre)
    assert core.command_allowed("cargo test -p core", pre)
    assert not core.command_allowed("git statusx", pre)
    assert not core.command_allowed("git push", pre)
    assert not core.command_allowed("cargo test", pre)
    for bad in ("git log; rm -rf /", "git log && del x", "git log | Out-File x", "git log > x", "git log $(rm x)",
                "git log (Remove-Item x)", "git log `\nrm x", "git log\nrm x", "git log & calc"):
        assert not core.command_allowed(bad, pre), bad
    assert not core.command_allowed("", pre)
    assert not core.command_allowed("git log", [])


def test_suggest_prefix():
    assert core.suggest_prefix("git log --oneline -5") == "git log"
    assert core.suggest_prefix("git status") == "git status"
    assert core.suggest_prefix("ls -la") == "ls"
    assert core.suggest_prefix("cat src/main.rs") == "cat"
    assert core.suggest_prefix("git log | head") is None


def test_allow_command_persists(project):
    assert core.allowed_commands(project) == []
    core.allow_command(project, "git status")
    core.allow_command(project, "git log")
    core.allow_command(project, "git log")  # no duplicates
    assert core.allowed_commands(project) == ["git status", "git log"]
    again = core.project(project.slug)  # as after a restart
    assert again.cfg["chat"]["allowed_commands"] == ["git status", "git log"]
    assert again.cfg["repo"]["base"] == "main" and "quick" in again.cfg["gates"]  # the rest is untouched
    with pytest.raises(ValueError):
        core.allow_command(project, "git log; rm x")
    with pytest.raises(ValueError):
        core.allow_command(project, "  ")


def test_allow_command_into_existing_chat_table(project):
    f = project.path("project.toml")
    f.write_text(f.read_text(encoding="utf-8") + '\n[chat]\nnote = "keep"\n\n[other]\nx = 1\n', encoding="utf-8")
    core.allow_command(project, "git diff")
    cfg = toml(project)
    assert cfg["chat"] == {"note": "keep", "allowed_commands": ["git diff"]} and cfg["other"] == {"x": 1}


def test_chat_mode_persists(project):
    assert core.chat_mode(project) == "assistant"
    core.set_chat_mode(project, "coworker")
    assert core.chat_mode(core.project(project.slug)) == "coworker"
    assert toml(project)["chat_mode"] == "coworker"
    core.set_chat_mode(project, "assistant")
    text = project.path("project.toml").read_text(encoding="utf-8")
    assert text.count("chat_mode =") == 1 and toml(project)["chat_mode"] == "assistant"
    with pytest.raises(ValueError):
        core.set_chat_mode(project, "boss")


def test_chat_model_settings(monkeypatch):
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"model": "sonnet"}})
    assert core.chat_settings("assistant")["model"] == "sonnet"  # older settings.toml
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"model": "sonnet", "assistant": {"model": "haiku"}}})
    assert core.chat_settings("assistant")["model"] == "haiku"
    monkeypatch.setattr(core, "settings", lambda: {})
    assert core.chat_settings("assistant")["model"] == "haiku"
    assert tomllib.loads(core.DEFAULT_SETTINGS)["chat"]["assistant"]["model"] == "haiku"


# ----------------------------------------------------------------------------- HTTP

@pytest.fixture
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def test_http_pending_and_mode(project, http):
    c = server.chat(project)
    base = f"{http}/api/p/{project.slug}"
    assert get(base + "/pending") == {"pending": []}
    fut = start(c.permission("Bash", {"command": "git log -3"}))
    wait_pending(c)
    [req] = get(base + "/pending")["pending"]
    assert req["detail"] == "git log -3"
    assert post(f"{base}/pending/{req['id']}", {"action": "always_allow", "prefix": "git log"}) == {"ok": True}
    assert isinstance(fut.result(2), PermissionResultAllow)
    assert core.allowed_commands(project) == ["git log"]
    assert "error" in post(f"{base}/pending/{req['id']}", {"action": "deny"})

    assert get(base + "/chat")["mode"] == "assistant"
    c.history = [{"role": "user", "text": "hello"}]
    assert post(base + "/chat/mode", {"mode": "coworker"})["mode"] == "coworker"
    info = get(base + "/chat")
    assert info["mode"] == "coworker" and info["history"] == [{"role": "user", "text": "hello"}]
    assert get(base + "/state")["project"]["chat_mode"] == "coworker"
    assert "error" in post(base + "/chat/mode", {"mode": "boss"})


# ----------------------------------------------------------------------------- transcript on disk

def test_transcript_survives_restart(project, monkeypatch):
    c = server.Chat(server.LOOP, project)
    answer = {"text": "Ünïcode ✓", "tools": [{"name": "Read", "input": "a.md", "output": "", "error": False}], "cost": 0.01}

    async def fake_ask(text):
        return answer

    monkeypatch.setattr(c, "_ask", fake_ask)
    assert c.ask("hello") == answer
    saved = json.loads(project.path("chat.json").read_text(encoding="utf-8"))
    assert saved == [{"role": "user", "text": "hello"}, {"role": "assistant", **answer}]
    assert "Ünïcode ✓" in project.path("chat.json").read_text(encoding="utf-8")  # ensure_ascii=False

    # a new Chat for the same project (as after a restart) shows the same transcript
    again = server.Chat(server.LOOP, project)
    assert again.history == saved
    assert server.Chat(server.LOOP, project).history == saved


def test_reset_clears_transcript_file(project):
    c = server.Chat(server.LOOP, project)
    c.history.append({"role": "user", "text": "hello"})
    c.save_history()
    c.reset()
    assert c.history == []
    assert not project.path("chat.json").exists()
    assert server.Chat(server.LOOP, project).history == []


def test_mode_switch_keeps_transcript_file(project):
    c = server.Chat(server.LOOP, project)
    c.history.append({"role": "user", "text": "hello"})
    c.save_history()
    c.reset(keep_history=True)
    assert c.history == [{"role": "user", "text": "hello"}]
    assert server.Chat(server.LOOP, project).history == [{"role": "user", "text": "hello"}]


@pytest.mark.parametrize("content", [b"{not json", b"", b'{"role": "user"}', b'"just text"', b"\xff\xfe bad utf-8"])
def test_bad_transcript_file_gives_empty_history(project, content):
    project.path("chat.json").write_bytes(content)
    assert server.Chat(server.LOOP, project).history == []


def test_missing_transcript_file_gives_empty_history(project):
    assert not project.path("chat.json").exists()
    assert server.Chat(server.LOOP, project).history == []


# ----------------------------------------------------------------------------- running memory

def memory_text(fact="Uses SQLite"):
    return "\n".join(f"## {s}\n- {fact if s == 'Decisions' else 'none'}\n" for s in server.MEMORY_SECTIONS)


def fake_chat(project, monkeypatch, replies):
    """A Chat whose model answers "pong" and whose memory call returns the given replies in turn (or raises)."""
    c = server.Chat(server.LOOP, project)
    prompts, queue = [], list(replies)

    async def fake_ask(text):
        return {"text": "pong", "tools": [], "cost": 0.0}

    async def fake_memory(p, model, prompt):
        prompts.append((model, prompt))
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(c, "_ask", fake_ask)
    monkeypatch.setattr(server, "memory_call", fake_memory)
    return c, prompts


def test_memory_injected_into_prompt(chat):
    assert "earlier conversations" not in chat._assistant_options().system_prompt
    chat.save_memory(memory_text("Deploy on Fridays"))
    for opts in (chat._assistant_options(), server.Chat(server.LOOP, chat.p)._assistant_options()):  # also after a restart
        assert server.MEMORY_HEADING in opts.system_prompt and "Deploy on Fridays" in opts.system_prompt
    assert not chat.p.path("chat_memory.tmp").exists()


def test_memory_written_after_ask(project, monkeypatch):
    c, prompts = fake_chat(project, monkeypatch, [memory_text("Uses SQLite")])
    assert c.ask("we use sqlite")["text"] == "pong"
    assert "Uses SQLite" in project.path("chat_memory.md").read_text(encoding="utf-8")
    assert "we use sqlite" in prompts[0][1] and "pong" in prompts[0][1]
    assert "Uses SQLite" not in project.path("chat.json").read_text(encoding="utf-8")  # separate stores


def test_memory_update_gets_old_memory_and_retries_once(project, monkeypatch):
    project.path("chat_memory.md").write_text(memory_text("Old fact"), encoding="utf-8")
    c, prompts = fake_chat(project, monkeypatch, ["", memory_text("New fact")])
    c.ask("hi")
    assert "Old fact" in prompts[0][1] and len(prompts) == 2
    assert "New fact" in project.path("chat_memory.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [["", ""], ["just text", "no headings"], ["## Decisions\nonly one", "x"],
                                 [memory_text("word " * 2000)] * 2, [RuntimeError("boom")]])
def test_bad_memory_update_keeps_old_memory_and_chat_answers(project, monkeypatch, bad):
    old = memory_text("Keep me")
    project.path("chat_memory.md").write_text(old, encoding="utf-8")
    c, _ = fake_chat(project, monkeypatch, bad)
    assert c.ask("hi")["text"] == "pong"
    assert project.path("chat_memory.md").read_text(encoding="utf-8") == old
    assert [m["role"] for m in c.history] == ["user", "assistant"]


def test_failed_ask_does_not_update_memory(project, monkeypatch):
    c, prompts = fake_chat(project, monkeypatch, [memory_text()])

    async def boom(text):
        raise RuntimeError("down")

    monkeypatch.setattr(c, "_ask", boom)
    assert c.ask("hi")["text"].startswith("Error")
    assert prompts == [] and not project.path("chat_memory.md").exists()


def test_reset_clears_memory_but_mode_switch_keeps_it(project):
    c = server.Chat(server.LOOP, project)
    c.save_memory(memory_text())
    c.reset(keep_history=True)
    assert project.path("chat_memory.md").exists()
    c.reset()
    assert not project.path("chat_memory.md").exists()
    assert server.MEMORY_HEADING not in c._assistant_options().system_prompt

# ----------------------------------------------------------------------------- stuck card

def test_answered_card_leaves_the_list_at_once(chat):
    """The page polls right after answering; the card must already be gone even if the SDK loop is busy."""
    fut = start(chat.permission("Bash", {"command": "git log"}))
    [req] = wait_pending(chat)
    release = threading.Event()

    async def block():
        release.wait(5)

    blocker = start(block())  # the loop can't run the waiting ask() yet
    time.sleep(0.05)
    assert chat.approvals.resolve(req["id"], "deny")
    assert chat.approvals.pending() == []
    assert chat.approvals.resolve(req["id"], "deny") is False  # a second click is "not waiting any more"
    assert not fut.done()
    release.set()
    blocker.result(2)
    assert isinstance(fut.result(2), PermissionResultDeny)


def test_reset_clears_cards_at_once(chat):
    start(chat.permission("Bash", {"command": "dir"}))
    wait_pending(chat)
    chat.reset()
    assert chat.approvals.pending() == []


def test_page_rerenders_an_empty_pending_list():
    """Source check only (the JS isn't run by pytest): after an answer the page must reset pendingKey to a
    value an empty poll can't equal, otherwise ""=="" returns early and a stale card stays."""
    html = (server.Path(server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert 'pendingKey = "";' not in html
    assert "let pendingKey = null;" in html and "pendingKey = null; pollPending()" in html


def test_http_answer_leaves_empty_list_and_second_answer_404s(project, http):
    c = server.chat(project)
    base = f"{http}/api/p/{project.slug}"
    fut = start(c.permission("Bash", {"command": "git log -3"}))
    [req] = wait_pending(c)
    assert post(f"{base}/pending/{req['id']}", {"action": "deny"}) == {"ok": True}
    assert get(base + "/pending") == {"pending": []}
    assert isinstance(fut.result(2), PermissionResultDeny)
    assert post(f"{base}/pending/{req['id']}", {"action": "deny"})["error"].startswith("that request isn't waiting")


def test_page_makes_chat_text_copyable():
    """Source check only: selectable bubbles plus a plain-text copy button with an execCommand fallback."""
    html = (server.Path(server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert ".msg, .msg * { user-select: text;" in html
    assert "async function copyText(" in html and 'document.execCommand("copy")' in html
    assert "navigator.clipboard.writeText" in html and "copyButton(() => pre.textContent" in html


def test_task_activity_reads_the_timeline(chat):
    call_tool(chat, "add_task", {"title": "Fix it", "body": "Do the thing."})
    err, text = call_tool(chat, "task_activity", {})
    assert not err and "no agent is working" in text
    act = core.Activity(chat.p, "001", "worker")
    act.add("tool", name="Edit", input="core.py", file="C:/x/core.py")
    act.add("tool_error", name="Bash", text="boom")
    err, text = call_tool(chat, "task_activity", {"id": "001"})
    assert not err and "tool calls: 1" in text and "tool errors: 1" in text and "core.py" in text and "Edit" in text
    err, text = call_tool(chat, "task_activity", {"id": "042"})
    assert err and "no task" in text
