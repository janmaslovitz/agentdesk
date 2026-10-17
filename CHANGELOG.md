# Changelog

What changed in each AgentDesk version, newest first. Write new entries under **Unreleased**; `release.ps1` moves
them under the new version number, tags it and builds the installer (see README, "Versions and releases").

## Unreleased

### Fixed
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
