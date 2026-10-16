# AgentDesk

Delegates coding tasks to Claude agents, choosing the cheapest model that can do each one,
across any number of projects and Claude accounts, with a dashboard to follow progress and spend
and a chat to talk to.

## Install (any computer)

Needs **Python 3.11+** and **git**. Copy this folder anywhere, then:

- Windows: right-click `install.ps1` → *Run with PowerShell* (or `powershell -ExecutionPolicy Bypass -File install.ps1`)
- macOS / Linux: `sh install.sh`

The Claude command-line tool comes bundled with the SDK; nothing else to install.

**Start:** double-click `AgentDesk.cmd` (Windows) or run `./agentdesk.sh`. The dashboard opens at
http://127.0.0.1:8765, reachable from this computer only.

## Moving to another computer

Copy the whole folder (with `data/`) and run the installer there; the `.venv` folder can be left
out, since the installer rebuilds it. Repository paths in `data/projects/*/project.toml` may need
editing if the repos live elsewhere on the new computer. Logins (`data/logins/`) are per computer
by nature: press **Log in** again there. To keep the data somewhere else (e.g. a synced folder),
set the `AGENTDESK_HOME` environment variable to that folder.

## Accounts

Sidebar → Accounts → **+ Add**:
- **Claude subscription**: a separate login kept in its own folder. Press **Log in**; a console
  opens; type `/login` and sign in with that account. The account named `default` uses your
  normal Claude Code login.
- **API key**: name an environment variable (e.g. `ANTHROPIC_API_KEY_WORK`) holding the key. AgentDesk
  reads it when it starts and never stores keys.

Each project uses one account (chosen when adding it; changeable in its `project.toml`). The
usage gauge is per account: runs pause when the account's usage reaches the threshold.

## Projects

Sidebar → Projects → **+ Add**: name, the repository folder (a git repo), the branch tasks start
from, a checks preset (Rust, Node, Python, Go, none) and the account. The checks run after every
attempt; edit them in `data/projects/<project>/project.toml` (`[gates] quick` after each attempt,
`final` on demand; `[gates.env]` environment for them).

## How work flows

1. You add tasks: **+ Task**, or describe the work to the project's assistant in the chat (it
   writes the task for you).
2. **Run next** / **Run all**: a cheap router (Haiku) reads the task and the repo, picks a tier
   and writes a brief. The worker — Haiku (mechanical), Sonnet (standard) or Opus at high effort
   (hard) — does it in its own git worktree on branch `auto/<id>`, never touching your checkout.
3. AgentDesk runs the checks itself (no model tokens). Failures go back to the worker, trimmed;
   after two failed attempts the task moves up a tier; after the top tier it's marked blocked.
4. Green work is committed on its branch. Nothing is ever pushed, released or published:
   you review the `auto/*` branches and merge what you want.

Limits: a budget per call and per run, and a pause at the account's usage threshold
(`data/settings.toml`, `[limits]`). Workers can't push, change remotes, publish or edit outside
their worktree (`[guard]`).

## The chat: Assistant, Coworker and Planner modes

Each project has a chat with a mode switch in its header, saved in the project's `project.toml`
as `chat_mode = "assistant"`, `"coworker"` or `"planner"` (default assistant). Switching starts a
new chat session and keeps the messages on screen.

- **Assistant** (safe, cheap; model from `[chat.assistant] model` in `data/settings.toml`, default
  `haiku`). It reads the project's files, state, reports, logs and the repository without asking.
  It never edits your code: it may only write task files (`tasks/*.md`) and add to `notes.md`. It
  hands work to the workers with its own tools: `add_task` and `queue_status` (no approval) and
  `run_next`, `run_task`, `run_all`, `plan_task`, `stop_run`, which each show an approval card first.
