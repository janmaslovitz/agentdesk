"""The chat agents' defaults and their per-project config layer.

SDK-free on purpose (it imports `core`, never `claude_agent_sdk`), so it can be tested and used to render the
settings page without the SDK. server.py turns what is computed here into `AgentDefinition(**kwargs)` / options.

Three chat modes (assistant, coworker, planner) and three subagents of the live Coworker (explorer, reviewer,
implementer). Their settings come in layers:

    modes   : CHAT_DEFAULTS < settings.toml `[chat.<mode>]` (both via core.chat_settings) < project.toml `[chat.<mode>]`
    agents  : AGENT_DEFAULTS < settings.toml `[chat.agents.<name>]` < project.toml `[chat.agents.<name>]`

Security rules (they hold whatever the config files say):

* Config can only REMOVE tools from a mode (`tools_off`). It never adds to `allowed_tools`: tools listed there skip
  `can_use_tool` and the guard, so the allow-list is a constant of this module (Read, Glob, Grep).
* The planner and the assistant stay read-only: their tool lists are constants that config can only shrink.
* A subagent's tool list is clamped to TOOL_CATALOGUE: no Agent (no nesting), no MCP tools (no run/integrate tools),
  nothing unknown. An empty or wholly invalid list falls back to the default, never to "inherit everything".
* Project config can't switch `full_access`, `live` or any other key outside MODE_KEYS / AGENT_KEYS.
* Invalid values (e.g. hand-written in project.toml through the raw editor) are ignored and fall back to the layer
  below; reading never raises at session start. `save_config` is the strict side: it rejects them with a field name.
"""

from __future__ import annotations

import copy
import math
import os
import re
import tomllib

from . import core

MODES = ("assistant", "coworker", "planner")
AGENT_NAMES = ("explorer", "reviewer", "implementer")

# Per-mode keys a project may set under `[chat.<mode>]`. The assistant has no per-message budget (it is one
# persistent session; only the autonomous modes have one), and only the coworker has a daily budget.
_COMMON_KEYS = ("model", "effort", "max_turns", "tools_off", "prompt_extra")
MODE_KEYS = {
    "assistant": _COMMON_KEYS,
    "coworker": ("model", "effort", "budget_usd", "daily_budget_usd", "max_turns", "tools_off", "prompt_extra"),
    "planner": ("model", "effort", "budget_usd", "max_turns", "tools_off", "prompt_extra"),
}
AGENT_KEYS = ("enabled", "model", "effort", "max_turns", "tools", "prompt_extra")

MODEL_ALIASES = ("haiku", "sonnet", "opus", "inherit")
MODEL_ID = re.compile(r"claude-[a-z0-9.-]+")
EFFORTS = ("low", "medium", "high")
# Built-in tools a subagent may be given (and a mode may have switched off). No Agent: subagents don't nest.
TOOL_CATALOGUE = ("Read", "Grep", "Glob", "Bash", "PowerShell", "Edit", "Write", "MultiEdit", "NotebookEdit",
                  "WebFetch", "WebSearch")
# Besides the catalogue a mode may switch off the Agent tool and the AgentDesk server's own tools (short names).
AGENTDESK_TOOLS = ("add_task", "queue_status", "task_activity", "run_next", "run_task", "run_all", "plan_task", "stop_run",
                   "architect", "plan", "graph", "review", "answer_task", "checkup", "watch_run", "schedule_checkup",
                   "approve_plan", "cancel_plan", "integrate", "propose_plan", "update_plan")
MODE_TOOL_NAMES = TOOL_CATALOGUE + ("Agent",) + AGENTDESK_TOOLS
MCP_PREFIX = "mcp__agentdesk__"

RANGES = {"budget_usd": (0.1, 100), "daily_budget_usd": (0, 1000), "max_turns": (1, 500)}
PROMPT_EXTRA_MAX = 4000
ASSISTANT_MAX_TURNS = 40
EXTRA_HEADING = "Project instructions for this chat"

