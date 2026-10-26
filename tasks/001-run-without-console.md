+++
id = "001"
title = "Run the dashboard in the background (no console window)"
tier = "standard"
+++

Source: section 2 of `zz_agentdesk_next.md` (read it first).

## Goal
The dashboard and its runs must not depend on a console window staying open.

- `AgentDesk.cmd`: if the server answers on 127.0.0.1:8765, just open the browser. Otherwise start the server
  detached with no window (`start "" pythonw -m agentdesk dashboard`, or Popen with
  `DETACHED_PROCESS | CREATE_NO_WINDOW`), then open the browser. `agentdesk.sh`: start with `nohup … &`
  (or `setsid`) and open the browser.
- Under `pythonw`, `sys.stdout`/`sys.stderr` are None: guard `reconfigure` calls and log server output to
  `data/server.log`.
- Runs (`Runner.start` in `agentdesk/server.py`) are detached from the server's console on Windows
  (`DETACHED_PROCESS | CREATE_NO_WINDOW`) so closing the window or the server doesn't kill them. Their output
  keeps going to `runner.log` / `agentdesk.log`.
- Dashboard **Quit** button: `POST /api/quit`, then the server shuts down. Runs keep going.
- When the server is unreachable, the page shows a banner "AgentDesk isn't running — start AgentDesk.cmd"
  instead of a NetworkError, and retries every few seconds.
- Single instance: starting while one is already answering on the port opens the browser to the existing one
  instead of failing.
- A documented `stop` command ends the dashboard.

## Where
- `AgentDesk.cmd`, `agentdesk.sh`, `install.ps1` (keep the `.venv` lookup).
- `agentdesk/__main__.py` (add `stop`; keep existing commands).
- `agentdesk/server.py` (`Runner.start`, `main()`, the Quit endpoint, stdout guard).
- `agentdesk/web/index.html` (banner and retry).
- `README.md` ("Start" section).

## Constraints
- Windows first; keep macOS/Linux working.
- Don't change the pipeline in `agentdesk/core.py` or the run arguments.
- Server stays on 127.0.0.1 only.
- Don't change the `data/settings.toml` format.
- Test with a temporary `AGENTDESK_HOME` and a scratch repo; keep the user's `data/` intact.

## Done when
- `AgentDesk.cmd` opens the browser; closing its window leaves the dashboard reachable at http://127.0.0.1:8765.
- A run started from the dashboard keeps going after the dashboard is closed, and `status` still shows it as running.
- Quit ends the server; a second start opens the existing one; the offline banner appears and clears when the server returns.
- README "Start" matches what the code does.
