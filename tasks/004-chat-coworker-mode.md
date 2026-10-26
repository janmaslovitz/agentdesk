+++
id = "004"
title = "Chat Coworker mode (autonomous, own worktree)"
tier = "hard"
depends = ["003"]
+++

Source: section 1 (Coworker mode) of `zz_agentdesk_next.md` (read it first). Needs task 003 (chat modes, approvals, run tools).

## Goal
Add Coworker mode to the chat toggle. It works on its own, without approval prompts, but with limits.

- Model from settings `[chat.coworker]`, default sonnet, effort medium.
- Freely reads and analyzes; runs shell commands without approval; starts/stops runs and manages the queue
  with the same custom tools from task 003, without approval.
- Code edits only in its own worktree `<worktrees>/cowork` on branch `auto/cowork`, never in the user's
  checkout. It may commit there. Same guard deny-list as workers: no push, remotes, publishing, PRs, and no
  edits outside its worktree or the project folder.
- Budget per message: `[chat.coworker] budget_usd`, default 2.0. The account usage pause
  (`pause_at_utilization`) applies: when paused, say so instead of continuing.
- Visible badge "Coworker: runs commands without asking" in the chat header.

## Where
- `agentdesk/server.py` (`Chat` class, mode switch, `can_use_tool` for coworker).
- `agentdesk/core.py` (worktree creation for `cowork`, guard rules, budget/pause checks; reuse the existing ones).
- `agentdesk/web/index.html` (badge).
- `data/settings.toml` defaults for `[chat.coworker]` (add them to the default file, don't rewrite the user's existing values).
- `README.md` (modes).

## Constraints
- Same guard rules as workers in `core.py`; don't weaken them.
- Never touch the user's checkout or `main`.
- Test with a temporary `AGENTDESK_HOME` and a scratch repo; keep `data/` intact. Live test with Haiku or sonnet on a tiny prompt only.

## Done when
- Switching to coworker shows the badge and the session resets with the same history.
- A coworker command runs without an approval card; a push or remote command is blocked by the guard.
- Edits land only in `<worktrees>/cowork` on `auto/cowork`.
- Spending past `budget_usd` in one message stops it with a clear message.
- Tests cover the guard rules and the budget stop.
