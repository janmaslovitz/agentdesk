"""server.py reads agentdesk/chat_agents.py: project overrides reach the ClaudeAgentOptions of every mode,
allowed_tools never grow, the live Coworker's subagents come from the config, and the /chat/agents routes work."""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

from agentdesk import chat_agents as ca
from agentdesk import core, server


def write_chat_tables(p, text: str) -> None:
    path = p.path("project.toml")
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n\n" + text + "\n", encoding="utf-8")


@pytest.fixture
def proj(project):
    core.set_chat_mode(project, "coworker")
    return project


@pytest.fixture
def chat(proj):
    c = server.Chat(server.LOOP, proj)
    yield c
    c.approvals.deny_all()


def coworker_settings(monkeypatch, **extra):
    orig = core.chat_settings
    monkeypatch.setattr(core, "chat_settings", lambda mode: {**orig(mode), **(extra if mode == "coworker" else {})})


@pytest.fixture
def full_access(monkeypatch):
    coworker_settings(monkeypatch, full_access=True, live=False)


@pytest.fixture
def guarded(monkeypatch):
    coworker_settings(monkeypatch, full_access=False)


@pytest.fixture
def live(monkeypatch):
    coworker_settings(monkeypatch, full_access=True, live=True)


# ----------------------------------------------------------------------------- the options carry the project's values

def test_assistant_options_follow_the_project(chat, project):
    base = chat._assistant_options()
    assert isinstance(base, ClaudeAgentOptions) and base.max_turns == ca.mode_settings(project, "assistant")["max_turns"]
    write_chat_tables(project, '[chat.assistant]\nmodel = "opus"\neffort = "high"\nmax_turns = 7\ntools_off = ["Grep"]\n'
                               'prompt_extra = "Answer in haiku."')
    o = chat._assistant_options()
    assert (o.model, o.effort, o.max_turns) == ("opus", "high", 7) and chat.model == "opus"
    assert "Answer in haiku." in o.system_prompt and ca.EXTRA_HEADING in o.system_prompt
    assert o.allowed_tools == ["Read", "Glob"]  # a tool switched off leaves the allowed list...
    assert "Grep" in o.disallowed_tools  # ...and joins the disallowed one
    assert o.max_budget_usd is None  # the assistant has no budget
    assert set(base.disallowed_tools) <= set(o.disallowed_tools)


def test_planner_options_follow_the_project(chat, project):
    write_chat_tables(project, '[chat.planner]\nmodel = "claude-opus-4-1"\nbudget_usd = 3.5\nmax_turns = 11\n'
                               'tools_off = ["WebFetch"]\nprompt_extra = "Prefer small tasks."')
    o = chat._planner_options()
    assert (o.model, o.max_budget_usd, o.max_turns) == ("claude-opus-4-1", 3.5, 11)
    assert "Prefer small tasks." in o.system_prompt and "$3.50" in o.system_prompt
    assert o.allowed_tools == ["Read", "Glob", "Grep"]
    for t in ("Bash", "Write", "Edit", "MultiEdit", "Agent", "WebFetch"):
        assert t in o.disallowed_tools
    assert o.can_use_tool == chat.planner_permission and "PreToolUse" in o.hooks


def test_guarded_coworker_options_follow_the_project(chat, project, guarded):
    write_chat_tables(project, '[chat.coworker]\nmodel = "opus"\nbudget_usd = 1.25\nmax_turns = 9\ntools_off = ["run_all", "Bash"]\n'
                               'prompt_extra = "Mind the tests."')
    o = chat._coworker_options()
    assert (o.model, o.max_budget_usd, o.max_turns) == ("opus", 1.25, 9)
    assert "Mind the tests." in o.system_prompt and "$1.25" in o.system_prompt
    assert o.allowed_tools == ["Read", "Glob", "Grep"]
    assert {"Agent", "WebFetch", "WebSearch", "NotebookEdit", "Bash", "mcp__agentdesk__run_all"} <= set(o.disallowed_tools)
    assert "PreToolUse" in o.hooks and o.can_use_tool == chat.coworker_permission


def test_full_access_coworker_options_follow_the_project(chat, project, full_access):
    assert chat._coworker_options().disallowed_tools == []
    write_chat_tables(project, '[chat.coworker]\nmax_turns = 12\nbudget_usd = 2\ntools_off = ["WebSearch"]')
    o = chat._coworker_options()
    assert (o.max_turns, o.max_budget_usd) == (12, 2.0)
    assert o.allowed_tools == ["Read", "Glob", "Grep"] and o.disallowed_tools == ["WebSearch"]


