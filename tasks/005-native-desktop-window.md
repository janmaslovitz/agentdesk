+++
id = "005"
title = "Open the dashboard in a native desktop window (no browser)"
tier = "standard"
depends = ["001"]
+++

## Goal
AgentDesk opens in its own desktop window, not in a browser tab. The existing dashboard page (`agentdesk/web/index.html`)
stays the same; only the way it's shown changes.

- Use `pywebview` (add to `requirements.txt`) to show the page served by the local server at http://127.0.0.1:8765.
- `AgentDesk.cmd` (and `agentdesk.sh`) start the native window. The browser is no longer opened by default.
- Keep a fallback: `python -m agentdesk dashboard --browser` opens the old browser behaviour.
- Closing the window doesn't stop runs (same as task 001). Closing the window ends the window only; the server
  keeps running in the background, or it's stopped via the Quit button / `stop` command from task 001.
- Window title "AgentDesk", reasonable default size, remembers size and position in `data/settings.toml` under `[window]`
  (don't rewrite the user's existing values).
- If the native window can't start (missing web engine), show a clear error and fall back to the browser.

## Where
- `requirements.txt` (pywebview).
- `agentdesk/__main__.py` (`dashboard` command: window vs `--browser`).
- `agentdesk/server.py` (`main()`: start the window; keep the server thread alive).
- `AgentDesk.cmd`, `agentdesk.sh`, `install.ps1` (install the new requirement, keep `.venv` lookup).
- `README.md` ("Start" section).

## Constraints
- Windows first (WebView2 is normally present); macOS/Linux work with the platform engine or fall back to browser.
- Don't change `core.py` pipeline behaviour.
- Server stays on 127.0.0.1 only.
- Test with a temporary `AGENTDESK_HOME`; keep the user's `data/` intact.

## Done when
- `AgentDesk.cmd` opens the dashboard in its own window with title "AgentDesk", without a browser tab.
- Closing the window leaves runs going (verified with a run started from the dashboard).
- `python -m agentdesk dashboard --browser` still opens the browser.
- If the window can't start, the browser fallback opens and an error is logged.
- README describes the window and the fallback.
