+++
id = "016"
title = "Git control panel in the dashboard (status, merge, pull, push)"
tier = "standard"
depends = ["015"]
+++

## Goal
A "Git" panel per project in the dashboard so the user can see and run the common git actions without a terminal.

- Show: current branch, base branch, clean/dirty, ahead/behind versus the remote (if a remote exists), list of `auto/*` branches with commits ahead of base.
- Actions (buttons): Merge a chosen branch into base (reuse the logic from task 015), Pull (fast-forward only by default), Push (current/base branch), Refresh/fetch.
- Every action that changes anything (merge, pull, push) asks for a confirmation in the UI first, showing what will happen (branch names, commit count, remote URL).
- Results and errors are shown in the panel (git's own message, plain words); a merge conflict aborts the merge (`merge --abort`) and says which files conflicted.
- If the project has no remote, Pull/Push are disabled with a hint ("local-only project").

## Where
- `agentdesk/core.py`: small git helper functions (status, branches, merge, pull, push), no UI code.
- `agentdesk/server.py`: `GET /api/projects/<p>/git`, `POST /api/projects/<p>/git/<action>`.
- `agentdesk/web/index.html`: the panel.
- Tests in `tests/` using a scratch repo plus a local bare repo as the "remote".

## Constraints
- Never force-push, never rewrite history, never change remotes or global git config.
- Work only on the project's configured repo path; refuse when a run is in progress for that project.
- The workers' own guard still blocks push for automated runs; this panel is a user action only (needs the UI confirmation, no agent tool for it).
- All testing in an isolated clone with a temp `AGENTDESK_HOME` and a local bare remote; never the live checkout.

## Done when
- The panel shows correct status and the four actions work against a local bare remote in tests.
- Conflicts abort cleanly and leave the repo as before.
- `python -m pytest tests` passes.
