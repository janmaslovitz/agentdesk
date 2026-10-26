+++
id = "008"
title = "Package AgentDesk as a Windows program (.exe)"
tier = "standard"
depends = ["005", "015"]
+++

## Goal
Build a standalone Windows program so AgentDesk starts without a Python install or the `.venv` setup.
Double-clicking the program opens the native desktop window from task 005.

- Use PyInstaller (add it to a separate `requirements-build.txt`, not to the runtime requirements).
- A build script `build_exe.ps1` at the repo root: creates the build venv, builds, and puts the result in
  `dist\AgentDesk\AgentDesk.exe`.
- The program reads and writes data in `AGENTDESK_HOME` if set; otherwise in a `data` folder next to the
  `.exe` if that folder already exists (so existing `data/` folders keep working); otherwise in
  `%LOCALAPPDATA%\AgentDesk` (created on first start). Program Files is not writable, so never create `data`
  next to an `.exe` that was not given one. Source runs keep `APP/data` as now.
- The web page (`agentdesk/web/index.html`) and any other assets are bundled into the build.
- Single instance and background behaviour from task 001 still apply to the `.exe`.
- The `.cmd` and `.sh` launchers keep working with the source version.
- App icon: there is no `.ico` in the repo yet. Add `agentdesk/web/agentdesk.ico` (simple placeholder is fine,
  multi-size 16/32/48/256) and embed it in the `.exe` via the PyInstaller `--icon` option. Also use it for the window.
- Desktop icon: `build_exe.ps1` creates a desktop shortcut "AgentDesk" pointing to `dist\AgentDesk\AgentDesk.exe`
  with the same icon (via WScript.Shell COM). Create it only when missing; re-running the build updates it. Add a
  `-NoDesktopShortcut` switch to skip it.

## Where
- New: `build_exe.ps1`, `requirements-build.txt`, a PyInstaller spec file if needed.
- `agentdesk/core.py` only if the data-folder path needs a fix for a frozen program (keep the rule, don't change data).
- `README.md` (a "Build the program" section).

## Constraints
- All work happens in an isolated container with a cloned test app, never on the live app or its `data/`.
- Don't include `data/` (projects, logins, API keys) in the build output.
- Don't bundle any secrets or the user's account files.
- The build must not change the source version's behaviour.
- Test in a clean folder (copy of the repo without `.venv`), not in the working copy.

## Done when
- `build_exe.ps1` produces `dist\AgentDesk\AgentDesk.exe` from a clean checkout.
- The `.exe` opens the dashboard window and reads the existing `data\` folder next to it.
- With no `data\` next to the `.exe` and no `AGENTDESK_HOME`, it uses `%LOCALAPPDATA%\AgentDesk` (test with a
  temporary `LOCALAPPDATA` value, not the real one).
- The dashboard runs a task from the `.exe` the same as from source (checked with a scratch repo and Haiku).
- The output folder contains no `data\logins`, no `settings` secrets, and no project files.
- The exe shows the icon in Explorer, and the desktop shortcut starts the window with that icon.
- Report what was actually built (exe size), what was tested, and whether the desktop shortcut was checked.
- README describes how to build and where the data goes.