# Tools that go in allowed_tools: they skip can_use_tool, so only reading belongs here. A constant, never from config.
BASE_ALLOWED = ("Read", "Glob", "Grep")
# What each mode never gets (disallowed_tools). The guarded coworker (full_access = false) differs from the full one.
# The assistant's Write/Edit are not here: its permission callback limits them to tasks/*.md and notes.md.
BASE_DISALLOWED = {
    "assistant": ("WebFetch", "WebSearch", "NotebookEdit", "MultiEdit", "Task", "Agent"),
    "planner": ("Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"),
    "coworker": ("WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"),
    "coworker_full": (),
}
READ_ONLY_MODES = ("assistant", "planner")

IS_WINDOWS = os.name == "nt"  # PowerShell is only offered there


def shell_tools() -> tuple[str, ...]:
    """The shell tools that exist on this platform: Bash everywhere, PowerShell on Windows only."""
    return ("Bash", "PowerShell") if IS_WINDOWS else ("Bash",)


# ----------------------------------------------------------------------------- defaults of the subagents

_READ = ["Read", "Grep", "Glob"]
AGENT_DEFAULTS: dict[str, dict] = {
    "explorer": {
        "description": "Fast read-only investigator: finds files, reads code and logs, answers a specific question about the "
                       "repository. It has no shell: it can't run git or any command.",
        "prompt": "You investigate and report. You have Read, Grep and Glob only: you can't run commands, so for git history or "
                  "command output say in your report that the caller has to get it. Never change anything. Answer the question "
                  "in a short, factual report with file paths and line numbers; if you can't find it, say what you searched.",
        "model": "haiku", "tools": list(_READ), "max_turns": 30, "effort": "low", "enabled": True,
    },
    "reviewer": {
        "description": "Reviews a branch, commit range or diff for correctness bugs and missed requirements, and runs the "
                       "project's checks. Reads and runs commands; never edits.",
        "prompt": "You review code. Read the diff (git diff / git log / git show), read the surrounding code, run the project's "
                  "checks when that helps, and report concrete problems with file and line, most serious first. You have no edit "
                  "tools and must not change anything through the shell either: use it only to read (git diff, log, show, "
                  "status) and to run checks. Never create, edit or delete files, commit, reset, check out, stash, push or "
                  "change anything else in the repository. If it is fine, say so.",
        "model": "sonnet", "tools": _READ + ["Bash", "PowerShell"], "max_turns": 40, "effort": "medium", "enabled": True,
    },
    "implementer": {
        "description": "Makes one focused code change in the checkout and runs the relevant checks, then reports what changed. "
                       "Has read, edit and shell tools; no web access, no other agents, no AgentDesk run tools.",
        "prompt": "You make exactly the change you are asked for, in the files you are told about, following the project's "
                  "instructions (CLAUDE.md / AGENTS.md / README). Run the relevant checks. Don't commit unless asked; never push, "
                  "publish or change remotes. You have no web tools and can't start other agents or runs: if the change needs "
                  "more than you were told, stop and say so. Report the files changed and the check results.",
        "model": "sonnet", "tools": _READ + ["Edit", "Write", "MultiEdit", "Bash", "PowerShell"], "max_turns": 60,
        "effort": "medium", "enabled": True,
    },
}


# ----------------------------------------------------------------------------- validation

