+++
id = "003"
title = "Chat Assistant mode with approvals and run tools"
tier = "hard"
+++

Source: section 1 (Assistant mode) of `zz_agentdesk_next.md` (read it first).

## Goal
Add a chat mode toggle per project, and the Assistant mode (safe and cheap). Coworker mode is task 004.

- Toggle in the chat header, stored in `project.toml` as `chat_mode = "assistant" | "coworker"`
  (default assistant). Switching resets the chat session (new options), keeps the visible history.
- Assistant mode: model from settings `[chat.assistant]`, default haiku.
  - Reads files, repo, state, reports, logs (Read/Glob/Grep, no approval).
  - Never edits repository code. May write `tasks/*.md` and append to `notes.md`.
  - Delegates work with custom tools (SDK MCP server via `create_sdk_mcp_server` and `@tool`):
    `add_task`, `run_next`, `run_task(id)`, `run_all`, `plan_task(id)`, `stop_run`, `queue_status`.
    Run and stop calls need the user's approval.
  - Shell commands (Bash/PowerShell) need approval every time, except an allow-list the user grows with
    "Always allow", stored per project in `project.toml` `[chat] allowed_commands` as exact prefixes
    (e.g. `git status`, `git log`).
- Approval flow: when `can_use_tool` is called, create a pending request (id, tool, command/args, reason)
  and await an asyncio Future. The dashboard polls `GET /api/p/<p>/pending` and shows a card in the chat:
  **Allow once / Always allow / Deny** via `POST /api/p/<p>/pending/<id>`. Timeout 10 min → deny.
- Don't list Bash/PowerShell or the run tools in `allowed_tools`: those bypass `can_use_tool`.
- Show every tool call in the chat (command + short output, collapsible), and cost/tokens per message
  (already tracked in `chat_usage.json`).

## Where
- `agentdesk/server.py` (`Chat` class, `can_use_tool`, new endpoints, polling).
- `agentdesk/core.py` (allow-list read/write, if it belongs there).
- `agentdesk/web/index.html` (toggle, approval card, tool-call display).
- `README.md` (modes).

## Constraints
- Don't change the worker pipeline in `core.py` beyond what the tools call.
- Keep the current behaviour that the chat can't run shell commands unless approved.
- Test with a temporary `AGENTDESK_HOME` and a scratch repo; keep `data/` intact. Live chat test with Haiku only (cents).

## Done when
- Toggle works and persists in `project.toml`.
- Unit tests cover the approval queue (allow once, always allow, deny, timeout) and the allow-list.
- In assistant mode, a shell command shows an approval card; nothing runs until Allow.
- A run tool call shows an approval card and starts the run only when allowed.
- A headless screenshot of the dashboard with a pending approval card is saved.
