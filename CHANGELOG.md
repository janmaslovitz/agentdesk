# Changelog

What changed in each AgentDesk version, newest first. Write new entries under **Unreleased**; `release.ps1` moves
them under the new version number, tags it and builds the installer (see README, "Versions and releases").

## Unreleased


## 1.3.2 - 2026-10-23

### Fixed
- The dashboard could show no projects (and a 404 on every project) after a crash or kill: a data file written at that
  moment, such as the account usage file, was left as zeros, and reading it failed every request. Data files are now
  flushed to disk before they replace the old one, a file that can't be read is set aside as `<name>.corrupt-<time>`
  (and noted in the log) instead of failing, `state.json` falls back to its last backup (`state.json.bak`, kept at most
  once a minute) instead of an empty queue, and the web server restarts its own loop if it ever dies (a window could stay
  open on a server that no longer answered).

## 1.3.1 - 2026-10-23

### Added
- macOS and Linux downloads: a GitHub Actions workflow (`.github/workflows/release.yml`) builds every `v*` tag on
  Windows, macOS and Linux and attaches `AgentDesk-Setup-x.y.z.exe`, `AgentDesk-x.y.z-macos-<arch>.zip` (unsigned
  `AgentDesk.app`) and `AgentDesk-x.y.z-linux-x86_64.tar.gz` to its GitHub Release, with the notes from this file. It
  can also rebuild a given tag by hand. `build_unix.sh` builds and packs on macOS / Linux; `tools/release.py notes`
  prints a version's notes.

### Changed
- `agentdesk.spec` works on every platform (the Windows icon, version resource and imports only on Windows).
- On macOS and Linux the "Update available" button opens the release page instead of installing (the in-app updater
  stays Windows-only).
- The packaged program on macOS / Linux keeps its data in `~/Library/Application Support/AgentDesk` /
  `~/.local/share/AgentDesk`, and `AgentDesk stop` recognises it there. Windows is unchanged.

## 1.3.0 - 2026-10-23

### Added
- Quick-question buttons above the chat (what is the agent doing, queue status, what needs attention, spending, tool stats). The chat reads the agent timeline with the new `task_activity` tool; `queue_status` shows a "now:" line for running tasks.
- The selected task opens as its own framed card with a Close button.
- In-app updater: an "Update available" button checks GitHub Releases (once at start, cached 1 h; `[updates] check = false`
  in settings.toml disables it), downloads and verifies the installer and runs it silently. Installed build only.
- Per-task tool-call stats (`tool_stats` in state.json: total, per tool, MCP calls per server; `runs_stats` per run with turns, cost, duration) and a "Tools:" line in `reports/<id>.md`, to measure how useful optional MCP servers are.
- Build panel builds versions: pick the next version (proposed from the tags, CHANGELOG and `__version__`) or a release
  tag, tick the finished tasks to include (merged / unmerged / already-in badges, finished-since-last-tag ticked by
  default) and **Build** makes `build/<version>` from the base plus those task branches, then builds it. A conflict
  stops with a clear message; the base branch and task branches are never changed, nothing is pushed. The choice is
  remembered per project. API: `GET .../build` returns `versions` and `tasks`, `POST .../build` takes `version` and `tasks`.

### Changed
- Project page: the Git and Build sections share one row (half the width each, stacked under 900 px); the Build log
  is folded, and the plain ref build, Clean + rebuild and Custom build moved into a small **More** menu.
- Less explanatory text in the dashboard: no chat placeholder, no long tooltips, no empty-state hints.


## 1.2.1 - 2026-10-22

### Fixed
- After an update the dashboard could show no projects at all: the installed app had been started from an agent's
  shell (an install or restart done by the Coworker) that carried the workers' throw-away `AGENTDESK_HOME`
  (`%TEMP%\agentdesk-worker-home`), so it read an empty data folder. The installed program now never uses that
  folder as its data, and the full-access Coworker is no longer given it. Your data was never touched.

## 1.2.0 - 2026-10-22

### Added
- Per-project MCP servers for workers from `[mcp.<name>]` in project.toml; `.code-graph/`
  is never committed.
