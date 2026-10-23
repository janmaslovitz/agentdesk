# AgentDesk

Delegates coding tasks to Claude agents, choosing the cheapest model that can do each one,
across any number of projects and Claude accounts, with a dashboard to follow progress and spend
and a chat to talk to.

## Downloads

Every release on the [Releases page](https://github.com/janmaslovitz/agentdesk/releases/latest) has ready-made builds
(no Python needed):

- **Windows:** `AgentDesk-Setup-x.y.z.exe`, the installer (see [Install](#install)). It updates itself from the app.
- **macOS:** `AgentDesk-x.y.z-macos-arm64.zip` (Apple silicon). Unzip it and move `AgentDesk.app` to Applications.
  The build is **not signed**: the first time, right-click the app → *Open* → *Open* (on recent macOS: try to open it
  once, then System Settings → Privacy & Security → *Open Anyway*; or run `xattr -dr com.apple.quarantine AgentDesk.app`).
- **Linux:** `AgentDesk-x.y.z-linux-x86_64.tar.gz` (built on Ubuntu 22.04). Unpack it anywhere and run `AgentDesk/AgentDesk`.
  The window needs WebKitGTK (`sudo apt install gir1.2-webkit2-4.1`); without it the dashboard opens in the browser.

On macOS and Linux the "Update available" button opens the release page; download the new build and replace the old one.
The data stays where it is: `~/Library/Application Support/AgentDesk` (macOS), `~/.local/share/AgentDesk` (Linux,
or `$XDG_DATA_HOME/AgentDesk`), unless a `data` folder sits next to the program.

## Install (any computer)

Needs **Python 3.11+** and **git**. Copy this folder anywhere, then:

- Windows: right-click `install.ps1` → *Run with PowerShell* (or `powershell -ExecutionPolicy Bypass -File install.ps1`)
- macOS / Linux: `sh install.sh`

The Claude command-line tool comes bundled with the SDK; nothing else to install.

**Start:** double-click `AgentDesk.cmd` (Windows) or run `./agentdesk.sh`. The dashboard opens in its own
window titled "AgentDesk" (pywebview; no browser tab). The server runs in the background with no console
window at http://127.0.0.1:8765, reachable from this computer only. Closing the window does not stop the
server or runs: with the optional tray (`pip install -r requirements-tray.txt`) the X hides the window to a tray icon,
where click or "Open" shows it again and "Quit" closes the window and stops the server (runs keep going); without it
the window just closes (reason in `data/agentdesk.log`) and you start again to get a window back, or use `stop`. Size and position are remembered
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

**Usage reading.** The gauge uses the newest reading of the account's usage, with its
source and age. Source `sdk`: the percentage Claude Code reports in its rate-limit events during a
run or chat (often missing, then there is no automatic reading). Source `manual`: type the number
under **Current usage: [ ] %** in the account row and press **Save**. A reading older than
`[limits] usage_max_age_min` (default 30) is shown as stale. Usage never blocks or pauses runs or the
Coworker (there is no threshold); Anthropic's own limit still applies. Only the percentage, time and source are stored
(`data/accounts/<name>.json`, key `reading`; no credentials).

**Cost per run.** Each run/attempt in `state.json` records `usage_before`, `usage_after` and
`usage_used_pct` (percent points; null = unknown, shown as "unknown", e.g. when no newer reading
arrived during the run). The dashboard shows per task "used about X% of your window" and, above
the task list, the average per tier.

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

## MCP servers

A project can give its workers extra MCP servers in `project.toml`:

```toml
[mcp.code-graph]
command = "npx"
args = ["-y", "@sdsrs/code-graph"]
env = { CODE_GRAPH_NO_AUTO_UPDATE = "1" }
enabled = true                  # default false
roles = ["worker"]              # default ["worker"]; "router" is not supported yet (its tools are read-only and not pre-approved)
```

Only `enabled = true` entries for the role are passed to the agent; `npx` is resolved with `which` (`npx.cmd` on
Windows). The server runs in the agent's working directory (the worker's worktree). `allowed_tools` is not
widened and the worker guard allows `mcp__*` tools. Invalid entries are skipped with a log line.
`.code-graph/` is never committed by the orchestrator. Do not put secrets in `env`: it reaches the CLI as a
command-line argument; use the OS environment instead.

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

## The Git window

Each project's **Git** section shows the branch, what is ahead/behind, the `auto/*` branches, and the repository
it is connected to (`origin`). Everything that changes something asks first; nothing happens on its own.

- **Refresh** fetches and reloads the lists below. **Merge into base**, **Pull** (fast-forward only) and **Push**
  (the current or the base branch, never forced; sets the upstream when there is none) work as before.
- **Connect to**: paste `https://github.com/owner/name`, `git@github.com:owner/name.git` or just `owner/name`, or
  press **List** to pick one of the repositories of an account or organization (`gh repo list`). **Connect…**
  sets `origin` (`git remote set-url`, or `add` when there is none); git's error is shown as it is.
- **GitHub account**: the accounts the GitHub CLI knows (`gh auth status`; ★ = gh's active one). **Use for this
  project** saves it in `project.toml` as `[git] account = "name"` (not exported: it is per computer); pull, push,
  fetch and the pull-request calls then run as that account, and each result says which account it used. The
  token comes from `gh auth token` and is passed only in that one command's environment: it is never shown,
  logged or stored. ssh and local remotes keep git's own credentials. **Make active in gh** runs
  `gh auth switch -u <name>`, which changes gh for every program on this computer.
- **Pull requests** lists the open ones (`gh pr list`), shows a PR's checks on demand and opens it in the
  browser. **New pull request** (from branch, into, title, description, draft) runs `gh pr create`; push the
  branch first. Merging a pull request is left to GitHub.

Needs the [GitHub CLI](https://cli.github.com/) for the account and pull-request parts (`gh auth login` once;
`gh auth login` again to add another account). Without it, or when it isn't logged in, the window says what to
run; fetch, pull, push and merge keep working. gh answers are reused for 5 seconds; **Refresh** asks again.

## The Build panel

Next to the Git section (half the width each; stacked on narrow windows). It builds a version made of the base
branch plus the finished tasks you choose, without touching the base branch:

- **Version**: *New build (next)* proposes the next patch version after the newest of the release tags `vX.Y.Z`,
  the top version in `CHANGELOG.md` and `__version__` (editable), or pick an existing release tag to build that
  release plus tasks.
- The list holds the finished tasks (`done` in state.json) whose branch `auto/<id>` still exists, with a
  *merged*/*unmerged* badge. Tasks already in the version's base (the base branch, or the tag) are ticked and
  greyed out (*already in*); tasks finished since the last tag are ticked by default. **Select all / none**.
- **Build** (re)creates the branch `build/<version>` from that base in the build worktree (`<project>/build/src`),
  merges the ticked `auto/<id>` branches into it one by one and then runs the build command (`[build] command` of
  project.toml when set, else `build_exe.ps1`). A merge conflict stops it: the merge is aborted, nothing is built,
  and the result line names the task and files. The base branch and the task branches never change and nothing is
  pushed. The result line shows the version, the tasks, success or failure and a link to the output folder; the
  last choice is remembered in `<project>/build/selection.json`.
- **More ▾** keeps the plain builds: a branch or commit as it is, **Clean + rebuild** (fresh worktree) and
  **Custom build**. **Stop** ends a running build and its child processes; the log is under **Log**.

The list refreshes when a task finishes; the log is polled only while a build runs. API: `GET /api/p/<slug>/build`
(adds `versions` and `tasks`; `?version=` picks one, `?lite=1` skips them) and `POST /api/p/<slug>/build` with
`version` and `tasks` (ids of finished tasks; anything else is refused with 400).

## Disk hygiene

Each project's **Disk** section shows what it uses on disk, measured in the background (never while a dashboard
request waits; cached for 5 minutes, **Measure again** asks now): every cargo target dir it uses (`CARGO_TARGET_DIR`
from `[gates.env]`, `[hygiene] target_dirs`, `<repo>/target`, `<worktree>/target`, and `target*` folders with a
`CACHEDIR.TAG`/`.rustc_info.json` next to the repository, the worktrees folder or the configured target dir) with
their profiles (debug, release ...), the worktrees and the project's AgentDesk data folder. It warns when a target dir
is over `max_target_gb` or the project over `max_total_gb`, and lists `[gates.env]` settings that make caches grow
(CARGO_INCREMENTAL, debug info, one target shared by many worktrees); project.toml is never edited.

**Clean up** first shows a dry run (what would be deleted and how much); only **Delete selected** (confirmed) deletes:
- a target dir over its limit, or any when the project is over its total: files in `<profile>/deps`, `build`,
  `.fingerprint`, `incremental` untouched for `stale_days` (cargo rebuilds what it needs); if the dir is still over
  `max_target_gb`, the whole profile folder (e.g. `debug`, a build cache only);
- per-task target dirs (`target-017`) no worktree or open task uses, untouched for `stale_days`;
- worktrees of tasks that are done and merged into the base (or whose branch is gone) and have no uncommitted or
  untracked files: `git worktree remove --force`, then `git worktree prune`; `auto/*` branches merged into the base
  that no worktree and no open task needs (deleted after checking the merge again). Nothing is pushed;
- run timelines (`runs/*.jsonl`) over `max_runs_mb` (oldest first, older than `stale_days`) and `agentdesk.log`
  / `runner.log` over `max_log_mb` (the newest half is kept). Reports and state.json are never touched.

Every deletion is checked again right before it happens: only inside the project's configured locations, only in
folders that are clearly cargo target dirs (CACHEDIR.TAG, .rustc_info.json or named `target*`, no `.git`/`Cargo.toml`),
never through a symlink or junction, never while a run or build is going for the project or a cargo/rustc/rustup
process (or a program started from inside the folder) uses it. Processes come from `psutil` if installed, else
PowerShell `Get-CimInstance` (Windows) or `/proc`; if they can't be listed, nothing is deleted. Each action and the
space it freed goes to the project's `agentdesk.log`.

Settings (`data/settings.toml` `[hygiene]`, overridable per project in `project.toml` `[hygiene]`; a bad value is
ignored and logged): `enabled = true`, `auto = false` (true: the background job also cleans up by itself every
`interval_min = 30` minutes), `max_target_gb = 60`, `max_total_gb = 150`, `stale_days = 3`, `max_runs_mb = 200`,
`max_log_mb = 20`, `target_dirs = []`.

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
  The chat header shows the badge *Coworker: full access, no approvals*. It is the project's curator:
  it works **in the project checkout itself, main included**, with every tool and shell command and
  the queue tools (`add_task`, `run_next`, `run_task`, `run_all`, `plan_task`, `stop_run`,
  `queue_status`), **without approval cards and without the guard**. It reviews and merges the
  workers' branches, edits and commits on the base branch, tidies branches and worktrees, and pushes
  or publishes when asked. Its instructions tell it to protect uncommitted work, prefer reversible
  commands and run the checks before committing; nothing enforces that. Set
  `[chat.coworker] full_access = false` to get the older, guarded mode instead: own worktree
  `<worktrees>/cowork` on branch `auto/cowork`, the limits below. Limits of that guarded mode:
  - the workers' guard (`[guard] deny` in `settings.toml`): no push, remotes, publishing, pull
    requests, hard resets or switching to main; shell commands naming `project.toml` or
    `settings.toml` are refused too. Edits are allowed only in its worktree, `tasks/` and
    `notes.md` of the project folder. The check runs before every tool call (also the ones the
    Claude CLI would allow by itself), and a refused call is reported in the chat.
  - a budget per message, `[chat.coworker] budget_usd` (default 5.0). Each message runs in a fresh
    Claude process that resumes the conversation, so the budget counts that message only; when it's
    reached the answer ends with *Stopped: … reached the Coworker budget*. The check runs after each
    model turn, so a message can go over by one turn.


  After approval the chat reports each plan task starting, finishing (branch and commit) or being
  blocked, and the run ending. A blocked task is only reported: the plan waits for you, and the
  Planner doesn't rewrite an approved plan. The card's **Stop** stops the run without an approval
  card; **Start** goes on; **Close** forgets a stopped or finished plan (its tasks stay in the
  queue). One approved plan per project at a time.

**Live Coworker** (default; `[chat.coworker] live = true`, needs `full_access`). The Coworker is one
persistent session instead of a fresh process per message, and works like a teammate:

- Its answers, tool calls and background-task events stream into the chat as they happen, and you can
  keep writing while it works (the **Stop** bar above the input interrupts it).
- It can run long commands in the background and is woken when they finish, launch **subagents**
  (`explorer` on Haiku for reading and searching, `reviewer` for diffs and branches, `implementer` for one
  focused change) and run several in parallel.
- Its AgentDesk tools: `checkup` (queue, run, failures, unmerged branches, usage and spending in one report),
  `watch_run` (wakes it when the current run ends; nothing is spent while waiting) and `schedule_checkup`
  (wakes it in N minutes).
- With `checkup_interval_min > 0` it checks the project by itself that often, but only while a session is
  open (you have chatted since the dashboard started), it is idle, and the daily budget isn't used up.
  Default 0 (never).
- Spending: `budget_usd` (default $5) is checked at the end of each burst of work (a message or a wake-up);
  `daily_budget_usd` (default $25, 0 = no cap) caps everything the Coworker and its subagents spend per day
  and blocks new work when reached. A persistent session reuses the prompt cache, so it is usually cheaper
  per message than a fresh process each time; waiting is free.

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

## Build the program

Make `AgentDesk.exe` (Windows) so AgentDesk starts without Python or `.venv`:

```powershell
.\build_exe.ps1                      # builds dist\AgentDesk\AgentDesk.exe and a Desktop shortcut "AgentDesk"
.\build_exe.ps1 -NoDesktopShortcut   # build only
```

The script makes a build venv (`.venv-build`), installs `requirements.txt` + `requirements-build.txt` (PyInstaller) and runs `agentdesk.spec`.
Run the whole `dist\AgentDesk` folder from anywhere (copy it as a whole). Double-click `AgentDesk.exe` to open the window; the icon is `agentdesk\web\agentdesk.ico`.
The shortcut is created if missing and updated on re-runs.

Data goes in `AGENTDESK_HOME` if set; otherwise the program uses a `data` folder next to the `.exe` if one is already there (copy an existing `data\` in by hand), else `%LOCALAPPDATA%\AgentDesk`. From source it is `data\` in the repo as before.
The build never contains `data\`, accounts, logins or keys.
The `.cmd`/`.sh` launchers still run the source version.

On macOS / Linux, `bash build_unix.sh` does the same with the same spec and packs the result into `dist/`:
`AgentDesk-x.y.z-macos-<arch>.zip` (an `AgentDesk.app`; icon from `agentdesk/web/agentdesk.icns` if you add one) or
`AgentDesk-x.y.z-linux-<arch>.tar.gz` (the `AgentDesk` folder). On Linux it also needs the GTK / WebKitGTK development
packages for the window (the list is in `.github/workflows/release.yml`). You can only build for the system you are on.

## Build the installer

Make `AgentDesk-Setup.exe` (Windows) from a clean checkout. It needs Python 3.11+ and [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`):

```powershell
.\build_installer.ps1               # runs build_exe.ps1, then ISCC.exe on installer\AgentDesk.iss
.\build_installer.ps1 -Version 1.2  # version shown in Apps & features
```

The result is `dist\installer\AgentDesk-Setup.exe`. It packs the program only (`dist\AgentDesk`): no `data\`, logins, keys, projects, `.venv`, worktrees or `__pycache__`.
It installs neither Python nor Claude Code.

## Install

Run `AgentDesk-Setup.exe`. It asks for administrator rights and installs to `C:\Program Files\AgentDesk`; the folder page lets you pick another folder.
Choose "install for me only" (or run with `/CURRENTUSER`) to install without elevation, e.g. into your own folder.
It adds a Start menu entry "AgentDesk" (opens the window, with the tray icon), an optional desktop icon (unchecked), and an entry in Apps & features.
If the Claude Code CLI or the WebView2 runtime is missing, setup says so with a download link and carries on.

- **Data** goes to `%LOCALAPPDATA%\AgentDesk` (logins, projects, settings), never into Program Files. A `data` folder next to `AgentDesk.exe` is used instead if you put one there. Setup never deletes or overwrites it.
- **Uninstall** from Apps & features (or the Start menu). It removes the program files and asks "Also remove my data?"; `%LOCALAPPDATA%\AgentDesk` stays unless you answer Yes. A silent uninstall keeps it unless `/REMOVEDATA` is given.
- Upgrade by running a newer setup over the old one; the data is kept.

## Versions and releases

AgentDesk has one version number, `__version__` in `agentdesk/__init__.py` (semantic: `major.minor.patch`). It is
shown in the dashboard sidebar (with the commit and build time), by `AgentDesk.exe version`, in the exe's file
properties, and in the installer (`AgentDesk-Setup-x.y.z.exe`, listed in Apps & features). The app notes in its log
(`agentdesk.log`) when it starts after an upgrade, and `data/app_version.json` remembers the last version that ran.

`CHANGELOG.md` lists what changed in each version. Write new entries under `## Unreleased`; then make the release:

```
.\release.ps1                  # runs the tests, then 1.1.0 -> 1.1.1: commit "Release 1.1.1", tag v1.1.1, build the installer
.\release.ps1 -Bump minor      # new features: 1.1.0 -> 1.2.0   (-Bump major for a breaking change)
.\release.ps1 -Version 1.4.0   # an exact version (must be newer)
```

It refuses to run if the tests fail, if tracked files have uncommitted changes, if the Unreleased section is empty, or if
the tag exists. It never pushes (`git push --follow-tags` when you want to). Every release installer stays in
`dist\installer\` (`AgentDesk-Setup.exe` is always a copy of the newest), so going back is installing an older one.
Installing a newer version over an older one keeps your data (`%LOCALAPPDATA%\AgentDesk`).

**Release builds in CI.** Pushing the tag (with `--follow-tags`, as above) starts the GitHub Actions workflow
`.github/workflows/release.yml`: it checks that the tag matches `__version__`, runs the tests once (Ubuntu), then builds on
Windows (`build_installer.ps1`, Inno Setup from Chocolatey), macOS and Ubuntu 22.04 (`build_unix.sh`) and attaches
`AgentDesk-Setup-x.y.z.exe`, `AgentDesk-x.y.z-macos-<arch>.zip` and `AgentDesk-x.y.z-linux-x86_64.tar.gz` to the
GitHub Release of the tag. A missing release is created as a draft, with the notes of that version from `CHANGELOG.md`
(`python tools/release.py notes x.y.z`), and published once all three builds are attached; if a build fails it stays a
draft. To rebuild a tag, run the workflow by hand (Actions → Release → *Run workflow*, tag `vx.y.z`): existing assets
are replaced. It uses only `GITHUB_TOKEN`; nothing is signed.

## AgentDesk Server (several users)

`python -m agentdesk serve` runs the dashboard without a window (`--host`, `--port`, `--data`, `--auth-token-file`).
For several people on one machine, run the **gateway** in front of it:

```
python -m agentdesk gateway --home /srv/agentdesk user add alice --admin   # asks for the password
python -m agentdesk gateway --home /srv/agentdesk user add bob
python -m agentdesk gateway --home /srv/agentdesk                          # listens on 0.0.0.0:8080
```

Users open `http://<server>:8080`, sign in at `/login` (sign out at `/logout`) and get their own AgentDesk: the gateway
starts `python -m agentdesk serve --host 127.0.0.1 --port <free port> --data <home>/users/<name>` for that user on first
use, passes everything (API, chat, uploads, export zips, streamed answers) through to it with a per-instance secret in
`X-AgentDesk-Gateway-Token`, restarts it if it dies and stops it after `idle_stop_minutes` without requests (never while a
run, chat turn or build is going on: the gateway asks `GET /api/busy`). Instances listen on loopback only. Which instance
a request reaches comes only from the server-side session, never from anything the browser sends.

The server home (`--home`, else `AGENTDESK_SERVER_HOME`, else `~/agentdesk-server`) holds:

```
server.toml         settings (written with comments on first start): host, port, tls_cert/tls_key, allowed_hosts,
                    session_hours, session_idle_minutes, login_max_failures, login_lockout_minutes, idle_stop_minutes,
                    run_as_users, run_as
users.json          users: role (admin|user), scrypt password hash (salted), disabled, created, home, projects, accounts
users/<name>/       each user's AgentDesk data folder
audit.log           sign-ins, failed sign-ins, lockouts, user changes, instance starts/stops (JSON lines; no passwords)
instances/, logs/   per-instance secrets (0600) and instance output
```

Users: `user list | add NAME [--admin] | passwd NAME | disable NAME | enable NAME | remove NAME | role NAME admin|user |
assign NAME --project SLUG --account NAME` (`--password-stdin` reads the password from standard input). A password change,
disabling or removing a user ends that user's sessions. Sessions live in the gateway's memory (a restart signs everyone
out); the cookie is HttpOnly and SameSite=Strict (and Secure with TLS). State-changing requests must come from the
gateway's own origin or carry the session's CSRF token (`X-AgentDesk-CSRF`, from `GET /gateway/me`). After
`login_max_failures` wrong passwords for one user from one address (or many from one address) that pair is locked for
`login_lockout_minutes`.

**Admin cabinet.** Admins open `/admin` (link "Admin" in the bar the gateway adds to every page): users, per-user projects
(git URL or exported .zip), Claude accounts, instances (start / stop / restart, log tail, disk free) and the audit log.
Accounts are kept in `<server-home>/accounts.json` and written to the assigned user's `settings.toml`; an API-key account
only names the environment variable holding the key (set it where the gateway runs). Everyone changes their own password
at `/account`.

**HTTPS:** set `tls_cert` and `tls_key` (PEM files) in server.toml. Without TLS, passwords cross the LAN unencrypted.

**Separate OS accounts (optional, Linux).** Without it every instance runs as the gateway's OS user, so the agents of one
user could read another user's files on disk; the gateway itself never lets one user reach another's instance. With
`run_as_users = true` each instance runs as OS user `agentdesk-<username>`, its secret is passed on standard input:

1. Create the accounts: `sudo useradd --system --home-dir /srv/agentdesk/users/alice --shell /usr/sbin/nologin agentdesk-alice`,
   then `sudo chown -R agentdesk-alice: /srv/agentdesk/users/alice && sudo chmod 700 /srv/agentdesk/users/alice`.
2. The app folder (this repository) and Python must be readable by those accounts; each one needs its own Claude login.
3. Let the gateway's user (say `agentdesk`) start them without a password, `/etc/sudoers.d/agentdesk`:
   `agentdesk ALL=(%agentdesk-users) NOPASSWD: /usr/bin/python3 -m agentdesk serve *` (put the accounts in group
   `agentdesk-users`, or list them). The gateway runs `sudo -n -u agentdesk-<username> -- python -m agentdesk serve ...`.
4. Or set `run_as = "systemd-run"` when the gateway runs as root: it uses `systemd-run --pipe --uid=agentdesk-<username>`.

## Server (Debian, headless)

For a Debian / Ubuntu machine without a desktop (needs Python 3.11+, systemd). The `.deb` from the GitHub Release
installs the gateway of the previous section as a service, with the console tool `agentdesk-server`.

**Install.**

```
sudo apt install ./agentdesk-server_X.Y.Z_all.deb      # pulls python3, python3-venv, git; postinst builds /opt/agentdesk-server/venv
sudo apt install nodejs npm && sudo npm install -g @anthropic-ai/claude-code   # the claude CLI (not packaged)
sudo -u agentdesk -H claude                             # sign in once (or use API-key accounts in /admin)
```

The postinst installs the Python packages with pip (internet needed; build the package with `--bundle-wheels` on the
same distro for an offline install). Layout: program `/opt/agentdesk-server`, settings `/etc/agentdesk-server/server.toml`,
state `/var/lib/agentdesk-server` (user `agentdesk`), logs `journalctl -u agentdesk-server`. To build the package yourself:
`python3 packaging/debian/build_deb.py` (on Debian / Ubuntu, needs `dpkg-deb`) writes `dist/agentdesk-server_X.Y.Z_all.deb`.

**Init and first login.**

```
sudo agentdesk-server init                       # creates server.toml and the first admin (asks for the password)
sudo agentdesk-server init --admin alice --admin-password-file /root/pw   # unattended
sudo systemctl enable --now agentdesk-server
sudo agentdesk-server doctor                     # git, claude CLI + login, port, permissions, disk
```

Open `http://<server>:8080/`, sign in as the admin. Everything below works over SSH too:
`agentdesk-server status`, `user add NAME [--admin] | list | passwd NAME | disable NAME | enable NAME | delete NAME [--purge]`,
`config get [KEY]`, `config set host|port|idle_timeout|run_as_users|tls ...` (`config set tls CERT KEY`, `config set tls off`;
restart the service afterwards). Passwords are asked on the terminal or read from a file, never taken as arguments.

**Adding users and projects.** Add users with `agentdesk-server user add bob` or in the admin cabinet (`/admin`), which also
adds projects to a user (git URL cloned on the server, or an exported zip) and assigns Claude accounts. Each user gets an
own AgentDesk with its own data folder `/var/lib/agentdesk-server/users/<name>/`.

**Updating.** `sudo apt install ./agentdesk-server_NEW_all.deb`: settings and state are kept, the service is restarted.
Users' running instances stop with it (runs in progress are interrupted), so update when nobody is working.

**Backup.** Stop the service (or accept a slightly inconsistent copy) and copy `/var/lib/agentdesk-server` (users.json
with password hashes, project data, audit log, the Claude login in `home/`) and `/etc/agentdesk-server/server.toml`, e.g.
`sudo systemctl stop agentdesk-server && sudo tar czf agentdesk-backup.tgz /var/lib/agentdesk-server /etc/agentdesk-server && sudo systemctl start agentdesk-server`.
Keep the archive private: it holds credentials. Restore by unpacking it over the same paths.

**Security.**
- LAN only. The gateway listens on `0.0.0.0:8080` by default; never forward the port or expose the server to the public
  internet. Limit it with the firewall, e.g. `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`.
- Put TLS in front: without it passwords cross the LAN in clear text. Either `agentdesk-server config set tls CERT KEY`, or
  nginx / Caddy as a reverse proxy (examples in `/usr/share/doc/agentdesk-server/`: `nginx-agentdesk.conf.example`,
  `Caddyfile.example`), then `agentdesk-server config set host 127.0.0.1` so only the proxy can reach the gateway.
- The agents run code and shell commands as the OS user `agentdesk`; all users share that account unless you enable
  `run_as_users` (see above; edit the unit with `systemctl edit agentdesk-server` and set `NoNewPrivileges=false`, since sudo
  needs it). The unit is hardened (`ProtectSystem=full`, `ProtectHome=true`, `PrivateTmp`, `NoNewPrivileges`): repositories
  outside `/var/lib/agentdesk-server` need `ReadWritePaths=` in a drop-in.
- Use strong passwords; repeated wrong passwords lock the user and address for a while (see above).

**Troubleshooting.**
- `sudo agentdesk-server doctor` first; then `journalctl -u agentdesk-server -e` and `systemctl status agentdesk-server`.
- *Packages missing / service fails to import*: the postinst could not reach PyPI. Run `sudo apt install --reinstall ./agentdesk-server_*.deb`.
- *Page does not load from another computer*: firewall, or `host` is `127.0.0.1` (`agentdesk-server config get host`);
  behind a proxy set `allowed_hosts` to the name users type.
- *"claude not found" / not signed in*: install the CLI, then `sudo -u agentdesk -H claude`; the service's `PATH` must contain it
  (`/usr/bin` or `/usr/local/bin` do).
- *Port in use*: `agentdesk-server config set port 8081` and restart.
- *Forgot the admin password*: `sudo agentdesk-server user passwd NAME`.

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
