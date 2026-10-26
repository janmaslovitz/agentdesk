+++
id = "006"
title = "Chat Planner mode: plan first, start work after approval"
tier = "hard"
depends = ["003", "004"]
+++

## Goal
Add a third chat mode, **planner**, next to assistant and coworker (toggle in the chat header, stored as
`chat_mode = "planner"` in `project.toml`).

Flow:
1. The user describes the work in the chat.
2. The planner reads the request and the repo, and writes a plan: an ordered list of tasks, each with a short
   goal, the files it touches, its tier, and its `depends`. It may write the proposed task files, but they
   stay marked as a draft until approved (e.g. a `status = "draft"` line in the TOML header, or a `tasks/drafts/` folder).
3. The plan is shown in the chat as a card: the task list, dependency order, and estimated cost range.
   Buttons: **Approve and start / Edit / Cancel** (reuse the approval card from task 003).
4. On approval, the drafts become real tasks and the queue starts automatically (same as "Run all"), with
   the planner's chat showing progress (which task started, finished, or is blocked).
5. The planner doesn't edit repository code itself. It doesn't start or stop runs except as part of the approved plan.

Rules:
- Model from settings `[chat.planner]`, default sonnet, effort medium; budget per message `[chat.planner] budget_usd`, default 2.0.
- Read/Glob/Grep only, plus the planning tools (`propose_plan`, `update_plan`, `cancel_plan`).
- Stopping the auto-started queue is one button in the chat (uses `stop_run`); it doesn't need an approval.
- If a task in the plan fails and goes to blocked, the planner reports it in the chat and waits. It doesn't
  rewrite the plan without asking.
- The account usage pause (`pause_at_utilization`) applies; when paused, it says so.
- One approved plan at a time per project.

## Where
- `agentdesk/server.py` (`Chat` class: planner mode, plan approval, auto-start hook after approval).
- `agentdesk/core.py` (draft handling in task loading: drafts are not run until approved; the queue start call).
- `agentdesk/web/index.html` (plan card, progress in chat).
- `data/settings.toml` defaults for `[chat.planner]` (add defaults only; don't rewrite the user's values).
- `README.md` (modes).

## Constraints
- Drafts must never be picked up by "Run next" / "Run all" before approval.
- Same guard rules as workers; no pushes or publishing.
- Test with a temporary `AGENTDESK_HOME` and a scratch repo; keep `data/` intact. Live test with a tiny request only.

## Done when
- Planner mode toggles and persists in `project.toml`.
- A request produces a draft plan (files in `tasks/` marked draft) and a plan card in the chat.
- Nothing runs before **Approve**; drafts are ignored by Run next / Run all.
- After approval, tasks run in dependency order without further clicks, and the chat shows progress.
- **Cancel** removes the drafts. **Edit** lets the user ask for changes, which update the draft plan.
- Tests cover draft exclusion from the queue, approval start, and cancel.