def test_live_options_follow_the_project(chat, project, live):
    write_chat_tables(project, '[chat.coworker]\nmodel = "opus"\nbudget_usd = 1.5\ndaily_budget_usd = 7\nmax_turns = 13\n'
                               'tools_off = ["Edit"]\nprompt_extra = "Live extra."')
    assert chat.is_live()
    o = chat._live_options()
    assert (o.model, o.max_turns, o.max_budget_usd) == ("opus", 13, 7.0)
    assert "Live extra." in o.system_prompt and "$1.50 per burst" in o.system_prompt and "$7.00 per day" in o.system_prompt
    assert o.allowed_tools == ["Read", "Glob", "Grep"]
    assert "Edit" in o.disallowed_tools
    assert not {"Bash", "Write"} & set(o.disallowed_tools)  # full access: nothing else is taken away


def test_no_daily_cap_is_not_printed_as_zero_dollars(chat, project, live):
    write_chat_tables(project, "[chat.coworker]\ndaily_budget_usd = 0")
    o = chat._live_options()
    assert "$0.00 per day" not in o.system_prompt and "no daily cap" in o.system_prompt
    assert o.max_budget_usd is None


def test_allowed_tools_never_grow(chat, project, live):
    """Whatever the project writes (also keys and tools it can't know), allowed_tools stays within Read/Glob/Grep."""
    for table in ("assistant", "planner", "coworker"):
        write_chat_tables(project, f'[chat.{table}]\ntools_off = ["Bash"]\nallowed_tools = ["Bash", "Write"]')
    for build in (chat._assistant_options, chat._planner_options, chat._coworker_options, chat._live_options):
        assert set(build().allowed_tools) <= {"Read", "Glob", "Grep"}


@pytest.mark.parametrize("fixture", ["guarded", "full_access", "live"])
def test_allowed_tools_exactly_as_before_without_overrides(request, chat, fixture):
    request.getfixturevalue(fixture)
    for build in (chat._assistant_options, chat._planner_options, chat._coworker_options, chat._live_options):
        assert build().allowed_tools == ["Read", "Glob", "Grep"]


def test_read_only_modes_keep_their_read_only_sets(chat):
    a, pl = chat._assistant_options(), chat._planner_options()
    assert a.can_use_tool == chat.permission and pl.can_use_tool == chat.planner_permission
    assert {"MultiEdit", "Agent", "WebFetch"} <= set(a.disallowed_tools)
    assert {"Bash", "Write", "Edit", "MultiEdit"} <= set(pl.disallowed_tools)


def test_guarded_coworker_keeps_agent_disallowed(chat, project, guarded):
    write_chat_tables(project, "[chat.coworker]\ntools_off = []\n")
    assert "Agent" in chat._coworker_options().disallowed_tools


# ----------------------------------------------------------------------------- the live Coworker's subagents

def test_live_agents_come_from_the_config(proj):
    agents = server.live_agents(proj)
    assert set(agents) == set(ca.AGENT_NAMES) and all(isinstance(a, AgentDefinition) for a in agents.values())
    assert agents["implementer"].tools and "Agent" not in agents["implementer"].tools
    assert agents["reviewer"].maxTurns == ca.AGENT_DEFAULTS["reviewer"]["max_turns"]
    write_chat_tables(proj, '[chat.agents.explorer]\nenabled = false\n\n[chat.agents.reviewer]\nmodel = "opus"\nmax_turns = 5\n'
                            'prompt_extra = "Be strict."')
    agents = server.live_agents(proj)
    assert "explorer" not in agents
    assert agents["reviewer"].model == "opus" and agents["reviewer"].maxTurns == 5 and "Be strict." in agents["reviewer"].prompt


def test_disabled_agent_is_absent_from_options_and_prompt(chat, project, live):
    write_chat_tables(project, "[chat.agents.explorer]\nenabled = false")
    o = chat._live_options()
    assert "explorer" not in o.agents and {"reviewer", "implementer"} <= set(o.agents)
    assert '"explorer"' not in o.system_prompt and '"reviewer"' in o.system_prompt


def test_only_configured_subagents_can_be_started(chat, project, live):
    assert [f"Agent({n})" for n in server.BUILTIN_AGENTS] == chat._live_options().disallowed_tools
    for n in ca.AGENT_NAMES:
        write_chat_tables(project, f"[chat.agents.{n}]\nenabled = false")
    o = chat._live_options()
    assert o.agents == {} and "Agent" in o.disallowed_tools and "Subagents" not in o.system_prompt


def test_reviewer_shell_is_read_only():
    denied = ["git commit -am x", "git reset --hard", "git -C . checkout main", "echo x > f.txt", "rm -rf build", "sed -i s/a/b/ f",
              "git " + "push origin main", "cat a | tee b"]
    for cmd in denied:
        assert server.reviewer_shell_denied(cmd), cmd
    for cmd in ("git diff main...auto/001", "git log --oneline -5", "python -m pytest -q 2>&1", "git show HEAD:a.py", "ls > /dev/null"):
        assert server.reviewer_shell_denied(cmd) is None, cmd


