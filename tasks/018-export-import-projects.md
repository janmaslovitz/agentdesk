+++
id = "018"
title = "Export and import projects (to git, or to an offline file)"
tier = "standard"
depends = ["016"]
+++

## Goal
Move a project (its AgentDesk settings and data) between computers or back it up, two ways:

1. **Offline file**: Export writes one `.zip` (chosen by the user in the UI) containing the project's config, tasks, notes, state, reports and chat history. Import reads such a `.zip` and creates the project.
2. **Git**: Export commits the same content into a folder `.agentdesk/` (or a separate "config repo" the user names) and pushes it with the git panel from 016; Import clones/pulls that repo or folder and creates the project from it.

Both are available in the dashboard (Export / Import buttons per project, Import on the project list) and as CLI commands (`export <project> <file>`, `import-file <file>`; the existing `import` command stays as is).

## What is included / excluded
- Included: project config, `tasks/`, `notes.md`, `state.json`, `reports/`, chat history, per-project settings.
- Excluded by default, never exported: account logins, API keys, tokens, the account/credential files, `agentdesk.log`, worktrees. An "include logs" checkbox may add the log.
- The repo path is machine-specific: on Import the user picks the local repo path (or "clone from URL"); the account is picked from the local accounts (or left unset).

## Where
- `agentdesk/core.py`: `export_project(...)`, `import_project(...)` (zip and git folder variants), with a manifest file (`manifest.json`: format version, project name, exported-at, app version).
- `agentdesk/server.py`: `POST /api/projects/<p>/export`, `POST /api/projects/import`.
- `agentdesk/web/index.html`: buttons and dialogs.
- `agentdesk/__main__.py`: CLI commands.
- Tests in `tests/`.

## Constraints
- Never write secrets into an export; add a test that scans an export for the account/key values and fails if found.
- Import never overwrites an existing project: on a name clash it asks for a new name (or refuses in the CLI).
- Reject zips with paths outside the project folder (zip-slip), unknown manifest versions, and oversize files.
- Git export never force-pushes and never changes remotes; the push goes through the 016 confirmation.
- All testing with a temp `AGENTDESK_HOME` and a local bare repo, never the live checkout or its `data/`.

## Done when
- Export then Import of a project (zip and via a local bare repo) recreates its tasks, notes, reports and state in a fresh `AGENTDESK_HOME`.
- No secrets in the export (tested); zip-slip and name-clash tests pass.
- `python -m pytest tests` passes.
