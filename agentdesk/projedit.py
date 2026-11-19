"""Deleting and editing a project from the dashboard: the logic only (the server and the gateway call it).

Nothing here changes core; core's helpers are used read-only. Edits keep comments and unknown keys of project.toml."""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from pathlib import Path

from . import core, fsutil, hygiene

SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+")
MAX_TOML = 64 * 1024
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class ProjectBusy(ValueError):
    """A run, build or chat is in progress in the project."""


def _log(msg: str) -> None:
    try:
        with (core.DATA / "agentdesk.log").open("a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------- delete

def delete_project(p: core.Project, delete_repo: bool, busy: str | None = None) -> dict:
    if busy:
        raise ProjectBusy(busy)
    if core.build_job(p).running():
        raise ProjectBusy("a build is running in this project")
    projects = core.DATA / "projects"
    if not SLUG_RE.fullmatch(p.slug or "") or p.slug in (".", "..") or p.dir.parent != projects:
        raise ValueError("bad project")
    try:
        repo: Path | None = p.repo
    except (KeyError, TypeError):
        repo = None
    freed, errors, why = fsutil.safe_delete(projects, p.dir)
    if why:
        raise ValueError(f"the project folder is not deleted: {why}")
    hygiene.audit_deletion(p, {"kind": "project", "path": str(p.dir), "why": "deleted from the dashboard"}, freed)
    _log(f"project {p.slug} deleted from the dashboard ({fsutil.fmt(freed)} freed" + (f", {errors} errors" if errors else "") + ")")
    repo_deleted, note = False, ""
    if repo is not None and repo.exists() or (repo is not None and repo.is_symlink()):
        refusal = fsutil.refuse_delete(core.DATA / "repos", repo)
        if delete_repo and refusal is None:
            rf, re_, rwhy = fsutil.safe_delete(core.DATA / "repos", repo)
            if rwhy:
                note = f"the repository is kept: {rwhy}"
            else:
                repo_deleted, freed = True, freed + rf
                hygiene.audit_deletion(p, {"kind": "repo", "path": str(repo), "why": "deleted from the dashboard"}, rf)
                _log(f"repository {repo} of project {p.slug} deleted from the dashboard ({fsutil.fmt(rf)} freed)")
        elif delete_repo:
            note = f"the repository is kept: {refusal}"
        else:
            note = "the repository is kept"
        if not repo_deleted and repo.is_dir() and not repo.is_symlink() and (repo / ".git").exists():
            core.git(["worktree", "prune"], repo)
    elif repo is not None:
        note = "the repository folder does not exist"
    return {"ok": True, "slug": p.slug, "freed": int(freed), "repo_deleted": repo_deleted, "repo_note": note}


# ----------------------------------------------------------------------------- view

def _gate_list(p: core.Project, key: str) -> list[str]:
    v = (p.cfg.get("gates") or {}).get(key, [])
    return [str(x) for x in v] if isinstance(v, list) else []


def settings_view(p: core.Project) -> dict:
    g = p.cfg.get("gates") or {}
    try:
        timeout = int(g.get("timeout_s", 3600))
    except (TypeError, ValueError):
        timeout = 3600
    return {"slug": p.slug, "name": p.name, "repo": str((p.cfg.get("repo") or {}).get("path", "")),
            "base": str((p.cfg.get("repo") or {}).get("base", "")),
            "account": str(p.cfg.get("account", "")),
            "accounts": [str(a.get("name")) for a in core.settings().get("accounts", [])],
            "quick": _gate_list(p, "quick"), "final": _gate_list(p, "final"), "timeout_s": timeout,
            "toml": core._project_text(p)}


# ----------------------------------------------------------------------------- surgical edits

def _value_end(lines: list[str], at: int, key: str) -> int:
    """Index after the last line of the (maybe multi-line) value of `key` that starts on lines[at]."""
    for end in range(at + 1, len(lines) + 1):
        try:
            tomllib.loads("\n".join(lines[at:end]))
            return end
        except tomllib.TOMLDecodeError:
            continue
    return at + 1


def _set_key(text: str, table: str | None, key: str, value: str) -> str:
    lines = text.split("\n")
    heads = [n for n, ln in enumerate(lines) if re.match(r"\s*\[", ln)]
    if table is None:
        start, stop = 0, heads[0] if heads else len(lines)
    else:
        h = next((n for n in heads if lines[n].strip() == f"[{table}]"), None)
        if h is None:
            return text.rstrip("\n") + f"\n\n[{table}]\n{key} = {value}\n"
        start, stop = h + 1, next((n for n in heads if n > h), len(lines))
    at = next((n for n in range(start, stop) if re.match(rf"\s*{re.escape(key)}\s*=", lines[n])), None)
    if at is None:
        if table is None:
            lines.insert(0, f"{key} = {value}")
        else:
            lines.insert(start, f"{key} = {value}")
    else:
        lines[at:_value_end(lines, at, key)] = [f"{key} = {value}"]
    return "\n".join(lines)


def _commands(v, field: str) -> list[str]:
    if isinstance(v, str):
        v = [ln for ln in v.split("\n") if ln.strip()]  # blank lines in a text are just spacing
    if not isinstance(v, (list, tuple)) or not all(isinstance(c, str) for c in v):
        raise ValueError(f"{field}: a list of commands")
    out = [c.strip(" \t\r") for c in v]
    if len(out) > 20:
        raise ValueError(f"{field}: at most 20 commands")
    for c in out:
        if not c:
            raise ValueError(f"{field}: a command is empty")
        if len(c) > 1000:
            raise ValueError(f"{field}: a command is longer than 1000 characters")
        if "\x00" in c or "\n" in c or "\r" in c:
            raise ValueError(f"{field}: a command has a NUL or a line break")
    return out


def _base_ok(p: core.Project, base: str) -> None:
    if not base or base.startswith("-") or core.git(["check-ref-format", "--branch", base], core.DATA)[0] != 0:
        raise ValueError("not a valid branch name")
    for ref in (f"refs/heads/{base}", f"refs/remotes/{base}"):
        if core.git(["show-ref", "--verify", "--quiet", ref], p.repo)[0] == 0:
            return
    raise ValueError(f"no branch {base!r} in the repository")


def update_settings(p: core.Project, d: dict) -> dict:
    text = core._project_text(p)
    field = ""
    try:
        if "name" in d:
            field = "name"
            n = d["name"]
            if not isinstance(n, str) or not 1 <= len(n.strip()) <= 80 or _CTRL.search(n):
                raise ValueError("a name is 1-80 characters without control characters")
            text = _set_key(text, None, "name", core.toml_str(n.strip()))
        if "base" in d:
            field = "base"
            if not isinstance(d["base"], str):
                raise ValueError("not a valid branch name")
            _base_ok(p, d["base"])
            text = _set_key(text, "repo", "base", core.toml_str(d["base"]))
        if "account" in d:
            field = "account"
            names = [a.get("name") for a in core.settings().get("accounts", [])]
            if d["account"] not in names:
                raise ValueError(f"no account {d['account']!r}")
            text = _set_key(text, None, "account", core.toml_str(d["account"]))
        for k in ("quick", "final"):
            if k in d:
                field = k
                cmds = _commands(d[k], k)
                text = _set_key(text, "gates", k, "[" + ", ".join(core.toml_str(c) for c in cmds) + "]")
        if "timeout_s" in d:
            field = "timeout_s"
            t = d["timeout_s"]
            if isinstance(t, str) and t.strip().isdigit():
                t = int(t.strip())
            if isinstance(t, bool) or not isinstance(t, int) or not 60 <= t <= 86400:
                raise ValueError("the timeout is 60 to 86400 seconds")
            text = _set_key(text, "gates", "timeout_s", str(t))
        field = ""
        new = tomllib.loads(text)
        want = {"name": d.get("name", "").strip() if isinstance(d.get("name"), str) else None}
        if want["name"] is not None and new.get("name") != want["name"]:
            raise ValueError("couldn't update project.toml; edit it by hand")
        return _write(p, text)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        return {"ok": False, "error": str(e), "field": field}


def _write(p: core.Project, text: str) -> dict:
    core._write_project(p, text)
    return {"ok": True, "settings": settings_view(p)}


def save_toml(p: core.Project, text: str, allow_repo_change: bool = False) -> dict:
    def bad(msg: str, field: str = "toml") -> dict:
        return {"ok": False, "error": msg, "field": field}
    if not isinstance(text, str):
        return bad("the text is not a string")
    if len(text.encode("utf-8")) > MAX_TOML:
        return bad("project.toml is larger than 64 KB")
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return bad(f"not valid TOML: {e}")
    if not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
        return bad("name must be a text", "name")
    repo = cfg.get("repo")
    if not isinstance(repo, dict) or not isinstance(repo.get("path"), str) or not isinstance(repo.get("base"), str):
        return bad("[repo] needs path and base as text", "repo")
    if not allow_repo_change:
        cur = p.cfg.get("repo") or {}
        for k in ("path", "worktrees"):
            if repo.get(k) != cur.get(k):
                return bad(f"repo.{k} can't be changed here", "repo")
    if "account" in cfg and cfg["account"] not in [a.get("name") for a in core.settings().get("accounts", [])]:
        return bad(f"no account {cfg['account']!r}", "account")
    g = cfg.get("gates", {})
    if not isinstance(g, dict):
        return bad("[gates] must be a table", "gates")
    for k in ("quick", "final"):
        if k in g and not (isinstance(g[k], list) and all(isinstance(c, str) for c in g[k])):
            return bad(f"gates.{k} must be a list of texts", k)
    try:
        return _write(p, text)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        return bad(str(e))