def test_subagent_hook_only_limits_the_reviewer(chat):
    def hook(**data):
        return asyncio.run_coroutine_threadsafe(chat.subagent_hook(data), server.LOOP).result(3)

    commit = {"tool_name": "Bash", "tool_input": {"command": "git commit -am x"}}
    assert hook(**commit, agent_type="reviewer")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook(**commit) == {}  # the Coworker itself
    assert hook(**commit, agent_type="implementer") == {}
    assert hook(tool_name="Bash", tool_input={"command": "git diff"}, agent_type="reviewer") == {}


# ----------------------------------------------------------------------------- prompts match the modes' tools

def test_prompts_do_not_promise_what_the_mode_lacks(chat, guarded, monkeypatch):
    monkeypatch.setattr(ca, "IS_WINDOWS", False)
    for o in (chat._assistant_options(), chat._planner_options(), chat._coworker_options()):
        assert "PowerShell" not in o.system_prompt
    guarded_prompt = chat._coworker_options().system_prompt
    assert "woken with" not in guarded_prompt and "Subagents" not in guarded_prompt
    assert "allow-list" in chat._assistant_options().system_prompt


def test_powershell_is_named_on_windows(chat, monkeypatch):
    monkeypatch.setattr(ca, "IS_WINDOWS", True)
    assert "Bash/PowerShell" in chat._assistant_options().system_prompt


def test_live_prompt_is_the_only_one_that_wakes_the_coworker(chat, live):
    assert 'woken with "[Run finished]"' in chat._live_options().system_prompt
    assert 'woken with "[Run finished]"' not in chat._coworker_options().system_prompt


def test_full_prompt_no_longer_contradicts_itself():
    assert "except the very important commands" in server.COWORKER_FULL_PROMPT


def test_budget_note_names_the_project_setting(project):
    write_chat_tables(project, "[chat.coworker]\nbudget_usd = 1.5")

    class R:
        subtype = "error_max_budget_usd"

    note = server.budget_note(R(), 1.6, "coworker", project)
    assert "$1.50" in note and "project's chat settings" in note


def test_info_shows_the_effective_model(chat, project):
    write_chat_tables(project, '[chat.coworker]\nmodel = "opus"')
    assert chat.info()["model"] == "opus"
    core.set_chat_mode(project, "assistant")
    write_chat_tables(project, '[chat.assistant]\nmodel = "sonnet"')
    assert chat.info()["model"] == "sonnet"


# ----------------------------------------------------------------------------- the routes

@pytest.fixture
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(url, body=None):
    req = urllib.request.Request(url, None if body is None else json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_get_returns_the_config_view(project, http):
    code, out = call(f"{http}/api/p/{project.slug}/chat/agents")
    assert code == 200 and {"modes", "agents", "choices"} <= set(out)
    assert out["modes"]["coworker"]["effective"]["max_turns"] == ca.mode_settings(project, "coworker")["max_turns"]


def test_post_saves_and_resets_an_idle_chat(project, http):
    c = server.chat(project)
    c.session_id = "old-session"
    body = {"modes": {"assistant": {"max_turns": 9}}, "agents": {"explorer": {"enabled": False}}}
    code, out = call(f"{http}/api/p/{project.slug}/chat/agents", body)
    assert code == 200 and out["ok"] and "note" not in out
    assert out["config"]["modes"]["assistant"]["effective"]["max_turns"] == 9
    assert ca.mode_settings(project, "assistant")["max_turns"] == 9 and "explorer" not in ca.agent_definitions(project)
    assert c.session_id is None  # a new session picks the settings up
    assert call(f"{http}/api/p/{project.slug}/chat/agents")[1]["agents"]["explorer"]["project"] == {"enabled": False}


def test_post_while_busy_applies_from_the_next_session(project, http):
    c = server.chat(project)
    c.session_id, c.busy = "keep", True
    try:
        code, out = call(f"{http}/api/p/{project.slug}/chat/agents", {"modes": {"planner": {"max_turns": 8}}})
    finally:
        c.busy = False
    assert code == 200 and out["ok"] and out["note"] == "applies from the next session"
    assert c.session_id == "keep" and ca.mode_settings(project, "planner")["max_turns"] == 8


def test_post_returns_validation_errors_and_saves_nothing(project, http):
    before = project.path("project.toml").read_text(encoding="utf-8")
    code, out = call(f"{http}/api/p/{project.slug}/chat/agents", {"modes": {"coworker": {"budget_usd": 100000}}})
    assert code == 400 and out["ok"] is False and out["error"] and out["field"] == "modes.coworker.budget_usd"
    code, out = call(f"{http}/api/p/{project.slug}/chat/agents", {"modes": {"coworker": {"full_access": True}}})
    assert code == 400 and out["ok"] is False
    assert project.path("project.toml").read_text(encoding="utf-8") == before
