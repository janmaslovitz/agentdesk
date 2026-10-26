+++
id = "011"
title = "Closing the dashboard window hides it to the system tray"
tier = "standard"
+++

## Goal
Clicking the window's close button (X) hides the AgentDesk window into the system tray instead of ending it.
The server and any runs keep going. A tray icon brings the window back (click or "Open") and has "Quit" (ends the
window and the server, same as the dashboard Quit button).

## What's wrong today
`open_window` in `agentdesk/server.py` (around line 838) creates the pywebview window; closing it ends the window
(`closed` handler saves its geometry), and `main()` keeps the server alive. There is no tray icon and no way to
get the window back except starting `AgentDesk.cmd` again.

## Where
- `agentdesk/server.py`: `open_window` and `main()`. Hide on close (cancel the close event, `window.hide()`),
  show again from the tray; keep the geometry saving working.
- `agentdesk/__main__.py` / dependencies: a tray library (e.g. `pystray` + Pillow) added as an optional
  dependency. If it's missing or the tray can't start, fall back to the current behaviour (close ends the window)
  and log a line to `agentdesk.log`.
- `README.md` ("Start" section): one sentence on the tray behaviour and Quit.

## Constraints
- All work happens in an isolated container with a cloned test app, never on the live app or its `data/`.
- Windows first; keep macOS/Linux working (if the tray can't start there, the fallback applies).
- Server stays on 127.0.0.1 only. Don't change the run pipeline in `agentdesk/core.py`.
- Don't change the `data/settings.toml` format; no new settings keys unless needed.
- Closing the tray icon's window must not stop runs; only the tray "Quit" (or `python -m agentdesk stop`) ends the server.
- Test with a temporary `AGENTDESK_HOME`; keep the user's `data/` intact.

## Done when
- Closing the window leaves a tray icon; clicking it shows the same dashboard again with the same state.
- A run started from the dashboard keeps going after the window is hidden.
- Tray "Quit" ends the window and the server; `status` shows nothing running afterwards unless a run was detached.
- With the tray library missing, the window closes as before and a log line says why.
- README "Start" matches what the code does.
