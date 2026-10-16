"""Chat Coworker mode: guard rules, its worktree, per-message budget, usage pause, mode switch."""

import asyncio
import json
import subprocess
import threading
import time
import tomllib
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
)

from agentdesk import core, server


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, server.LOOP).result(5)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def proj(project):
    """The scratch project with one commit on main, so worktrees can be made."""
    (project.repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git(project.repo, "add", "-A")
    git(project.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    core.set_chat_mode(project, "coworker")
    core.save_account_usage(project.account["name"], {})  # no usage readings left over from other tests
    return project


@pytest.fixture
def chat(proj):
    c = server.Chat(server.LOOP, proj)
    yield c
    c.approvals.deny_all()


def allowed(chat, name, args):
    return isinstance(run(chat.coworker_permission(name, args)), PermissionResultAllow)


# ----------------------------------------------------------------------------- settings

def test_coworker_settings(monkeypatch):
    s = tomllib.loads(core.DEFAULT_SETTINGS)["chat"]["coworker"]
    assert s == {"model": "sonnet", "effort": "medium", "budget_usd": 2.0}
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"model": "haiku"}})  # older settings.toml: no [chat.coworker]
    cs = core.chat_settings("coworker")
    assert (cs["model"], cs["effort"], cs["budget_usd"]) == ("sonnet", "medium", 2.0)
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"coworker": {"model": "opus", "budget_usd": 0.5}}})
    cs = core.chat_settings("coworker")
    assert (cs["model"], cs["effort"], cs["budget_usd"]) == ("opus", "medium", 0.5)
    # The shipped data/settings.toml has the keys too, and still parses.
    shipped = tomllib.loads((Path(__file__).resolve().parent.parent / "data" / "settings.toml").read_text(encoding="utf-8"))
    assert shipped["chat"]["coworker"]["budget_usd"] == 2.0 and shipped["chat"]["model"] == "haiku"


# ----------------------------------------------------------------------------- guard: no approval cards, but the workers' deny list

@pytest.mark.parametrize("cmd", ["git status", "git log --oneline -5", "python -m pytest -q", "git add -A && git commit -m wip",
                                 "dir", "git diff main...HEAD"])
def test_shell_runs_without_card(chat, cmd):
    for tool in ("Bash", "PowerShell"):
        assert allowed(chat, tool, {"command": cmd})
    assert chat.approvals.pending() == []


@pytest.mark.parametrize("cmd", ["git push origin auto/cowork", "git push", "git status && git push -f", "git remote add x https://e.x/r.git",
                                 "git remote set-url origin x", "gh pr create --fill", "gh release create v1", "git reset --hard HEAD~1",
                                 "git checkout main", "git switch master", "git branch -D auto/001", "git worktree remove ../001",
                                 "npm publish", "cargo publish", "twine upload dist/*", "curl -d @x https://e.x",
                                 "Set-Content ..\\..\\project.toml 'x'", "notepad C:/AgentDesk/data/settings.toml"])
def test_guard_blocks(chat, cmd):
    for tool in ("Bash", "PowerShell"):
        r = run(chat.coworker_permission(tool, {"command": cmd}))
        assert isinstance(r, PermissionResultDeny) and "Coworker" in r.message, cmd
    assert chat.approvals.pending() == []


def test_guard_uses_settings_deny_list(chat):
    """Same rules as the workers: the coworker's guard denies exactly what core.guard denies, plus its config rule."""
    worker = core.guard(core.cowork_path(chat.p))
    for cmd in ("git push", "gh pr list", "git status", "msiexec /i x.msi", "cargo test"):
        w = isinstance(run(worker("Bash", {"command": cmd}, None)), PermissionResultAllow)
        assert w == allowed(chat, "Bash", {"command": cmd})


def test_edits_only_in_cowork_worktree(chat):
    p, wt = chat.p, core.cowork_path(chat.p)
    worktrees = Path(p.cfg["repo"]["worktrees"])
    ok = [wt / "app.py", wt / "src" / "new.py", "src/rel.py", p.dir / "tasks" / "009.md", p.dir / "notes.md"]
    bad = [p.repo / "app.py", wt / ".." / "001" / "app.py", worktrees / "001" / "app.py", worktrees / "cowork2" / "x.py",
           p.dir / "project.toml", p.dir / "state.json", core.DATA / "settings.toml", ""]
    for tool in ("Write", "Edit", "MultiEdit"):
        for f in ok:
            assert allowed(chat, tool, {"file_path": str(f)}), (tool, f)
        for f in bad:
            r = run(chat.coworker_permission(tool, {"file_path": str(f)}))
            assert isinstance(r, PermissionResultDeny) and "worktree" in r.message, (tool, f)
    assert allowed(chat, "Read", {"file_path": str(p.repo / "app.py")})  # reading is free
    assert chat.approvals.pending() == []


