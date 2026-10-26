+++
id = "002"
title = "Projects in plain folders: local git setup when adding a project"
tier = "standard"
+++

Source: section 3 of `zz_agentdesk_next.md` (read it first).

## Goal
A user can add a project from a folder that isn't a git repo, without knowing git. AgentDesk sets up
local version tracking and then uses it like any repo.

When adding a project (dashboard "+ Add" and `add-project` command):
- If the folder isn't a git repo, offer "Set up local version tracking (stays on this computer)".
- If accepted:
  1. `git init`.
  2. Write a `.gitignore` if none exists: node_modules, target, dist, build, .venv, __pycache__, *.log, .DS_Store.
  3. Commit everything as "AgentDesk: starting point". If git has no identity, use the local identity
     `AgentDesk <agentdesk@localhost>` (set it for this repo only).
  4. Use the current branch as the base.
- Never add remotes.
- If the folder has no commits and is a repo, offer the same first commit (don't change anything else).
- README explains it's local only and nothing is pushed.

## Where
- `agentdesk/core.py` (`add_project`, plus a small git helper; use the existing `git()` helper).
- `agentdesk/__main__.py` (`add-project` flag, e.g. `--local-git`, and the prompt/default behaviour).
- `agentdesk/server.py` (`POST /api/projects` response: say when setup is needed; the dashboard then asks).
- `agentdesk/web/index.html` (the offer).
- `README.md` (Projects section).

## Constraints
- Never run `git init` in a folder without asking first.
- Never overwrite an existing `.gitignore`.
- Don't add remotes, push, or change the user's existing git config.
- Test with a scratch folder under a temporary `AGENTDESK_HOME`; don't touch `data/` or any real project.

## Done when
- A plain folder becomes a repo with one commit "AgentDesk: starting point" and the right `.gitignore`.
- An existing repo is left as it is.
- A task run on the new project creates its worktree from that base branch.
- Unit tests cover the plain-folder path and the existing-repo path.
