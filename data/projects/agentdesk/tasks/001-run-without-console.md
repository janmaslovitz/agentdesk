+++
id = "001"
title = "Dashboard and runs don't need a console window kept open"
tier = "standard"
+++

## Goal
Today the dashboard and its runs only live while the console window opened by `AgentDesk.cmd` stays open.
Closing that window kills the dashboard server and any run started from it. After this change:

1. The dashboard can be started without a visible console window (Windows: a hidden start, e.g. via
   `pythonw -m agentdesk` or a `.vbs`/`.cmd` launcher that uses `start` with a hidden window), and it keeps
   running after the window that started it is closed.
2. Runs started from the dashboard keep going even if the dashboard server is closed or restarted, and they
   don't open or depend on any console window.
3. There is an obvious way to stop the dashboard (a `stop` command or documented PID file), and the
   existing `AgentDesk.cmd` double-click still opens the dashboard in the browser as before.

## What's wrong today
- `AgentDesk.cmd` runs `python -m agentdesk` in the console; closing it ends the server.
- `agentdesk/server.py` `Runner.start` launches runs with `subprocess.Popen(..., creationflags=CREATE_NEW_PROCESS_GROUP)`
  on Windows. Those runs still share the console of the launcher, so closing that console kills them too.
- The dashboard keeps no record of a running server (no PID or lock file), so a second start can't tell
  the server is already up.

## Where
- `AgentDesk.cmd`, `agentdesk.sh`, `install.ps1` (launchers; keep the `.venv` lookup).
- `agentdesk/__main__.py` (add e.g. `stop` and a single-instance check; keep existing commands).
- `agentdesk/server.py` (`Runner.start`: detach the run process on Windows with `DETACHED_PROCESS | CREATE_NO_WINDOW`
  or equivalent; `main()`: single-instance check: if the port already answers, open the browser and exit).
- `README.md` (update the "Start" section).

## Constraints
- Windows is the main platform; keep macOS/Linux working (`agentdesk.sh`, `nohup`/`setsid` equivalents).
- Don't change the task pipeline in `agentdesk/core.py` or the run arguments.
- Keep the server bound to 127.0.0.1 only.
- Don't store secrets or change `data/settings.toml` format.
- Stdout/stderr of runs keep going to `runner.log` / `agentdesk.log`.

## Done when
- Starting via `AgentDesk.cmd` opens the browser; closing that console window leaves the dashboard reachable at
  http://127.0.0.1:8765 (or a documented hidden start is used instead of `AgentDesk.cmd`).
- A run started from the dashboard keeps going after the window that started the dashboard is closed; `status`
  and the dashboard still show it as running and its log lines keep being written.
- Starting a second dashboard while one is running doesn't fail with a port error; it opens the existing one.
- A documented stop command ends the dashboard and, optionally, asks for the run to stop.
- `python -m agentdesk status agentdesk` still works, and README "Start" section matches what the code does.
