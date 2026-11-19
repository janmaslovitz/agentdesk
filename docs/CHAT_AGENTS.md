# Chat agents: audit and config layer

Audit of how the chat agents work in `agentdesk/server.py` (prompts, `live_agents()`, the `_*_options()` methods,
`tools()` / `planner_tools()`), `core.chat_settings` / `CHAT_DEFAULTS` and the README's chat section, and the config
layer that `agentdesk/chat_agents.py` adds. Every finding is tagged:

- **fixed in chat_agents.py**: the corrected data or helper lives in `chat_agents.py`; server.py must still read it
  (the wiring task), a "Wiring" note says how;
- **fixed in server.py**: the text or code was in server.py (or the README) and the wiring task changed it; the note says how.

There are no open items left.

## How it works (the audit's starting point; the findings below say what changed)

| Mode | Tools it gets | Limits |
|---|---|---|
| Assistant | `allowed_tools` Read/Glob/Grep; Write/Edit only through `permission()` (tasks/*.md, notes.md); Bash/PowerShell and the run tools ask for an approval card (an allow-list in project.toml skips the card) | one persistent session, `max_turns` (default 40, configurable), no budget |
| Coworker (full access, live) | everything; only `[chat.coworker_confirm]` shell patterns ask; AgentDesk tools incl. `checkup`, `watch_run`; subagents from `live_agents()` | `max_turns`, per-burst budget checked *after* the burst, process backstop = `daily_budget_usd` |
| Coworker (full access, `live=false`) | same, a fresh process per message | `max_budget_usd = budget_usd` per message |
| Coworker (guarded) | own worktree, guard + hook; Web/Agent tools disallowed | as above |
| Planner | Read/Glob/Grep + `propose_plan`, `update_plan`, `cancel_plan`, `architect`; `planner_permission` and a hook deny the rest | `max_turns` (30), `budget_usd` (2.0) per message |

Subagents (live Coworker only): explorer (haiku, Read/Grep/Glob), reviewer (sonnet, + Bash/PowerShell),
implementer (sonnet, **no `tools` list**).

## Findings

### Subagents

1. **The implementer inherits every tool.** `AgentDefinition` without `tools` inherits the parent's: Agent, WebFetch,
   WebSearch, Write/Edit of anything, *and the `mcp__agentdesk__*` tools* (`run_all`, `integrate`, `approve_plan`, …), so a
   "focused change" helper can start runs. **fixed in chat_agents.py**: explicit list `Read, Grep, Glob, Edit, Write,
   MultiEdit, Bash` (+ PowerShell on Windows); the catalogue has no Agent and no MCP tools, and a configured list is
   clamped to it (an empty or wholly invalid list falls back to the default, never to "inherit"). Wiring: use
   `agent_definitions(p)`.
2. **The reviewer is told "Don't edit files" but has a shell.** Bash can write files, commit, reset. The prompt did not say
   so. **fixed in chat_agents.py**: the prompt now limits the shell to reading (`git diff/log/show/status`) and running
   checks and forbids creating/editing/deleting files, commits, resets, checkouts, stashes and pushes; it still has no
   Edit/Write tools. A prompt is not enforcement: **fixed in server.py**: the live Coworker has a `PreToolUse` hook (`Chat.subagent_hook`)
   that refuses a reviewer subagent's shell call (payload `agent_type == "reviewer"`) when it matches `[guard] deny` or
   changes files or git state (`reviewer_shell_denied`: commit/reset/checkout/…, redirects, `rm`/`mv`/`tee`/`sed -i`, …).
   In full-access mode the parent allows everything, so calls by the Coworker itself and by other subagents are not touched.
3. **PowerShell is offered on Linux/macOS.** The reviewer's tool list names `PowerShell` everywhere. **fixed in
   chat_agents.py**: `PowerShell` is dropped from subagent tools unless `IS_WINDOWS`; `shell_tools()` gives the
   platform's shell tools. Wiring: use it.
4. **Subagents have no limits.** No turn cap, no effort, one fixed model. **fixed in chat_agents.py**: `max_turns`
   (explorer 30, reviewer 40, implementer 60), `effort` (low/medium/medium), `enabled`, all overridable per project.
5. **The explorer's description over-promised** ("reads logs"; it can't run `git log`). **fixed in chat_agents.py**:
   description and prompt say it has no shell and must report what it couldn't get.
6. **Built-in subagents stay.** `agents=live_agents()` only adds to the SDK's built-in agents (general-purpose, …),
   which inherit all tools. **fixed in server.py**: `_live_options` adds `Agent(general-purpose)`, `Agent(Explore)`, `Agent(Plan)`,
   `Agent(statusline-setup)` to `disallowed_tools` (`BUILTIN_AGENTS`), so only the enabled configured subagents can be
   started; with all three disabled the whole `Agent` tool is disallowed and the prompt has no Subagents bullet.

### Prompts versus tools and behaviour

7. **"Shell commands (Bash/PowerShell)"** in CHAT_PROMPT, COWORKER_PROMPT and COWORKER_LIVE_ADDENDUM on every
   platform. **fixed in server.py**: the prompts carry `{shell}`, formatted with `"/".join(chat_agents.shell_tools())`.
8. **CHAT_PROMPT: "Shell commands also wait for the user's approval"**, but commands matching `[chat] allowed_commands`
   run without a card (README documents the allow-list). **fixed in server.py**: the prompt names the allow-list.
9. **COWORKER_FULL_PROMPT contradicts itself**: "every tool and every shell command runs without asking" and, a
   paragraph later, that push/release/PR/remote/hard-reset/… show an approval card (`coworker_permission` +
   `[chat.coworker_confirm]`). **fixed in server.py**: "except the very important commands listed below".
   README ("without approval cards and without the guard") had the same mismatch. **fixed in server.py** (README).
10. **COWORKER_ROLE promises wake-ups** ("you are woken with `[Run finished]`", architect "wakes you") for every
    Coworker mode, but `watch_run_bg`/`watch_architect_bg` wake a live session; with `live=false` or the guarded
    mode `inject()` only posts a note to the chat and the model is never woken. **fixed in server.py**:
    `coworker_role(folder, live)` keeps the wake-up sentences for the live session only; the other modes are told to poll
    `queue_status` / `plan()`.
11. **The tools lists in the prompts are incomplete/uneven**: COWORKER_PROMPT lists `checkup`, `watch_run` but not
    `schedule_checkup`; CHAT_PROMPT omits `answer_task`; README lists only seven Coworker tools (not `architect`, `plan`,
    `graph`, `review`, `integrate`, `approve_plan`, `cancel_plan`, `task_activity`, `answer_task`, `checkup`, `watch_run`,
    `schedule_checkup`). **fixed in server.py**: the Coworker prompts list `answer_task`, `schedule_checkup`; CHAT_PROMPT lists `answer_task`; README lists
    the full Coworker tool set.
12. **README's Planner bullet is missing**: the chat section jumps from the Coworker's budget bullet to "After approval
    the chat reports each plan task…" with the Planner description gone; `[chat.planner]` is documented nowhere.
    **fixed in server.py** (README: a Planner bullet and `[chat.planner]`).
13. **The assistant is not blocked from `MultiEdit`.** `permission()` handles Write/Edit only; `MultiEdit` falls through to
    `PermissionResultAllow()` and is absent from the assistant's `disallowed_tools`, so it can edit the repository
    despite "You never edit the repository". **fixed in chat_agents.py**: `BASE_DISALLOWED["assistant"]` contains
    `MultiEdit`. Wiring: build `allowed_tools`/`disallowed_tools` from `mode_tool_lists(p, "assistant")`.
14. **Tool lists were repeated by hand in four places** (`_assistant_options`, `_coworker_options` twice, `_live_options`,
    `_planner_options`). **fixed in chat_agents.py**: `BASE_ALLOWED` (Read/Glob/Grep, a constant: tools in `allowed_tools`
    skip `can_use_tool` and the guard) and `BASE_DISALLOWED[mode]`; `mode_tool_lists(p, mode)` returns both with
    `tools_off` applied (removal only).

### Budgets and limits versus the texts

15. **`max_turns=40` is hard-coded** for the assistant; planner/coworker read `chat_settings`. **fixed in
    chat_agents.py** (and `core.CHAT_DEFAULTS["assistant"]["max_turns"] = 40`): `mode_settings(p, "assistant")["max_turns"]`.
    Wiring: pass it to `core.options`.
16. **COWORKER_LIVE_ADDENDUM prints "$0.00 per day"** when `daily_budget_usd = 0`, which means *no cap*.
    **fixed in server.py**: `spending_text()` prints "no daily cap" for 0.
17. **Two budget statements in one live prompt**: COWORKER_FULL_PROMPT says "a budget per message (${budget})", the live
    addendum says "per burst (a message or a wake-up)"; and the real enforcement is different again: the burst budget is
    only checked after the burst (a note, no stop), the CLI's `max_budget_usd` is the *daily* backstop. **fixed in
    server.py**: one sentence per mode says what is enforced (per message: the process stops after the turn that reaches the
    budget; live: checked when the burst ends, daily cap blocks new work).
18. **`budget_note` points at "settings.toml"** only; with project overrides the value may come from project.toml.
    **fixed in server.py** (says "settings.toml or the project's chat settings"; `budget_note(..., p)` reads the project's value).
19. **The assistant has no budget** (one persistent session, `budget_note` only for coworker/planner) though the config
    story lists `budget_usd` for modes. **fixed in chat_agents.py**: `MODE_KEYS["assistant"]` has no budget keys, so the
    UI can't offer a limit that isn't enforced.
20. **Per-turn/budget overshoot**: the CLI checks `max_budget_usd` after each turn, so a message can go over by one turn
    (README says so for the guarded Coworker only). **fixed in server.py** (README wording).

### Settings documented differently from the code

21. **Defaults are in two places**: `DEFAULT_SETTINGS` (model, effort, budget_usd, …) and `core.CHAT_DEFAULTS`
    (effort, budget_usd, max_turns, full_access, live, …); `max_turns` appears only in `CHAT_DEFAULTS` (coworker 60,
    planner 30) and nowhere in the README or settings.toml comments. **fixed in chat_agents.py** for reading
    (`mode_settings` is the one place that overlays the layers and `config_view` shows the effective values); the
    README has the table now (**fixed in server.py**).
22. **The README says the models come from `data/settings.toml` only**; projects had no way to choose a model, effort, budget
    or turn cap. **fixed in chat_agents.py**: `[chat.<mode>]` and `[chat.agents.<name>]` in project.toml, validated.
23. **A project could not tell the chat anything about itself** except through chat_rules.md. **fixed in
    chat_agents.py**: `prompt_extra` (≤ 4000 chars, under "Project instructions for this chat"). Wiring: append
    `prompt_extra(p, mode)` in `with_memory` / the `*_options` prompts.

## The config layer (`agentdesk/chat_agents.py`)

SDK-free. Layers:

```
modes   CHAT_DEFAULTS < settings.toml [chat.<mode>]      (core.chat_settings)  < project.toml [chat.<mode>]
agents  AGENT_DEFAULTS < settings.toml [chat.agents.<n>]                        < project.toml [chat.agents.<n>]
```

| | keys |
|---|---|
| assistant | `model, effort, max_turns, tools_off, prompt_extra` |
| coworker | `model, effort, budget_usd (0.1–100), daily_budget_usd (0–1000, 0 = no cap), max_turns (1–500), tools_off, prompt_extra` |
| planner | `model, effort, budget_usd, max_turns, tools_off, prompt_extra` |
| explorer / reviewer / implementer | `enabled, model, effort, max_turns, tools, prompt_extra` |

Models: `haiku`, `sonnet`, `opus`, `inherit` (subagents only) or a full id `claude-[a-z0-9.-]+`. Efforts: `low`, `medium`,
`high`. `prompt_extra` ≤ 4000 characters.

Functions: `mode_settings`, `agent_settings`, `agent_definitions` (AgentDefinition kwargs), `prompt_extra`,
`mode_tool_lists` (allowed/disallowed with `tools_off` applied), `config_view` (`modes`/`agents` with `effective` and
`project`, plus `choices`), `save_config(p, data)`.

`save_config` takes `{"modes": {mode: {key: value|None}}, "agents": {name: {key: value|None}}}` and returns
`{"ok": True, "config": view}` or `{"ok": False, "error": ..., "field": "modes.coworker.budget_usd"}`. It rewrites only the
`[chat.<mode>]` / `[chat.agents.<name>]` tables of project.toml (the rest, including `chat_mode` and `[chat]
allowed_commands`, stays byte for byte); `null` removes an override and an empty table disappears; the result is read
back with `tomllib` and compared with the expected content before `core._write_project` writes it. A project.toml that
spells these tables another way (dotted keys, inline tables) is refused with an error rather than rewritten.

### Security rules

- Config can only **remove** tools from a mode (`tools_off`). It never adds to `allowed_tools`: those tools skip
  `can_use_tool` and the guard, so the list is the constant `BASE_ALLOWED`.
- The planner and the assistant stay **read-only** whatever the config says: their lists are constants that config can only
  shrink; the planner's permission callback and hook stay in place.
- Subagent tool lists are **clamped to the catalogue** (no Agent, no MCP tools, nothing unknown).
- A project can't set `full_access`, `live` or any key outside the tables above.
- **Invalid values** (hand-written through the raw editor) are ignored and fall back to the layer below; reading never raises
  at session start (an unreadable project.toml gives the defaults). Only `save_config` rejects them, with the field name.
- The settings.toml layer of the modes is passed through as before (it is the administrator's file), except the new keys.

### Wiring in server.py (done)

`_assistant_options`: `max_turns=mode_settings(...)["max_turns"]`, tool lists from `mode_tool_lists`, `prompt_extra`;
same for the coworker (three option builders) and planner; `live_agents()` → `{n: AgentDefinition(**kw) for n, kw in
agent_definitions(p).items()}`; budgets and models from `mode_settings(p, mode)` instead of `core.chat_settings(mode)`
(including `budget_note`, `is_live`, the live burst/daily checks); the prompt fixes 7–11, 16–18 above; README.