class Invalid(ValueError):
    """A value that is not acceptable; `field` names it (e.g. `modes.coworker.budget_usd`)."""

    def __init__(self, msg: str, field: str = ""):
        super().__init__(msg)
        self.field = field


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clean_tools(v, allowed: tuple[str, ...], strict: bool, what: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise Invalid(f"{what} must be a list of tool names")
    names = [x.strip() for x in v]
    bad = [x for x in names if x not in allowed]
    if bad and strict:
        raise Invalid(f"unknown tool {bad[0]!r} in {what}; choose from {', '.join(allowed)}")
    return list(dict.fromkeys(x for x in names if x in allowed))


def check_value(key: str, value, scope: str = "mode", strict: bool = True):
    """The normalised `value` for config key `key`, or raise Invalid. `scope` is "mode" or "agent". strict=False (reading
    a file) clamps tool lists to the catalogue instead of rejecting unknown names."""
    if key == "model":
        if not isinstance(value, str):
            raise Invalid("model must be a string")
        v = value.strip()
        if v in MODEL_ALIASES and not (v == "inherit" and scope == "mode"):
            return v
        if MODEL_ID.fullmatch(v):
            return v
        aliases = [a for a in MODEL_ALIASES if scope == "agent" or a != "inherit"]
        raise Invalid(f"model must be one of {', '.join(aliases)} or a full id like claude-sonnet-4-5")
    if key == "effort":
        if value not in EFFORTS:
            raise Invalid(f"effort must be one of {', '.join(EFFORTS)}")
        return value
    if key in ("budget_usd", "daily_budget_usd"):
        lo, hi = RANGES[key]
        if not _is_num(value) or not lo <= value <= hi:
            raise Invalid(f"{key} must be a number from {lo} to {hi}")
        return float(value)
    if key == "max_turns":
        lo, hi = RANGES[key]
        if not _is_int(value) or not lo <= value <= hi:
            raise Invalid(f"max_turns must be a whole number from {lo} to {hi}")
        return int(value)
    if key == "enabled":
        if not isinstance(value, bool):
            raise Invalid("enabled must be true or false")
        return value
    if key == "prompt_extra":
        if not isinstance(value, str):
            raise Invalid("prompt_extra must be text")
        if strict and len(value.strip()) > PROMPT_EXTRA_MAX:
            raise Invalid(f"prompt_extra is {len(value.strip())} characters; the limit is {PROMPT_EXTRA_MAX}")
        return value.strip()[:PROMPT_EXTRA_MAX]
    if key == "tools_off":
        return _clean_tools(value, MODE_TOOL_NAMES, strict, "tools_off")
    if key == "tools":
        tools = _clean_tools(value, TOOL_CATALOGUE, strict, "tools")
        if not tools:
            raise Invalid("tools needs at least one tool")
        return tools
    raise Invalid(f"unknown setting {key!r}")


def _valid_layer(table, keys: tuple[str, ...], scope: str) -> dict:
    """The valid `keys` of one config table; whatever is invalid (or not a table) is ignored."""
    out: dict = {}
    if not isinstance(table, dict):
        return out
    for k in keys:
        if k in table:
            try:
                out[k] = check_value(k, table[k], scope, strict=False)
            except Invalid:
                pass
    return out


def _project_cfg(p) -> dict:
    """project.toml as it is on disk now (a session starts from the file, not from a stale p.cfg)."""
    try:
        return core._project_cfg(p)
    except (OSError, ValueError):
        cfg = getattr(p, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}


def _chat_table(cfg: dict) -> dict:
    chat = cfg.get("chat")
    return chat if isinstance(chat, dict) else {}


def _settings_agents() -> dict:
    try:
        agents = core.settings().get("chat", {}).get("agents", {})
    except Exception:  # an unreadable settings.toml never breaks a chat
        return {}
    return agents if isinstance(agents, dict) else {}


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown chat mode {mode!r}; one of {', '.join(MODES)}")


def _check_agent(name: str) -> None:
    if name not in AGENT_NAMES:
        raise ValueError(f"unknown agent {name!r}; one of {', '.join(AGENT_NAMES)}")


# ----------------------------------------------------------------------------- reading the effective settings

def project_mode_overrides(p, mode: str) -> dict:
    """The valid values of project.toml `[chat.<mode>]` (only MODE_KEYS; the rest of the table is not ours)."""
    _check_mode(mode)
    return _valid_layer(_chat_table(_project_cfg(p)).get(mode), MODE_KEYS[mode], "mode")


def project_agent_overrides(p, name: str) -> dict:
    """The valid values of project.toml `[chat.agents.<name>]`."""
    _check_agent(name)
    agents = _chat_table(_project_cfg(p)).get("agents")
    return _valid_layer(agents.get(name) if isinstance(agents, dict) else None, AGENT_KEYS, "agent")


def mode_settings(p, mode: str) -> dict:
    """core.chat_settings(mode) (CHAT_DEFAULTS < settings.toml) overlaid with the valid keys of project.toml
    `[chat.<mode>]`. Adds `tools_off` ([]) and `prompt_extra` (""), and the assistant's max_turns defaults to 40.
    Invalid project values are ignored (the lower layer shows through); the settings.toml layer is passed through as
    core has always done, except the new keys, which are sanitised."""
    _check_mode(mode)
    out = dict(core.chat_settings(mode))
    if mode == "assistant":
        out.setdefault("max_turns", ASSISTANT_MAX_TURNS)
    for key, empty in (("tools_off", []), ("prompt_extra", "")):
        try:
            out[key] = check_value(key, out[key], strict=False) if key in out else empty
        except Invalid:
            out[key] = empty
    out.update(project_mode_overrides(p, mode))
    return out


def agent_settings(p, name: str) -> dict:
    """AGENT_DEFAULTS[name] < settings.toml `[chat.agents.<name>]` < project.toml `[chat.agents.<name>]`, each layer
    validated key by key. `tools` is clamped to TOOL_CATALOGUE and loses PowerShell off Windows; `prompt_extra` is added."""
    _check_agent(name)
    out = copy.deepcopy(AGENT_DEFAULTS[name])
    out["prompt_extra"] = ""
    out.update(_valid_layer(_settings_agents().get(name), AGENT_KEYS, "agent"))
    out.update(project_agent_overrides(p, name))
    tools = [t for t in out["tools"] if t in TOOL_CATALOGUE and (t != "PowerShell" or IS_WINDOWS)]
    out["tools"] = tools or [t for t in AGENT_DEFAULTS[name]["tools"] if t != "PowerShell"]
    return out


def _wrap(text: str) -> str:
    text = text.strip()[:PROMPT_EXTRA_MAX].strip() if isinstance(text, str) else ""
    return f"\n# {EXTRA_HEADING}\n{text}\n" if text else ""


def prompt_extra(p, mode: str) -> str:
    """The project's extra instructions for a mode, to append to its system prompt: at most 4000 characters, under a
    'Project instructions for this chat' header; "" when unset."""
    return _wrap(mode_settings(p, mode).get("prompt_extra") or "")


def agent_definitions(p) -> dict[str, dict]:
    """AgentDefinition keyword arguments (description, prompt incl. the agent's prompt_extra, tools, model, maxTurns,
    effort) for the enabled subagents. Tool lists are clamped to the catalogue (see agent_settings)."""
    out = {}
    for name in AGENT_NAMES:
        s = agent_settings(p, name)
        if s["enabled"]:
            out[name] = {"description": s["description"], "prompt": s["prompt"] + _wrap(s["prompt_extra"]), "tools": list(s["tools"]),
                         "model": s["model"], "maxTurns": s["max_turns"], "effort": s["effort"]}
    return out


def mode_tool_lists(p, mode: str, full_access: bool | None = None) -> dict[str, list[str]]:
    """{'allowed': [...], 'disallowed': [...]} for a mode's options. `allowed` is BASE_ALLOWED minus the tools switched
    off: config never adds to it (those tools skip can_use_tool and the guard). `disallowed` is the mode's constant list
    plus the tools switched off (AgentDesk tools as mcp__agentdesk__<name>). The assistant and the planner are
    read-only whatever the config says: nothing here can widen their lists. `full_access` (coworker only; default:
    the coworker's setting) picks the guarded or the full list."""
    _check_mode(mode)
    off = list(mode_settings(p, mode)["tools_off"])
    if mode == "coworker":
        if full_access is None:
            full_access = bool(core.chat_settings("coworker").get("full_access"))
        base = BASE_DISALLOWED["coworker_full" if full_access else "coworker"]
    else:
        base = BASE_DISALLOWED[mode]
    allowed = [t for t in BASE_ALLOWED if t not in off]
    disallowed = list(base)
    for t in off:
        full = t if t in TOOL_CATALOGUE or t == "Agent" else MCP_PREFIX + t
        if full not in disallowed:
            disallowed.append(full)
    return {"allowed": allowed, "disallowed": disallowed}


# ----------------------------------------------------------------------------- the view for the settings page

def choices() -> dict:
    return {"modes": list(MODES), "agents": list(AGENT_NAMES), "models": list(MODEL_ALIASES), "model_pattern": MODEL_ID.pattern,
            "efforts": list(EFFORTS), "tools": list(TOOL_CATALOGUE), "mode_tools": list(MODE_TOOL_NAMES),
            "mode_keys": {m: list(k) for m, k in MODE_KEYS.items()}, "agent_keys": list(AGENT_KEYS),
            "ranges": {k: list(v) for k, v in RANGES.items()}, "prompt_extra_max": PROMPT_EXTRA_MAX,
            "shell_tools": list(shell_tools())}


def config_view(p) -> dict:
    """{'modes': {mode: {'effective', 'project'}}, 'agents': {name: {'effective', 'project'}}, 'choices'}. `effective` is
    what a session would use now; `project` holds only the valid values set in this project's project.toml."""
    modes, agents = {}, {}
    for m in MODES:
        s = mode_settings(p, m)
        modes[m] = {"effective": {k: s.get(k) for k in MODE_KEYS[m]}, "project": project_mode_overrides(p, m)}
    for n in AGENT_NAMES:
        s = agent_settings(p, n)
        agents[n] = {"effective": {"description": s["description"], "prompt": s["prompt"], **{k: s[k] for k in AGENT_KEYS}},
                     "project": project_agent_overrides(p, n)}
    return {"modes": modes, "agents": agents, "choices": choices()}


# ----------------------------------------------------------------------------- saving

_BARE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(k: str) -> str:
    return k if _BARE.fullmatch(k) else core.toml_str(k)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf") if math.isinf(v) else repr(v)
    if isinstance(v, str):
        return core.toml_str(v).replace("\x7f", "\\u007f")
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if hasattr(v, "isoformat"):  # TOML dates and times read back by tomllib
        return v.isoformat()
    raise TypeError(f"can't write {type(v).__name__} as TOML")


def _render(path: tuple[str, ...], table: dict) -> str:
    """`[a.b]` plus its plain values (sub-tables stay where they are in the file)."""
    lines = ["[" + ".".join(_toml_key(x) for x in path) + "]"]
    lines += [f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in table.items() if not isinstance(v, dict)]
    return "\n".join(lines) + "\n"


def _remove_section(text: str, path: tuple[str, ...]) -> str:
    """Drop the `[path]` table (header to the next table header) from `text`; text without it is returned unchanged."""
    dots = r"\s*\.\s*".join(rf"(?:{re.escape(x)}|\"{re.escape(x)}\")" for x in path)
    m = re.search(rf"(?m)^[ \t]*\[\s*{dots}\s*\][ \t]*(?:#.*)?$\n?", text)
    if not m:
        return text
    nxt = re.search(r"(?m)^[ \t]*\[", text[m.end():])
    end = m.end() + nxt.start() if nxt else len(text)
    return text[:m.start()] + text[end:]


def _normalise(cfg: dict, targets: list[tuple[str, ...]]) -> dict:
    """A copy of cfg with empty tables at the target paths (and an empty `chat`) removed, for comparing."""
    cfg = copy.deepcopy(cfg)
    for path in targets:
        node = cfg
        for part in path[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(path[-1]) == {}:
            del node[path[-1]]
    chat = cfg.get("chat")
    if isinstance(chat, dict):
        if chat.get("agents") == {}:
            del chat["agents"]
        if not chat:
            del cfg["chat"]
    return cfg


def _plan(data) -> list[tuple[tuple[str, ...], str, dict]]:
    """Validate `data` ({'modes': {mode: {key: value|None}}, 'agents': {name: {key: value|None}}}) into a list of
    (table path, field prefix, {key: normalised value | None}); None means remove the override. Raises Invalid."""
    if not isinstance(data, dict):
        raise Invalid("the config must be an object", "")
    plan = []
    for top in data:
        if top not in ("modes", "agents"):
            raise Invalid(f"unknown section {top!r}; use modes or agents", str(top))
    for section, names, keys, scope in (("modes", MODES, MODE_KEYS, "mode"), ("agents", AGENT_NAMES, None, "agent")):
        block = data.get(section)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise Invalid(f"{section} must be an object", section)
        for name, values in block.items():
            field = f"{section}.{name}"
            if name not in names:
                raise Invalid(f"unknown {section[:-1]} {name!r}; one of {', '.join(names)}", field)
            if not isinstance(values, dict):
                raise Invalid(f"{field} must be an object", field)
            allowed = keys[name] if keys else AGENT_KEYS
            clean = {}
            for k, v in values.items():
                if k not in allowed:
                    raise Invalid(f"{k!r} can't be set for {name}; editable: {', '.join(allowed)}", f"{field}.{k}")
                if v is None or (k in ("prompt_extra",) and isinstance(v, str) and not v.strip()) or (k == "tools_off" and v == []):
                    clean[k] = None
                    continue
                try:
                    clean[k] = check_value(k, v, scope, strict=True)
                except Invalid as e:
                    raise Invalid(str(e), f"{field}.{k}") from None
            path = ("chat", name) if section == "modes" else ("chat", "agents", name)
            plan.append((path, field, clean))
    return plan


def save_config(p, data) -> dict:
    """Validate `data` and write it to project.toml; only the `[chat.<mode>]` and `[chat.agents.<name>]` tables are
    touched, the rest of the file (chat_mode, `[chat] allowed_commands`, gates, comments…) stays as it is. A null value
    removes that override; a table left empty disappears. Rewritten files are read back with tomllib and compared with
    the expected content before anything is written (core._write_project refuses unreadable text too), so a failed
    edit leaves project.toml as it was. Config here can only narrow (tools_off) or tune; see the module docstring.

    Returns {'ok': True, 'config': config_view(p)} or {'ok': False, 'error': str, 'field': str}."""
    try:
        plan = _plan(data)
    except Invalid as e:
        return {"ok": False, "error": str(e), "field": e.field}
    if not plan:
        return {"ok": True, "config": config_view(p)}
    try:
        text = core._project_text(p)
        old = tomllib.loads(text)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"project.toml can't be read: {e}", "field": "project.toml"}
    expected = copy.deepcopy(old)
    new_tables: list[tuple[tuple[str, ...], dict]] = []
    targets = [path for path, _f, _c in plan]
    for path, _field, changes in plan:
        node = old
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        table = copy.deepcopy(node) if isinstance(node, dict) else {}
        for k, v in changes.items():
            if v is None:
                table.pop(k, None)
            else:
                table[k] = v
        # expected content: set the table (or drop it when empty)
        parent = expected
        for part in path[:-1]:
            parent = parent.setdefault(part, {})
            if not isinstance(parent, dict):
                return {"ok": False, "error": f"project.toml has a [{path[0]}] value that isn't a table; edit it in the raw editor",
                        "field": "project.toml"}
        if table:
            parent[path[-1]] = table
        else:
            parent.pop(path[-1], None)
        new_tables.append((path, table))
    for path, _table in new_tables:
        text = _remove_section(text, path)
    text = text.rstrip("\n")
    adds = [_render(path, table) for path, table in new_tables if any(not isinstance(v, dict) for v in table.values())]
    text = (text + "\n\n" + "\n".join(adds)) if adds else (text + "\n" if text else "")
    try:
        got = tomllib.loads(text)
    except ValueError:
        got = None
    if got is None or _normalise(got, targets) != _normalise(expected, targets):
        return {"ok": False, "field": plan[0][1], "error": "couldn't update project.toml safely (is a chat table written in "
                "another form, e.g. dotted keys?); edit it in the raw editor"}
    try:
        core._write_project(p, text)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"couldn't write project.toml: {e}", "field": "project.toml"}
    return {"ok": True, "config": config_view(p)}