- Git window: shows the connected repository and connects/changes `origin` (paste a URL or owner/name, or pick from
  `gh repo list`), lists the GitHub CLI's accounts and switches them (`gh auth switch`), remembers a GitHub account
  per project (`[git] account` in project.toml) and runs pull, push, fetch and pull requests as it (token only in
  the command's environment, never shown or stored), lists open pull requests with their checks, opens them and
  creates new ones (title, description, draft). Every change asks first. A missing or logged-out gh is explained.
- Disk hygiene: each project's Disk section measures its cargo target dirs, worktrees and data folder in the
  background, warns over `[hygiene] max_target_gb` / `max_total_gb`, points out bloating `[gates.env]` settings, and
  cleans up on request (dry run first): stale build artifacts, oversized profile folders, orphan per-task target dirs,
  finished and merged worktrees and branches, old run timelines and big logs. Never while a run or a cargo process
  uses the folder; `auto = true` cleans by itself. Every action and the space freed is logged.
- Chat: message text is selectable and copyable (copy button on messages and tool output; polling never resets a selection).
### Changed
- Task list: Active, Blocked and Done are collapsible groups (Done is collapsed by default).
- Faster orchestration: the router's model call is skipped for tasks with a fixed `tier`, the task list is cached
  until files change, and the Claude SDK is imported lazily (quicker start).
- Push in the Git window can push the base branch as well as the current one, and says when it sets the upstream.

## 1.1.2 - 2026-10-20

### Changed
- Task detail: clicking a task shows, in order, the prompt, the orchestrator's handoff (tier, model, router's reasoning,
  runs), what the agent did (recorded activity) and the agent's report.
### Fixed
- Chat: the "working..." bar no longer stays visible when idle (CSS `display:flex` overrode `hidden`).
- Chat: no more jumping - the view follows new messages only when you are at the bottom; switching projects while the
  chat loads no longer mixes projects; the task detail panel is no longer rebuilt on every poll.
- Chat: after a restart, message numbering continues from the saved transcript (old entries were re-added).

## 1.1.1 - 2026-10-17

### Fixed
- The usage reader no longer hammers a rate-limited service: it asks every 3 minutes (was 1), and after a "busy"
  (HTTP 429) answer it waits 2, 4, 8, 15, then 30 minutes (or the service's own Retry-After) and says when it will
  try again. The dashboard showed "usage service is busy" because it kept retrying every minute.
- A chat turn that gets no reply no longer sits at "working..." with no explanation: the bar shows how long Claude
  has been silent, and after 2 minutes the chat says so once, with the last thing the Claude CLI wrote about its
  connection (it retries a failing connection quietly). A tool that is simply running (a long build) isn't counted
  as silence.
- The first start after upgrading from a build that didn't record versions is now logged ("Upgraded from an earlier build to ...").

## 1.1.0 - 2026-10-17

### Added
- Version numbers: one version in `agentdesk/__init__.py`, shown in the dashboard sidebar, `AgentDesk.exe version`,
  the exe's file properties, the installer (`AgentDesk-Setup-x.y.z.exe`) and Apps & features; the commit and build
  time are stamped into each build. `release.ps1` bumps, tags and builds. The app notes in its log when it was upgraded.
- Live Coworker: one persistent session that streams into the chat, runs background jobs and subagents
  (`explorer`, `reviewer`, `implementer`), is woken by finished jobs (`watch_run`), has `checkup` and `schedule_checkup`
  tools, optional self-checkups, a per-burst budget and a daily cap.
- Coworker with full access (project checkout, `main` included, every command); the old guarded mode stays behind
  `[chat.coworker] full_access = false`.
- Planner mode, assistant chat memory and saved history, animated working indicator, done tasks in a foldable group.
- Real account usage (5-hour and weekly) read automatically with the account's own login; manual entry as fallback.
- Accounts show whether the Claude CLI is really signed in, and as whom.
- Windows program and installer (PyInstaller + Inno Setup), system-tray hiding, native window, background running,
  git and build panels, project export/import, auto-runner for every project, plain-folder projects with local git.

### Changed
- The 85% usage pause is gone: usage never blocks runs or the Coworker. The Coworker budget is $5 per burst.
- Settings fall back to the built-in defaults for any key the file lacks.

### Fixed
- The dashboard stalled for seconds every 20 s: merge checks launched two `git` processes per task branch; they are
  now two calls in total, and the overview is served from the last answer while it refreshes.
- Chat polling re-downloaded the whole transcript (100 KB) every few seconds; it now fetches only new entries, and
  streamed entries no longer rewrite the whole transcript file each time.
- The packaged app opened a console window for every `git` command it ran.
- Workers died on network blips (`ECONNRESET`, "connection dropped") and each counted as a task crash; they now wait,
  reconnect to the same session and go on.
- `stop` could trust a stale pid file and take seconds to run; it only ends AgentDesk processes and answers at once.
- Window position was not remembered when the window closed; closed pages no longer fill the log with tracebacks.
