+++
id = "015"
title = "Merge finished task branches into main"
tier = "standard"
depends = ["007", "013"]
+++

## Goal
Bring the finished task branches into `main` so the main checkout has all the work, then stop. The exe build (task 008) runs after this.

## Where
- Main checkout: `C:\Users\kochurin.a\AgentDesk` (branch `main`).
- Branches to merge: `auto/013` (Claude CLI no-console fix, commit 6112299) and `auto/007` (usage reading, once done). Any other `auto/*` branch that is finished and not yet in `main`: check with `git branch --no-merged main` first.

## Steps
1. In the main checkout, check `git status`. If there are uncommitted changes, stop and report them. Do not discard anything.
2. Merge `auto/013`, then `auto/007`, into `main` with normal merge commits. Resolve conflicts only where the change is clearly additive; if a conflict changes behaviour, stop and report it.
3. Run the test suite from the repo root: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests`. It must pass.
4. Do not push. Do not add remotes. Do not touch `data/`.
5. Check that `agentdesk\core.py` has `_hide_sdk_console` and `agentdesk\server.py` has the usage endpoints from task 007.

## Constraints
- Never run `git reset --hard`, `git push --force`, or delete branches.
- Keep `__pycache__` out of the commits (use `PYTHONDONTWRITEBYTECODE=1`).

## Done when
- `git branch --no-merged main` shows no finished task branches.
- The test suite passes on `main`.
- Report: which branches were merged, the merge commit hashes, test results, and any conflicts.
