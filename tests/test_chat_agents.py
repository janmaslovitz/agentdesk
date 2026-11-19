"""chat_agents: defaults, layering, validation, saving to project.toml, tool lists, prompt_extra."""

import tomllib

import pytest

from agentdesk import chat_agents as ca
from agentdesk import core


def test_module_is_sdk_free():
    import ast

    tree = ast.parse(core.Path(ca.__file__).read_text(encoding="utf-8"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("claude_agent_sdk") for m in mods)


def read_cfg(p):
    return tomllib.loads(p.path("project.toml").read_text(encoding="utf-8"))


def set_settings(monkeypatch, chat: dict):
    """settings.toml's [chat] as `chat` (merged over the real defaults only for the keys given)."""
    real = core.settings

    def fake():
        s = real()
        s = {**s, "chat": {**s.get("chat", {}), **chat}}
        return s

    monkeypatch.setattr(core, "settings", fake)


def append_toml(p, text):
    f = p.path("project.toml")
    f.write_text(f.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + text, encoding="utf-8")
    p.cfg = tomllib.loads(f.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- defaults

def test_defaults_of_the_three_agents(project):
    assert set(ca.AGENT_NAMES) == {"explorer", "reviewer", "implementer"}
    for name in ca.AGENT_NAMES:
        d = ca.AGENT_DEFAULTS[name]
        assert set(d) >= {"description", "prompt", "model", "tools", "max_turns", "effort", "enabled"}
        assert d["effort"] in ca.EFFORTS and d["enabled"] is True and d["max_turns"] > 0
        assert set(d["tools"]) <= set(ca.TOOL_CATALOGUE)
    assert ca.AGENT_DEFAULTS["explorer"]["model"] == "haiku"
    assert ca.AGENT_DEFAULTS["explorer"]["tools"] == ["Read", "Grep", "Glob"]
    assert ca.AGENT_DEFAULTS["reviewer"]["model"] == "sonnet"
    defs = ca.agent_definitions(project)
    assert set(ca.AGENT_NAMES) <= set(defs)
    for d in defs.values():
        assert set(d) == {"description", "prompt", "tools", "model", "maxTurns", "effort"}


def test_mode_defaults(project):
    assert ca.mode_settings(project, "assistant")["max_turns"] == 40
    assert core.chat_settings("assistant")["max_turns"] == 40
    c = ca.mode_settings(project, "coworker")
    assert c["budget_usd"] == 5.0 and c["max_turns"] == 60 and c["tools_off"] == [] and c["prompt_extra"] == ""
    assert ca.mode_settings(project, "planner")["budget_usd"] == 2.0
    assert ca.mode_settings(project, "assistant")["model"] == "haiku"
    with pytest.raises(ValueError):
        ca.mode_settings(project, "nope")


def test_implementer_has_no_agent_or_web_tools(project):
    tools = ca.agent_settings(project, "implementer")["tools"]
    assert tools and not {"Agent", "Task", "WebFetch", "WebSearch"} & set(tools)
    assert not {"Agent", "Task", "WebFetch", "WebSearch"} & set(ca.AGENT_DEFAULTS["implementer"]["tools"])
    assert not {"Agent", "Task"} & set(ca.TOOL_CATALOGUE)
    assert not any(t.startswith("mcp__") for t in tools)


def test_reviewer_is_told_not_to_edit_and_has_no_edit_tools():
    r = ca.AGENT_DEFAULTS["reviewer"]
    assert not {"Edit", "Write", "MultiEdit", "NotebookEdit"} & set(r["tools"])
    assert "shell" in r["prompt"] and "Never create, edit or delete" in r["prompt"]


def test_powershell_only_on_windows(project, monkeypatch):
    monkeypatch.setattr(ca, "IS_WINDOWS", False)
    assert "PowerShell" not in ca.agent_settings(project, "reviewer")["tools"]
    assert "Bash" in ca.agent_settings(project, "reviewer")["tools"] and ca.shell_tools() == ("Bash",)
    monkeypatch.setattr(ca, "IS_WINDOWS", True)
    assert "PowerShell" in ca.agent_settings(project, "reviewer")["tools"]
    assert ca.shell_tools() == ("Bash", "PowerShell")


# ----------------------------------------------------------------------------- layers

def test_agent_precedence_defaults_settings_project(project, monkeypatch):
    assert ca.agent_settings(project, "explorer")["model"] == "haiku"
    set_settings(monkeypatch, {"agents": {"explorer": {"model": "sonnet", "max_turns": 12}}})
    s = ca.agent_settings(project, "explorer")
    assert s["model"] == "sonnet" and s["max_turns"] == 12
    append_toml(project, '[chat.agents.explorer]\nmodel = "opus"\n')
    s = ca.agent_settings(project, "explorer")
    assert s["model"] == "opus" and s["max_turns"] == 12 and s["effort"] == "low"


def test_mode_precedence_settings_then_project(project, monkeypatch):
    set_settings(monkeypatch, {"coworker": {**core.settings()["chat"]["coworker"], "budget_usd": 7.5}})
    assert ca.mode_settings(project, "coworker")["budget_usd"] == 7.5
    append_toml(project, '[chat.coworker]\nbudget_usd = 3\nmax_turns = 20\nfull_access = false\n')
    s = ca.mode_settings(project, "coworker")
    assert s["budget_usd"] == 3.0 and s["max_turns"] == 20
    assert s["full_access"] is True  # a project can't switch the coworker's access


def test_invalid_project_values_fall_back(project):
    append_toml(project, '[chat.coworker]\nbudget_usd = 5000\nmax_turns = "many"\neffort = "extreme"\nmodel = "gpt-4"\n'
                         'tools_off = "Bash"\nprompt_extra = 5\n\n'
                         '[chat.agents.explorer]\nmodel = 3\nmax_turns = 0\ntools = ["Agent", "WebFetch"]\nenabled = "yes"\n'
                         '[chat.planner]\nbudget_usd = 0.01\n')
    c = ca.mode_settings(project, "coworker")
    assert (c["budget_usd"], c["max_turns"], c["effort"], c["model"]) == (5.0, 60, "medium", "sonnet")
    assert c["tools_off"] == [] and c["prompt_extra"] == ""
    assert ca.mode_settings(project, "planner")["budget_usd"] == 2.0
    e = ca.agent_settings(project, "explorer")
    assert e["model"] == "haiku" and e["max_turns"] == 30 and e["enabled"] is True
    # unknown tools are clamped away; "WebFetch" is in the catalogue and stays, "Agent" doesn't
    assert "Agent" not in e["tools"]
    ca.agent_definitions(project)  # never raises
    ca.config_view(project)


def test_broken_project_toml_does_not_raise(project):
    f = project.path("project.toml")
    good = f.read_text(encoding="utf-8")
    f.write_text("this is [not toml", encoding="utf-8")
    try:
        assert ca.mode_settings(project, "coworker")["budget_usd"] == 5.0
        assert set(ca.AGENT_NAMES) <= set(ca.agent_definitions(project))
    finally:  # the scratch data home is shared: other tests list every project
        f.write_text(good, encoding="utf-8")


def test_disabled_agent_is_left_out(project):
    append_toml(project, "[chat.agents.reviewer]\nenabled = false\n")
    defs = ca.agent_definitions(project)
    assert "reviewer" not in defs and {"explorer", "implementer"} <= set(defs)


def test_agent_tools_clamped_to_catalogue(project):
    append_toml(project, '[chat.agents.explorer]\ntools = ["Read", "Agent", "mcp__agentdesk__run_all", "Bogus", "Read"]\n')
    assert ca.agent_settings(project, "explorer")["tools"] == ["Read"]
    append_toml(project, '[chat.agents.implementer]\ntools = ["Agent", "Bogus"]\n')  # nothing valid: default, not "inherit"
    assert ca.agent_settings(project, "implementer")["tools"]


def test_agent_definition_carries_overrides_and_extra(project):
    append_toml(project, '[chat.agents.explorer]\nmax_turns = 7\neffort = "high"\nmodel = "claude-haiku-4-5"\n'
                         'prompt_extra = "Answer in Spanish."\n')
    d = ca.agent_definitions(project)["explorer"]
    assert d["maxTurns"] == 7 and d["effort"] == "high" and d["model"] == "claude-haiku-4-5"
    assert d["prompt"].startswith(ca.AGENT_DEFAULTS["explorer"]["prompt"]) and "Answer in Spanish." in d["prompt"]


# ----------------------------------------------------------------------------- tools

def test_tools_off_never_adds(project):
    base = ca.mode_tool_lists(project, "coworker", full_access=False)
    assert base["allowed"] == list(ca.BASE_ALLOWED)
    append_toml(project, '[chat.coworker]\ntools_off = ["Grep", "WebFetch", "run_all", "Bash", "Bogus"]\n')
    got = ca.mode_tool_lists(project, "coworker", full_access=False)
    assert set(got["allowed"]) <= set(ca.BASE_ALLOWED) and "Grep" not in got["allowed"]
    assert {"Grep", "Bash", "WebFetch", "mcp__agentdesk__run_all"} <= set(got["disallowed"])
    assert set(base["disallowed"]) <= set(got["disallowed"])
    assert "Bogus" not in got["disallowed"] and "mcp__agentdesk__Bogus" not in got["disallowed"]
    full = ca.mode_tool_lists(project, "coworker", full_access=True)
    assert set(full["allowed"]) <= set(ca.BASE_ALLOWED)


@pytest.mark.parametrize("mode", ["planner", "assistant"])
def test_read_only_modes_stay_read_only(project, mode):
    append_toml(project, f'[chat.{mode}]\ntools_off = ["Grep"]\nfull_access = true\nallowed_tools = ["Bash", "Write"]\n'
                         'tools = ["Bash"]\n')
    got = ca.mode_tool_lists(project, mode)
    assert set(got["allowed"]) <= {"Read", "Glob", "Grep"}
    assert not {"Bash", "Write", "Edit", "PowerShell"} & set(got["allowed"])
    assert set(ca.BASE_DISALLOWED[mode]) <= set(got["disallowed"])
    if mode == "planner":
        assert {"Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit"} <= set(got["disallowed"])
    else:
        assert "MultiEdit" in got["disallowed"]
    res = ca.save_config(project, {"modes": {mode: {"tools_off": ["Read"]}}})
    assert res["ok"]
    got = ca.mode_tool_lists(project, mode)
    assert "Read" not in got["allowed"] and set(ca.BASE_DISALLOWED[mode]) <= set(got["disallowed"])


# ----------------------------------------------------------------------------- prompt_extra

def test_prompt_extra_cap_and_wrap(project):
    assert ca.prompt_extra(project, "coworker") == ""
    append_toml(project, '[chat.coworker]\nprompt_extra = "Use tabs."\n')
    out = ca.prompt_extra(project, "coworker")
    assert ca.EXTRA_HEADING in out and "Use tabs." in out
    assert ca.prompt_extra(project, "planner") == ""
    f = project.path("project.toml")
    f.write_text(f.read_text(encoding="utf-8").replace("Use tabs.", "x" * 9000), encoding="utf-8")
    out = ca.prompt_extra(project, "coworker")
    assert out.count("x") == ca.PROMPT_EXTRA_MAX and ca.EXTRA_HEADING in out


# ----------------------------------------------------------------------------- save_config

def test_save_validation_errors_name_the_field(project):
    before = project.path("project.toml").read_text(encoding="utf-8")
    cases = [
        ({"modes": {"coworker": {"budget_usd": 0.01}}}, "modes.coworker.budget_usd"),
        ({"modes": {"coworker": {"budget_usd": 101}}}, "modes.coworker.budget_usd"),
        ({"modes": {"coworker": {"budget_usd": True}}}, "modes.coworker.budget_usd"),
        ({"modes": {"coworker": {"daily_budget_usd": 1001}}}, "modes.coworker.daily_budget_usd"),
        ({"modes": {"coworker": {"daily_budget_usd": -1}}}, "modes.coworker.daily_budget_usd"),
        ({"modes": {"planner": {"max_turns": 0}}}, "modes.planner.max_turns"),
        ({"modes": {"planner": {"max_turns": 501}}}, "modes.planner.max_turns"),
        ({"modes": {"planner": {"max_turns": 2.5}}}, "modes.planner.max_turns"),
        ({"modes": {"assistant": {"effort": "max"}}}, "modes.assistant.effort"),
        ({"modes": {"assistant": {"model": "gpt-4"}}}, "modes.assistant.model"),
        ({"modes": {"assistant": {"model": "inherit"}}}, "modes.assistant.model"),
        ({"modes": {"assistant": {"budget_usd": 1}}}, "modes.assistant.budget_usd"),
        ({"modes": {"planner": {"daily_budget_usd": 1}}}, "modes.planner.daily_budget_usd"),
        ({"modes": {"coworker": {"full_access": False}}}, "modes.coworker.full_access"),
        ({"modes": {"coworker": {"prompt_extra": "y" * 4001}}}, "modes.coworker.prompt_extra"),
        ({"modes": {"coworker": {"prompt_extra": 3}}}, "modes.coworker.prompt_extra"),
        ({"modes": {"coworker": {"tools_off": ["Nope"]}}}, "modes.coworker.tools_off"),
        ({"modes": {"coworker": {"tools_off": "Bash"}}}, "modes.coworker.tools_off"),
        ({"modes": {"critic": {"model": "haiku"}}}, "modes.critic"),
        ({"modes": {"coworker": "x"}}, "modes.coworker"),
        ({"agents": {"explorer": {"tools": ["Agent"]}}}, "agents.explorer.tools"),
        ({"agents": {"explorer": {"tools": []}}}, "agents.explorer.tools"),
        ({"agents": {"explorer": {"enabled": "yes"}}}, "agents.explorer.enabled"),
        ({"agents": {"explorer": {"prompt": "hi"}}}, "agents.explorer.prompt"),
        ({"agents": {"wizard": {}}}, "agents.wizard"),
        ({"bogus": {}}, "bogus"),
    ]
    for data, field in cases:
        r = ca.save_config(project, data)
        assert r["ok"] is False and r["field"] == field and r["error"], (data, r)
    assert ca.save_config(project, "x")["ok"] is False
    assert project.path("project.toml").read_text(encoding="utf-8") == before


def test_save_accepts_valid_values_and_shows_them(project):
    r = ca.save_config(project, {
        "modes": {"coworker": {"budget_usd": 8, "daily_budget_usd": 0, "max_turns": 90, "effort": "high", "model": "opus",
                               "tools_off": ["WebFetch", "WebFetch"], "prompt_extra": "  Be terse.  "},
                  "assistant": {"max_turns": 25, "model": "claude-haiku-4-5"}},
        "agents": {"explorer": {"model": "sonnet", "max_turns": 10, "tools": ["Read", "Grep"], "enabled": False,
                                "prompt_extra": "Short."}}})
    assert r["ok"], r
    cfg = read_cfg(project)
    assert cfg["chat"]["coworker"] == {"budget_usd": 8.0, "daily_budget_usd": 0.0, "max_turns": 90, "effort": "high", "model": "opus",
                                       "tools_off": ["WebFetch"], "prompt_extra": "Be terse."}
    assert cfg["chat"]["assistant"] == {"max_turns": 25, "model": "claude-haiku-4-5"}
    assert cfg["chat"]["agents"]["explorer"]["enabled"] is False
    view = r["config"]
    assert view["modes"]["coworker"]["effective"]["budget_usd"] == 8.0
    assert view["modes"]["coworker"]["project"]["tools_off"] == ["WebFetch"]
    assert view["agents"]["explorer"]["effective"]["max_turns"] == 10
    assert view["agents"]["reviewer"]["project"] == {}
    assert set(view["choices"]["efforts"]) == {"low", "medium", "high"} and "opus" in view["choices"]["models"]
    assert "explorer" not in ca.agent_definitions(project)
    assert project.cfg["chat"]["coworker"]["budget_usd"] == 8.0  # p.cfg follows the file
    assert ca.save_config(project, {})["ok"] is True


def test_save_keeps_allowed_commands_chat_mode_and_the_rest(project):
    core.allow_command(project, "git status")
    core.set_chat_mode(project, "planner")
    f = project.path("project.toml")
    f.write_text("# my notes\n" + f.read_text(encoding="utf-8") + "\n[chat.coworker]\n# keep this key\nlive_note = \"x\"\nbudget_usd = 4.0\n"
                 "\n[extra]\nkeep = [1, 2]\n", encoding="utf-8")
    before = read_cfg(project)
    r = ca.save_config(project, {"modes": {"coworker": {"max_turns": 33}, "planner": {"effort": "low"}},
                                 "agents": {"reviewer": {"max_turns": 5}}})
    assert r["ok"], r
    after = read_cfg(project)
    assert core.chat_mode(project) == "planner" and after["chat_mode"] == "planner"
    assert after["chat"]["allowed_commands"] == ["git status"] == core.allowed_commands(project)
    assert after["extra"] == {"keep": [1, 2]} and after["gates"] == before["gates"] and after["repo"] == before["repo"]
    assert after["chat"]["coworker"] == {"live_note": "x", "budget_usd": 4.0, "max_turns": 33}
    assert after["chat"]["planner"] == {"effort": "low"} and after["chat"]["agents"]["reviewer"] == {"max_turns": 5}
    assert f.read_text(encoding="utf-8").startswith("# my notes\n")
    # and the allow-list still works afterwards
    core.allow_command(project, "git log")
    assert read_cfg(project)["chat"]["allowed_commands"] == ["git status", "git log"]
    assert read_cfg(project)["chat"]["coworker"]["max_turns"] == 33


def test_save_with_no_chat_table_then_allow_command(project):
    assert "chat" not in read_cfg(project) or "allowed_commands" not in read_cfg(project)["chat"]
    assert ca.save_config(project, {"modes": {"planner": {"budget_usd": 3}}})["ok"]
    core.allow_command(project, "git diff")
    cfg = read_cfg(project)
    assert cfg["chat"]["planner"]["budget_usd"] == 3.0 and cfg["chat"]["allowed_commands"] == ["git diff"]


def test_null_removes_an_override(project):
    assert ca.save_config(project, {"modes": {"coworker": {"budget_usd": 9, "max_turns": 70}},
                                    "agents": {"explorer": {"max_turns": 9}}})["ok"]
    assert ca.mode_settings(project, "coworker")["budget_usd"] == 9.0
    r = ca.save_config(project, {"modes": {"coworker": {"budget_usd": None}}})
    assert r["ok"] and ca.mode_settings(project, "coworker")["budget_usd"] == 5.0
    assert read_cfg(project)["chat"]["coworker"] == {"max_turns": 70}
    r = ca.save_config(project, {"modes": {"coworker": {"max_turns": None}}, "agents": {"explorer": {"max_turns": None}}})
    assert r["ok"]
    cfg = read_cfg(project)
    assert "coworker" not in cfg.get("chat", {}) and "agents" not in cfg.get("chat", {})
    assert ca.agent_settings(project, "explorer")["max_turns"] == 30
    assert ca.save_config(project, {"modes": {"coworker": {"budget_usd": None}}})["ok"]  # nothing to remove


def test_empty_prompt_extra_and_tools_off_remove(project):
    assert ca.save_config(project, {"modes": {"planner": {"prompt_extra": "hi", "tools_off": ["Grep"]}}})["ok"]
    assert ca.save_config(project, {"modes": {"planner": {"prompt_extra": "  ", "tools_off": []}}})["ok"]
    assert "planner" not in read_cfg(project).get("chat", {})


def test_special_characters_round_trip(project):
    text = 'Quote " backslash \\ newline\nunicode é — 中 tab\t end'
    assert ca.save_config(project, {"modes": {"coworker": {"prompt_extra": text}}})["ok"]
    assert read_cfg(project)["chat"]["coworker"]["prompt_extra"] == text
    assert text in ca.prompt_extra(project, "coworker")


def test_hand_written_dotted_form_is_refused_not_corrupted(project):
    append_toml(project, '[chat]\ncoworker.max_turns = 12\n')
    before = project.path("project.toml").read_text(encoding="utf-8")
    r = ca.save_config(project, {"modes": {"coworker": {"budget_usd": 3}}})
    assert r["ok"] is False and r["field"] == "modes.coworker"
    assert project.path("project.toml").read_text(encoding="utf-8") == before
    assert tomllib.loads(before)  # still valid


def test_view_does_not_assume_totals(project):
    v = ca.config_view(project)
    assert set(ca.MODES) <= set(v["modes"]) and set(ca.AGENT_NAMES) <= set(v["agents"])
    assert {"Read", "Bash", "Edit"} <= set(v["choices"]["tools"])
    for m in ca.MODES:
        assert set(v["modes"][m]["effective"]) == set(ca.MODE_KEYS[m])
    assert v["modes"]["assistant"]["effective"]["max_turns"] == 40


def test_model_validation_accepts_aliases_and_full_ids():
    for ok in ("haiku", "sonnet", "opus", "claude-sonnet-4-5", "claude-opus-4.1"):
        assert ca.check_value("model", ok) == ok
    assert ca.check_value("model", "inherit", "agent") == "inherit"
    for bad in ("", "Claude-x", "claude-", "claude x", "sonnet4", 5, None):
        with pytest.raises(ca.Invalid):
            ca.check_value("model", bad, "agent")
