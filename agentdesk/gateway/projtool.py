"""Project operations inside one user's data folder, run as a child process by the admin cabinet:

    AGENTDESK_HOME=<user home> python -m agentdesk.gateway.projtool list|remove|git|zip|clean ...

core binds its data folder when it is imported, and the gateway itself never imports core, so the work is done in a
process of its own with the user's AGENTDESK_HOME. The answer is one JSON object on stdout: {"ok": true, ...} or
{"ok": false, "error": "..."}.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path


def dir_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def listing(core) -> list[dict]:
    out = []
    for p in core.projects():
        repo = str(p.cfg.get("repo", {}).get("path", ""))
        rp = Path(repo) if repo else None
        out.append({"slug": p.slug, "name": p.name, "repo": repo,
                    "size": dir_size(p.dir), "repo_size": dir_size(rp) if rp and rp.is_dir() and not (rp == p.dir) else 0})
    return out


def create_from_git(core, url: str, name: str, repos: Path) -> dict:
    s = core.slug(name or re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]))
    if (core.DATA / "projects" / s).exists():
        raise ValueError(f"a project named {s} exists")
    dest = repos / s
    core.clone_for_import(url, into=dest)
    try:
        try:
            p = core.import_project(dest, str(dest), new_name=name or None)  # a clone that holds an AgentDesk export
        except core.BadExport as e:
            if "no AgentDesk export" not in str(e):
                raise
            code, branch = core.git(["rev-parse", "--abbrev-ref", "HEAD"], dest)
            p = core.add_project(name or s, str(dest), branch.strip() if code == 0 and branch.strip() != "HEAD" else "main",
                                 local_git=True)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"slug": p.slug, "name": p.name}


def create_from_zip(core, zip_path: str, name: str, repos: Path) -> dict:
    if not Path(zip_path).is_file():
        raise ValueError("no upload")
    try:
        manifest, _files = core._read_zip(Path(zip_path))
    except core.BadExport:
        raise
    pname = (name or str(manifest.get("project") or "")).strip()
    if not pname:
        raise core.BadExport("the export has no project name")
    s = core.slug(pname)
    if (core.DATA / "projects" / s).exists():
        raise ValueError(f"a project named {s} exists")
    repo = repos / s
    repo.mkdir(parents=True, exist_ok=False)
    try:
        p = core.import_project(zip_path, str(repo), new_name=pname)
    except BaseException:
        shutil.rmtree(repo, ignore_errors=True)
        raise
    return {"slug": p.slug, "name": p.name}


def remove(core, slug_: str, delete_repo: bool) -> dict:
    p = core.project(slug_)
    if re.search(r"[\/]", slug_) or p.dir.parent != core.DATA / "projects":
        raise ValueError("bad project")
    repo = p.repo
    shutil.rmtree(p.dir)
    if delete_repo and repo.is_dir() and (core.DATA / "repos") in repo.parents:
        shutil.rmtree(repo, ignore_errors=True)
    return {}


def clean(core, dry_run: bool) -> dict:
    """Disk hygiene for every project of this user (the storage cleanup of the server): the plan (dry run) or the
    cleanup itself, with all of hygiene's checks (never outside the project's folders, never while a run uses them;
    a project with a run going is skipped). Old import / upload left-overs go too. Deletions are audited by hygiene
    (storage-audit.jsonl, moved into the server's audit.log by the gateway)."""
    from .. import hygiene
    out = []
    for p in core.projects():
        try:
            cfg = core.hygiene_cfg(p)
            if not cfg["enabled"]:
                continue
            if dry_run:
                rep = hygiene.measure(p, cfg)
                out.append({"slug": p.slug, "total": rep["total"], "reclaimable": rep["reclaimable"],
                            "actions": [{k: a.get(k) for k in ("kind", "path", "bytes", "why")} for a in rep["actions"]]})
            else:
                res = hygiene.apply(p, None, cfg)
                out.append({"slug": p.slug, "freed": res["freed"], "done": len(res["done"]),
                            "skipped": [{"kind": s.get("kind"), "path": s.get("path"), "reason": s.get("reason")} for s in res["skipped"]][:50]})
        except Exception as e:  # one broken project must not stop the others
            out.append({"slug": p.slug, "error": f"{type(e).__name__}: {e}"[:300]})
    if not dry_run:
        hygiene.sweep_temp(core.DATA / "imports", 24.0)
    return {"projects": out}


def main(argv: list[str]) -> int:
    from .. import core
    try:
        op = argv[0]
        repos = core.DATA / "repos"
        if op == "list":
            res = {"projects": listing(core)}
        elif op == "remove":
            res = remove(core, argv[1], argv[2] == "1")
        elif op == "git":
            res = create_from_git(core, argv[1], argv[2], repos)
        elif op == "zip":
            res = create_from_zip(core, argv[1], argv[2], repos)
        elif op == "clean":
            res = clean(core, dry_run=(argv[1:2] or ["--dry-run"])[0] != "--apply")
        else:
            raise ValueError("unknown operation")
        print(json.dumps({"ok": True, **res}))
    except KeyError as e:
        print(json.dumps({"ok": False, "error": str(e.args[0]) if e.args else "not found"}))
    except Exception as e:  # the answer is the error text, not a traceback
        print(json.dumps({"ok": False, "error": str(e)[:600]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