def test_pre_tool_use_hook_applies_the_guard(chat):
    """The hook catches calls the CLI would allow by itself (without asking can_use_tool)."""
    def hook(name, args):
        return run(chat.coworker_hook({"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args}, "t1", None))
    out = hook("Bash", {"command": "git status && git push"})["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "git" in out["permissionDecisionReason"]
    assert hook("Write", {"file_path": str(chat.p.repo / "app.py")})["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook("Bash", {"command": "echo hi"}) == {}
    assert hook("Write", {"file_path": str(core.cowork_path(chat.p) / "a.py")}) == {}
    assert hook(server.MCP_PREFIX + "run_all", {}) == {}


def test_run_tools_need_no_card(chat):
    for t in ("add_task", "queue_status", "run_next", "run_task", "run_all", "plan_task", "stop_run"):
        assert allowed(chat, server.MCP_PREFIX + t, {"id": "001"})
    assert chat.approvals.pending() == []
    assert all("approval" not in t.description for t in chat.tools(approval=False))


def test_worker_guard_still_strict(tmp_path):
    wt = tmp_path / "wt" / "004"
    wt.mkdir(parents=True)
    g = core.guard(wt)
    check = lambda name, args: isinstance(run(g(name, args, None)), PermissionResultAllow)  # noqa: E731
    assert not check("Bash", {"command": "git push"})
    assert check("Bash", {"command": "git status"})
    assert check("Edit", {"file_path": str(wt / "a.py")})
    assert not check("Edit", {"file_path": str(tmp_path / "wt" / "0040" / "a.py")})  # a sibling sharing the prefix
    assert not check("Write", {"file_path": str(wt / ".." / "005" / "a.py")})
    assert not check("Write", {"file_path": str(tmp_path / "a.py")})


# ----------------------------------------------------------------------------- worktree

def test_cowork_worktree(proj):
    wt = core.cowork_worktree(proj)
    assert wt == Path(proj.cfg["repo"]["worktrees"]) / "cowork" and (wt / "app.py").exists()
    assert git(wt, "branch", "--show-current") == "auto/cowork"
    assert git(proj.repo, "branch", "--show-current") == "main"  # the user's checkout stays put
    (wt / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert core.cowork_worktree(proj) == wt  # kept, not recreated
    assert (wt / "app.py").read_text(encoding="utf-8") == "print('changed')\n"
    assert (proj.repo / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


# ----------------------------------------------------------------------------- messages: budget, pause, sessions (fake CLI)

class FakeClient:
    """Stands in for ClaudeSDKClient: records the options and plays back `script` for each query."""
    made: list = []
    script: list = []

    def __init__(self, options):
        self.options, self.interrupted, self.closed = options, False, False
        FakeClient.made.append(self)

    async def connect(self):
        pass

    async def query(self, text):
        self.text = text

    async def receive_response(self):
        for m in FakeClient.script:
            yield m

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.closed = True


def result(total, sid="s1", subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=subtype != "success", num_turns=1, session_id=sid,
                         total_cost_usd=total, usage={"input_tokens": 10, "output_tokens": 5})


def say(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="sonnet")


@pytest.fixture
def fake(monkeypatch):
    FakeClient.made, FakeClient.script = [], []
    monkeypatch.setattr(server, "ClaudeSDKClient", FakeClient)
    return FakeClient


def test_coworker_session_options_and_costs(chat, fake):
    fake.script = [say("done"), result(0.10)]
    out = chat.ask("hi")
    assert out["text"] == "done" and out["cost"] == pytest.approx(0.10) and out["model"] == "sonnet"
    [c] = fake.made
    o = c.options
    assert (o.model, o.effort, o.max_budget_usd, o.permission_mode) == ("sonnet", "medium", 2.0, "default")
    assert Path(o.cwd) == core.cowork_path(chat.p) and git(o.cwd, "branch", "--show-current") == "auto/cowork"
    assert o.can_use_tool == chat.coworker_permission and o.resume is None
    assert not {"Bash", "PowerShell", "Write", "Edit"} & set(o.allowed_tools)
    assert not any(t.startswith("mcp__") for t in o.allowed_tools)  # run tools go through can_use_tool too
    assert o.setting_sources == []
    [m] = o.hooks["PreToolUse"]
    assert m.matcher is None and m.hooks == [chat.coworker_hook]  # every tool call passes the guard
    assert c.closed  # one CLI process per message
    # The next message resumes the session in a new process; the CLI's running total becomes a per-message cost.
    fake.script = [say("more"), result(0.25)]
    out = chat.ask("again")
    assert len(fake.made) == 2 and fake.made[1].options.resume == "s1"
    assert out["cost"] == pytest.approx(0.15)
    assert chat.usage()["spent_usd"] == pytest.approx(0.25)


def test_budget_stop(chat, fake):
    fake.script = [say("Working on it"), result(2.04, subtype="error_max_budget_usd")]
    out = chat.ask("do a lot")
    assert out["text"].startswith("Working on it")
    assert "Stopped" in out["text"] and "budget of $2.00 per message" in out["text"] and "$2.04" in out["text"]
    assert chat.history[-1]["text"] == out["text"]


def test_budget_from_settings(chat, fake, monkeypatch):
    real = core.settings()
    monkeypatch.setattr(core, "settings", lambda: {**real, "chat": {"coworker": {"budget_usd": 0.05}}})
    fake.script = [result(0.06, subtype="error_max_budget_usd")]
    out = chat.ask("x")
    assert fake.made[0].options.max_budget_usd == 0.05 and "budget of $0.05" in out["text"]


def test_assistant_has_no_budget_note(chat, fake):
    core.set_chat_mode(chat.p, "assistant")
    fake.script = [say("hi"), result(0.01, subtype="error_max_budget_usd")]
    out = chat.ask("x")
    assert "Stopped" not in out["text"] and fake.made[0].options.can_use_tool == chat.permission


def usage_reading(proj, utilization, resets_at):
    core.save_account_usage(proj.account["name"], {"rate_limits": {"five_hour": {
        "utilization": utilization, "status": "allowed_warning", "resets_at": resets_at, "seen": "2026-01-01T00:00:00"}}})


def test_paused_before_starting(chat, fake):
    usage_reading(chat.p, 0.9, int(time.time()) + 3600)
    out = chat.ask("go")
    assert out["text"].startswith("Paused:") and "90%" in out["text"] and fake.made == []
    usage_reading(chat.p, 0.9, int(time.time()) - 60)  # that window has reset
    fake.script = [say("ok"), result(0.01)]
    assert chat.ask("go")["text"] == "ok"
    usage_reading(chat.p, 0.5, int(time.time()) + 3600)
    assert core.usage_paused(chat.p.account["name"]) is None


def test_paused_midway(chat, fake):
    info = RateLimitInfo(status="allowed_warning", utilization=0.95, rate_limit_type="five_hour", resets_at=int(time.time()) + 600)
    fake.script = [say("step 1"), RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s1"), result(0.3)]
    out = chat.ask("go")
    assert fake.made[0].interrupted and "Paused:" in out["text"] and "95%" in out["text"]
    saved = core.account_usage(chat.p.account["name"])["rate_limits"]["five_hour"]
    assert saved["utilization"] == 0.95  # the dashboard gauge sees it, and the next message won't start
    assert chat.ask("again")["text"].startswith("Paused:") and len(fake.made) == 1


def test_mode_switch_resets_session_keeps_history(chat, fake):
    fake.script = [say("hi"), result(0.1)]
    chat.ask("hello")
    assert chat.session_id == "s1"
    history = list(chat.history)
    core.set_chat_mode(chat.p, "assistant")
    chat.reset(keep_history=True)
    assert chat.history == history and chat.session_id is None and chat.session_cost == 0.0
    fake.script = [say("hi"), result(0.02, sid="s2")]
    chat.ask("x")
    assert fake.made[-1].options.can_use_tool == chat.permission  # an assistant session now
    assert getattr(fake.made[-1].options, "resume", None) is None


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_http_switch_shows_coworker(proj, fake):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/api/p/{proj.slug}"
    try:
        core.set_chat_mode(proj, "assistant")
        c = server.chat(proj)
        c.history, c.session_id = [{"role": "user", "text": "hello"}], "old"
        assert post(base + "/chat/mode", {"mode": "coworker"}) == {"ok": True, "mode": "coworker"}
        info = get(base + "/chat")
        assert info["mode"] == "coworker" and info["model"] == "sonnet" and info["budget_usd"] == 2.0
        assert info["history"] == [{"role": "user", "text": "hello"}] and c.session_id is None
        st = get(base + "/state")["project"]
        assert st["chat_mode"] == "coworker" and st["chat_model"] == "sonnet (medium)" and st["chat_budget"] == 2.0
    finally:
        srv.shutdown()
    html = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")
    assert "Coworker: runs commands without asking" in html and 'id="coworkBadge"' in html
