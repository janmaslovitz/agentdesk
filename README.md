# AgentDesk

Delegates coding tasks to Claude agents, choosing the cheapest model that can do each one,
across any number of projects and Claude accounts, with a dashboard to follow progress and spend
and a chat to talk to.

## Install (any computer)

Needs **Python 3.11+** and **git**. Copy this folder anywhere, then:

- Windows: right-click `install.ps1` → *Run with PowerShell* (or `powershell -ExecutionPolicy Bypass -File install.ps1`)
- macOS / Linux: `sh install.sh`

The Claude command-line tool comes bundled with the SDK; nothing else to install.

**Start:** double-click `AgentDesk.cmd` (Windows) or run `./agentdesk.sh`. The dashboard opens in its own
window titled "AgentDesk" (pywebview; no browser tab). The server runs in the background with no console
window at http://127.0.0.1:8765, reachable from this computer only. Closing the window does not stop the
server or runs; start again to get a window back, or use Quit / `stop`. Size and position are remembered
under `[window]` in `data/settings.toml` (existing values are only updated when the window closes). If the
window can't start (e.g. no WebView2), the error goes to `data/agentdesk.log` and the browser opens instead.
`python -m agentdesk dashboard --browser` always uses a browser tab; `--no-browser` only serves. Runs started from the dashboard are detached too, so they keep going if the dashboard
is closed. Output goes to `runner.log` (runs) and `data/agentdesk.log` (dashboard when started in the background).

**Stop:** `AgentDesk.cmd stop` (or `./agentdesk.sh stop`, `python -m agentdesk stop`) ends the dashboard
(PID in `data/dashboard.pid`). Add `--runs` to also stop runs started from the dashboard.

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

A plain folder works too. If it isn't a git repo (or is one with no commits), AgentDesk offers
"Set up local version tracking" (CLI: `add-project ... --local-git`). If accepted it runs `git init`,
writes a `.gitignore` only if there is none, and makes one commit, "AgentDesk: starting point"; the
current branch becomes the base. This is local only: nothing is pushed and no remote is added. If git
has no name/email, `AgentDesk <agentdesk@localhost>` is set for that repo only. Existing repos with
commits are never changed.

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

## The chat: Assistant and Coworker modes

Each project has a chat with a mode switch in its header, saved in the project's `project.toml`
as `chat_mode = "assistant"` or `"coworker"` (default assistant). Switching starts a new chat
session and keeps the messages on screen.

- **Assistant** (safe, cheap; model from `[chat.assistant] model` in `data/settings.toml`, default
  `haiku`). It reads the project's files, state, reports, logs and the repository without asking.
  It never edits your code: it may only write task files (`tasks/*.md`) and add to `notes.md`. It
  hands work to the workers with its own tools: `add_task` and `queue_status` (no approval) and
  `run_next`, `run_task`, `run_all`, `plan_task`, `stop_run`, which each show an approval card first.
- **Coworker** (autonomous) is planned for a later version; until then choosing it is saved but the
  chat behaves as the Assistant.

**Approvals.** When the chat wants to run a shell command (Bash/PowerShell) or start/stop a run, a
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
