+++
id = "017"
title = "Build control panel (build / recompile menu)"
tier = "standard"
depends = ["008", "016"]
+++

## Goal
A "Build" menu/panel in the dashboard to build and recompile the program on request.

- Actions: Build (run `build_exe.ps1`), Clean + rebuild (recompile from scratch), and optionally a per-project custom build command from the project's config file (`[build] command = "..."`).
- Show live output (streamed log), status (idle/running/ok/failed), time, and the result path (e.g. `dist\AgentDesk\AgentDesk.exe`) with an "Open folder" button.
- Choose which branch/commit to build (default: base branch); the build runs in a separate worktree/clone so the working copy is not touched.
- One build at a time; a Stop button; refuse to start while a task run is active for the project.

## Where
- `agentdesk/core.py`: build runner (subprocess, log file under the project folder, no console window).
- `agentdesk/server.py`: `POST /api/projects/<p>/build`, `GET .../build` (status + log tail), `POST .../build/stop`.
- `agentdesk/web/index.html`: the panel, next to the Git panel.
- Tests with a fake build command.

## Constraints
- Build commands come only from the project config or the built-in script, never from request text.
- Don't include `data/` or secrets in outputs; use a temp `AGENTDESK_HOME` for tests; never touch the live checkout.
- Use a non-default port when testing the dashboard (e.g. 8799).

## Done when
- Build and Clean+rebuild run from the UI with live log and correct status; Stop works.
- `python -m pytest tests` passes.