- **Coworker** (autonomous; model from `[chat.coworker]`, default `sonnet` at `effort = "medium"`).
  The chat header shows the badge *Coworker: runs commands without asking*. It reads and analyzes
  freely, runs shell commands and uses the same tools (`add_task`, `run_next`, `run_task`, `run_all`,
  `plan_task`, `stop_run`, `queue_status`) **without approval cards**. It changes code only in its
  own worktree `<worktrees>/cowork` on branch `auto/cowork` (created from the base branch on first
  use, kept afterwards) and may commit there; review and merge `auto/cowork` like the workers'
  branches. Limits:
  - the workers' guard (`[guard] deny` in `settings.toml`): no push, remotes, publishing, pull
    requests, hard resets or switching to main; shell commands naming `project.toml` or
    `settings.toml` are refused too. Edits are allowed only in its worktree, `tasks/` and
    `notes.md` of the project folder. The check runs before every tool call (also the ones the
    Claude CLI would allow by itself), and a refused call is reported in the chat.
  - a budget per message, `[chat.coworker] budget_usd` (default 2.0). Each message runs in a fresh
    Claude process that resumes the conversation, so the budget counts that message only; when it's
    reached the answer ends with *Stopped: … reached the Coworker budget*. The check runs after each
    model turn, so a message can go over by one turn.
  - the account usage pause (`[limits] pause_at_utilization`): if the account's last reading is at or
    over the threshold, the Coworker answers *Paused: …* instead of starting; if a reading crosses it
    during a message, it stops there and says so.
- **Planner** (plan first, work after your approval; model from `[chat.planner]`, default `sonnet`
  at `effort = "medium"`, `budget_usd = 2.0` per message, the same usage pause as the Coworker).
  Describe the work; the Planner reads the repository and the queue (Read, Glob, Grep only: it
  can't edit files, run commands or start runs) and writes a plan with `propose_plan`: ordered
  tasks, each with a goal, the files it touches, a tier and `depends`. The plan is a draft:
  `plan.json` in the project folder plus draft task files in `tasks/drafts/` marked
  `status = "draft"`, which Run next, Run all and Run never pick up. The chat shows it as a plan
  card with the dependency order and a rough cost range, and three buttons:
  - **Approve and start**: the drafts become real tasks (next free ids, in dependency order, their
    `depends` mapped) and the queue starts as with **Run all** (the whole ready queue, not only the
    plan). If a run is already going, the plan's tasks start when it ends; if the account is
    paused, they're queued and the chat says so (press **Start** on the card later).
  - **Edit**: type what should change; the Planner rewrites the draft with `update_plan`.
  - **Cancel**: deletes the draft plan and its draft files.

  After approval the chat reports each plan task starting, finishing (branch and commit) or being
  blocked, and the run ending. A blocked task is only reported: the plan waits for you, and the
  Planner doesn't rewrite an approved plan. The card's **Stop** stops the run without an approval
  card; **Start** goes on; **Close** forgets a stopped or finished plan (its tasks stay in the
  queue). One approved plan per project at a time.

**Approvals** (Assistant mode). When the chat wants to run a shell command (Bash/PowerShell) or start/stop a run, a
card appears under the messages: **Allow once**, **Always allow** or **Deny**. Nothing runs before
you answer; no answer within 10 minutes counts as Deny.

**Allow-list.** **Always allow** (shell commands only) saves a command prefix — prefilled as the
command and its subcommand, editable before you press it — in the project's `project.toml`:

```toml
[chat]
allowed_commands = ["git status", "git log", "cargo test -p"]
```

A command then runs without a card if it equals an entry or starts with it followed by a space
(`git log --oneline -5` matches `git log`; `git logs` doesn't). Only single plain commands qualify:
anything with `; & | > < ( ) { } $` or a backtick or newline always asks, and so does anything the
`[guard] deny` list in `settings.toml` matches. Edit or empty the list in `project.toml` at any time.

Every tool call shows in the chat (command or arguments, with its output folded under it), and
each answer shows its cost and tokens; the chat's totals are in the **Chat** card.

## Command line

```
AgentDesk.cmd projects                     (or ./agentdesk.sh …)
AgentDesk.cmd add-project NAME REPO BASE --preset rust --account default
AgentDesk.cmd status PROJECT
AgentDesk.cmd run PROJECT [TASK] [--max N]
AgentDesk.cmd plan PROJECT [TASK]
AgentDesk.cmd final PROJECT TASK
AgentDesk.cmd import NAME OLD_ORCHESTRATOR_DIR
```

## Files

```
agentdesk/            the app (core.py pipeline, server.py dashboard, web/index.html)
data/settings.toml    accounts, models per tier, limits, guard
data/accounts/        usage-limit readings per account
data/logins/          separate Claude logins (subscription accounts)
data/projects/<p>/    project.toml, tasks/, reports/, notes.md, state.json, logs, worktrees/
```
