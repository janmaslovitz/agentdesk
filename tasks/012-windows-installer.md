+++
id = "012"
title = "Windows installer for the AgentDesk program (default: Program Files)"
tier = "standard"
depends = ["008", "011"]
+++

## Goal
A separate installable Windows app: `AgentDesk-Setup.exe` built from the PyInstaller program of task 008.
Running the setup installs AgentDesk into Program Files by default, adds Start menu and desktop shortcuts,
and an uninstaller in Apps & features. The user can pick another folder in the setup.

## Where
- New: `installer/AgentDesk.iss` (Inno Setup script), `build_installer.ps1` (runs `build_exe.ps1` from task 008,
  then Inno Setup's `ISCC.exe`; output `dist\installer\AgentDesk-Setup.exe`).
- `README.md`: "Build the installer" and "Install" sections.

## Install rules
- Default install folder: `{autopf}\AgentDesk` (Program Files). Setup asks for admin rights (`PrivilegesRequired=admin`
  with the folder choice still shown). Program Files is not writable by normal users, so the app must not write
  there.
- Data: when installed, data goes to `%LOCALAPPDATA%\AgentDesk` (set `AGENTDESK_HOME` to that for the program,
  or make the frozen-program data rule use it). Keep the rule from task 008 for the portable build: `data` next to
  the `.exe` if it exists there, otherwise `%LOCALAPPDATA%\AgentDesk`. A user's existing `data\` can be copied in
  by hand; the installer must never delete or overwrite `data\`.
- Uninstall removes the program files only; `%LOCALAPPDATA%\AgentDesk` (logins, projects, settings) stays unless the
  user ticks an "also remove my data" box.
- Shortcuts: Start menu "AgentDesk" and an optional desktop icon (unchecked by default). The shortcut starts the
  program with the window (task 005) and the tray behaviour (task 011).
- Single instance and background behaviour from task 001 still apply.

## Constraints
- All work happens in an isolated container with a cloned test app, never on the live app or its `data/`.
- Don't bundle `data/`, logins, API keys, or project files into the installer.
- The installer does not install Python or Claude Code. If the Claude Code CLI or WebView2 is missing, the installer
  shows a message with the link and continues (WebView2 is preinstalled on Windows 11).
- Don't change the source version's behaviour; `AgentDesk.cmd` keeps working from the repo.
- Build from a clean checkout; check the installed files list has no `data`, `.venv`, `worktrees` or `__pycache__`.

## Done when
- `build_installer.ps1` produces `dist\installer\AgentDesk-Setup.exe` from a clean checkout.
- Installing with defaults puts the program in `C:\Program Files\AgentDesk`, the window opens from the Start menu,
  and data goes to `%LOCALAPPDATA%\AgentDesk`.
- Changing the folder in setup works; installing to a non-admin folder works without elevation.
- Uninstall removes the program; data stays unless the box was ticked.
- README describes install, uninstall and where the data goes.
- Report what was actually tested (install, launch, uninstall) and the installer size.
