+++
id = "013"
title = "Chat opens an empty console window (claude CLI) on Windows"
tier = "standard"
+++

## Goal
Sending a message to the Assistant (or Coworker) must not open an empty console window. The chat talks to the
Claude CLI in the background, as the workers do.

## What's wrong today
Reported: sending a message to the assistant opens an empty Claude CLI window.

Likely cause (verify first): the chat starts the bundled `claude.exe` through `ClaudeSDKClient` (`Chat._client`,
`agentdesk/server.py` ~line 400). The SDK opens that process with `anyio.open_process` and no `creationflags`
(`claude_agent_sdk/_internal/transport/subprocess_cli.py` ~line 873). When the dashboard runs without a console
(task 001: `pythonw`/detached start), Windows gives every console child a new visible console window.
Runs are fine because `Runner.start` (`server.py` ~line 64) starts them with `CREATE_NO_WINDOW`; the chat path has
no such flag.

## Where
- `agentdesk/server.py`: `Chat._client` / `_assistant_options` / `_coworker_options` and the places that call
  `ClaudeSDKClient` (also check `agentdesk/core.py` ~line 772, the one-shot `ClaudeSDKClient` use).
- Possibly a small helper in `agentdesk/core.py` that makes the SDK's CLI process start with
  `CREATE_NO_WINDOW` on Windows (for example a subclass of the transport or a wrapper around `anyio.open_process`
  limited to the SDK). Check how the SDK version in `requirements.txt` exposes this before choosing.

## Constraints
- All work happens in an isolated container with a cloned test app, never on the live app or its `data/`.
- Windows first; no change on macOS/Linux.
- Don't change the permission checks, approval cards, or the guard (`Chat.permission`, `coworker_permission`).
- Don't change the run pipeline or run arguments (`Runner.start`).
- Don't add `Bash`/`PowerShell` to `allowed_tools`.
- Tests: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider` in `tests/`; never touch the real `data/`.

## Done when
- Starting AgentDesk with no console (the `AgentDesk.cmd` / `pythonw` path) and sending a message in Assistant mode
  opens no new window; the answer still comes back.
- The same in Coworker mode, with a message that runs a shell command.
- Verified by checking the process list / window list during the message (report the method used).
- Tests pass; report what was tested and whether the cause above was confirmed or something else was found.
