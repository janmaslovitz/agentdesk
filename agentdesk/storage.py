"""AgentDesk Server: the server-wide disk budget (stdlib only, no import of core; used by the gateway and the
`agentdesk-server` console tool).

- Budget: `disk_total_gb` (512) with `disk_reserve_gb` (40) that must stay free, a soft limit (warn + auto-clean)
  and a hard limit (new runs, clones, imports and uploads are refused; running work is never stopped). The real
  file-system usage (shutil.disk_usage) counts, since other programs use the disk too.
- Quotas: `quota_gb` per user (an override per user in users.json). Over quota: that user's new runs / clones are
  refused and their data is cleaned first.
- Measuring is a background walker (never in a request): results are cached in `<server-home>/storage.json`.
- Each instance gets `<user home>/server-policy.json` (read by core.server_policy): the server's [hygiene]
  defaults (build cleanup on), the gate, the environment for the shared package caches and the clone options.
- Shared package caches in `<server-home>/cache/<tool>` (content-addressed tools only: cargo registry/git, npm, pip,
  uv, go modules, gradle modules), each with a size cap and LRU / age pruning inside that folder only, optional
  hard-link dedupe of identical files.
- Cleaning reuses disk hygiene (run per user by `agentdesk.gateway.projtool clean`, with its safety rules) and
  every deletion goes to the audit log.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from .fsutil import GB, MB, _entry_dir, _key, _lp, _plain, fmt, inside, is_link, safe_delete, walk_size

DEFAULTS: dict = {
    "disk_total_gb": 512.0,         # the disk budget of the server (smaller of this and the real disk)
    "disk_reserve_gb": 40.0,        # free space that must always remain (below it: hard limit)
    "soft_limit_pct": 80.0,         # warn + automatic cleanup from here
    "hard_limit_pct": 92.0,         # refuse new runs / clones / imports / uploads from here
    "quota_gb": 50.0,               # per user (users.json "quota_gb" overrides; 0 = no quota)
    "storage_scan_minutes": 15.0,   # how often the background walker measures
    "auto_clean": True,             # clean automatically at the soft limit and for users over quota
    "auto_clean_interval_min": 30.0,
    "build_stale_days": 2.0,        # [hygiene] defaults on the server (a project's own settings still win)
    "build_max_target_gb": 20.0,
    "build_max_project_gb": 60.0,
    "worktree_ttl_hours": 24.0,
    "max_worktrees": 6,
    "git_gc_hours": 24.0,
    "offpeak_hours": "01-06",
    "large_file_mb": 50.0,
    "clone_depth": 0,               # >0: shallow clones when a project is cloned from a URL
    "partial_clone": False,         # --filter=blob:none
    "shared_caches": True,
    "cache_caps_gb": {"cargo": 30.0, "npm": 15.0, "pip": 10.0, "uv": 15.0, "go": 10.0, "gradle": 20.0},
    "cache_max_age_days": 30.0,     # cache entries unused this long go even under the cap
    "cache_min_age_hours": 2.0,     # ... and entries used more recently are never touched
    "cache_dedupe": False,          # hard-link identical files inside the shared caches
    "shared_target": False,         # one CARGO_TARGET_DIR per user and project, shared by its worktrees
    "shared_target_max_gb": 30.0,
    "max_upload_mb": 200.0,         # largest project upload (.zip import)
    "temp_max_age_hours": 24.0,     # imports / upload spool left-overs
}

# tool -> what to share and how to prune. units: glob patterns (relative to the tool's cache folder) of the pieces
# that may go one by one ("**" = every file below; "**/x*" = every folder matching x* at any depth).
CACHE_TOOLS: dict[str, dict] = {
    "cargo": {"units": ("registry/cache/*/*", "registry/src/*/*", "git/checkouts/*/*", "git/db/*")},
    "npm": {"units": ("_cacache/content-v2/*/*/*/*", "_cacache/index-v5/*/*/*", "_npx/*")},
    "pip": {"units": ("http-v2/**", "http/**", "wheels/**")},
    "uv": {"units": ("*/*",)},
    "go": {"units": ("cache/download/**", "**/*@*")},
    "gradle": {"units": ("files-2.1/*/*/*",)},
}
BUILD_NAMES = {"target", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
               ".gradle", "build", "dist", ".next", ".nuxt", ".turbo", ".parcel-cache"}
MANIFESTS = ("Cargo.toml", "pom.xml", "package.json", "pyproject.toml", "setup.py", "build.gradle", "build.gradle.kts")
CATEGORIES = ("repos", "worktrees", "builds", "caches", "chats", "logs", "other")
POLICY = "server-policy.json"
STATE = "storage.json"


# ----------------------------------------------------------------------------- settings and pure logic

def caps(cfg: dict) -> dict[str, float]:
    """Per-tool cache caps in GB (unknown tools / bad values fall back to the defaults)."""
    out = dict(DEFAULTS["cache_caps_gb"])
    for k, v in (cfg.get("cache_caps_gb") or {}).items():
        if k in out and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = float(v)
    return out


def validate(cfg: dict) -> str | None:
    """Why the storage settings are inconsistent, or None."""
    s, h = float(cfg.get("soft_limit_pct", 80)), float(cfg.get("hard_limit_pct", 92))
    if not 0 < s < h <= 100:
        return "soft_limit_pct must be above 0 and below hard_limit_pct, which is at most 100"
    for k in ("disk_total_gb", "disk_reserve_gb", "quota_gb", "storage_scan_minutes", "max_upload_mb"):
        if float(cfg.get(k, 0)) < 0:
            return f"{k} must not be negative"
    if float(cfg.get("disk_reserve_gb", 0)) >= float(cfg.get("disk_total_gb", 512)):
        return "disk_reserve_gb must be smaller than disk_total_gb"
    return None


def budget(cfg: dict, fs_total: int, fs_used: int, fs_free: int, agentdesk_bytes: int = 0) -> dict:
    """Where the server stands: {total, used, free, pct, level ok|soft|hard, message}. When the file system is the
    budget's own disk (not much bigger than disk_total_gb), its real usage counts (other programs use it too);
    on a bigger shared disk AgentDesk's own measured usage counts. The reserve is always checked against the real
    free space."""
    limit = float(cfg["disk_total_gb"]) * GB
    total = int(min(limit, fs_total) if fs_total else limit)
    used = int(fs_used if not fs_total or fs_total <= limit * 1.05 else agentdesk_bytes)
    free = int(max(0, min(fs_free if fs_total else total - used, total - used)))
    pct = 100.0 * used / total if total else 100.0
    reserve = float(cfg["disk_reserve_gb"]) * GB
    soft, hard = float(cfg["soft_limit_pct"]), float(cfg["hard_limit_pct"])
    if pct >= hard or free < reserve:
        level = "hard"
        msg = (f"the server disk is {pct:.0f}% full ({fmt(free)} free, {fmt(reserve)} must stay free; hard limit "
               f"{hard:g}%): new runs, clones, imports and uploads are refused until space is freed (running work goes on)")
    elif pct >= soft:
        level = "soft"
        msg = f"the server disk is {pct:.0f}% full (soft limit {soft:g}%): old build output and caches are being cleaned"
    else:
        level, msg = "ok", ""
    return {"total": total, "used": used, "free": free, "pct": round(pct, 1), "level": level, "message": msg,
            "soft_pct": soft, "hard_pct": hard, "reserve": int(reserve)}


def quota_bytes(cfg: dict, user: dict | None) -> int:
    """The user's quota in bytes (0 = none): their own quota_gb, else the server default."""
    q = (user or {}).get("quota_gb")
    if q is None:
        q = cfg.get("quota_gb", 0)
    try:
        return max(0, int(float(q) * GB))
    except (TypeError, ValueError):
        return 0


def user_level(used: int, quota: int) -> str:
    if quota <= 0:
        return "ok"
    return "over" if used >= quota else "warn" if used >= 0.9 * quota else "ok"


def gate(bud: dict | None, used: int, quota: int) -> str | None:
    """Why this user's new runs / clones / imports / uploads are refused now, or None."""
    if bud and bud.get("level") == "hard":
        return bud["message"] + "; tell the administrator"
    if user_level(used, quota) == "over":
        return (f"you use {fmt(used)} of your {fmt(quota)} disk quota: new runs, clones and imports are refused until "
                "your usage is below it (finished worktrees and old build output are cleaned automatically; ask the "
                "administrator for more)")
    return None


# ----------------------------------------------------------------------------- measuring a user's home

def _has_manifest(d: Path) -> bool:
    return any(os.path.isfile(_lp(d.parent / m)) for m in MANIFESTS) or os.path.isfile(_lp(d / "CACHEDIR.TAG"))


def _is_build(d: Path) -> bool:
    n = d.name
    if n not in BUILD_NAMES:
        return False
    if n in ("target", "build", "dist"):
        return _has_manifest(d)
    if n in (".venv", "venv"):
        return os.path.isfile(_lp(d / "pyvenv.cfg"))
    return True


def measure_home(home: Path, top_n: int = 10) -> dict:
    """Bytes of one user's data folder by category (repos, worktrees, builds, caches, chats, logs, other), per project,
    and the biggest single items (top consumers). Never follows links (shared caches linked in are not counted)."""
    cats = dict.fromkeys(CATEGORIES, 0)
    projects: dict[str, int] = {}
    units: list[dict] = []
    total = files = errors = 0
    home = Path(home)
    if not os.path.isdir(_lp(home)) or is_link(home):
        return {"bytes": 0, "categories": cats, "projects": {}, "top": [], "errors": 0}
    # stack: (dir, category, project slug or None, unit index or None)
    stack: list[tuple[Path, str, str | None, int | None]] = [(home, "other", None, None)]
    while stack:
        d, cat, proj, unit = stack.pop()
        try:
            it = os.scandir(_lp(d))
        except OSError:
            errors += 1
            continue
        with it:
            entries = list(it)
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                p = Path(_plain(e.path))
                if e.is_dir(follow_symlinks=False):
                    if not _entry_dir(e):
                        continue
                    c, pj, u = cat, proj, unit
                    rel = p.relative_to(home).parts
                    if len(rel) == 1 and rel[0] in ("cache", "cargo-home", "gradle-home"):
                        c = "caches"
                    elif len(rel) == 1 and rel[0] == "cargo-target":
                        c = "builds"
                    elif len(rel) == 1 and rel[0] == "repos":
                        c = "repos"
                    elif len(rel) == 2 and rel[0] in ("repos", "projects", "cargo-target"):
                        pj = rel[1]
                        units.append({"path": str(p), "category": "repos" if rel[0] == "repos" else c if rel[0] == "cargo-target" else "data", "bytes": 0})
                        u = len(units) - 1
                    elif p.name == "worktrees" and c not in ("builds", "caches"):
                        c = "worktrees"
                    elif c == "worktrees" and p.parent.name == "worktrees":
                        units.append({"path": str(p), "category": "worktrees", "bytes": 0})
                        u = len(units) - 1
                    elif c != "caches" and _is_build(p):
                        c = "builds"
                        units.append({"path": str(p), "category": "builds", "bytes": 0})
                        u = len(units) - 1
                    elif c == "other" and len(rel) == 3 and rel[0] == "projects" and p.name in ("runs", "reports", "chats"):
                        c = "chats"
                    stack.append((p, c, pj, u))
                    continue
                st = e.stat(follow_symlinks=False)
                size = st.st_size
                c = cat
                if c in ("other", "repos", "worktrees") and (".log" in e.name and (e.name.endswith(".log") or e.name.rsplit(".", 1)[-1].isdigit())):
                    c = "logs"
                elif c == "other" and proj and e.name.startswith("chat") and e.name.endswith(".json"):
                    c = "chats"
                cats[c] += size
                total += size
                files += 1
                if proj:
                    projects[proj] = projects.get(proj, 0) + size
                if unit is not None:
                    units[unit]["bytes"] += size
            except OSError:
                errors += 1
    top = sorted((x for x in units if x["bytes"]), key=lambda x: -x["bytes"])[:top_n]
    return {"bytes": total, "files": files, "categories": cats, "projects": projects, "top": top, "errors": errors}


# ----------------------------------------------------------------------------- shared package caches

def cache_root(home: Path) -> Path:
    return Path(home) / "cache"


def _match_units(root: Path, pattern: str) -> list[Path]:
    """Entries below `root` matching a unit pattern (segments with fnmatch; "**" = every file below, "**/x" = every
    folder matching x at any depth, not entered further). Links are never followed or returned."""
    segs = pattern.split("/")
    out: list[Path] = []

    def scan(d: Path) -> list[os.DirEntry]:
        try:
            with os.scandir(_lp(d)) as it:
                return [e for e in it if not e.is_symlink() and not (hasattr(e, "is_junction") and e.is_junction())
                        and not (e.is_dir(follow_symlinks=False) and not _entry_dir(e))]
        except OSError:
            return []

    def walk(d: Path, i: int) -> None:
        if i >= len(segs):
            return
        s = segs[i]
        if s == "**":
            rest = segs[i + 1:]
            stack = [d]
            while stack:
                cur = stack.pop()
                for e in scan(cur):
                    p = Path(_plain(e.path))
                    is_dir = _entry_dir(e)
                    if rest:
                        if is_dir and fnmatch.fnmatch(e.name, rest[0]):
                            out.append(p)
                        elif is_dir:
                            stack.append(p)
                    elif is_dir:
                        stack.append(p)
                    else:
                        out.append(p)
            return
        for e in scan(d):
            if not fnmatch.fnmatch(e.name, s):
                continue
            p = Path(_plain(e.path))
            if i == len(segs) - 1:
                out.append(p)
            elif _entry_dir(e):
                walk(p, i + 1)

    walk(Path(root), 0)
    seen: set[str] = set()
    uniq = []
    for p in out:  # a unit inside another unit counts once (the outer one)
        k = _key(p)
        if k not in seen and not any(inside(p, q) and _key(q) != k for q in out if len(_key(q)) < len(k)):
            seen.add(k)
            uniq.append(p)
    return uniq


def prune_plan(tool_dir: Path, units: tuple[str, ...], cap: int, max_age_s: float, min_age_s: float,
               now: float | None = None) -> dict:
    """What pruning one tool's cache would delete: units unused (atime/mtime) for longer than max_age_s, then the
    least recently used ones while the folder is over `cap` bytes. Units used within min_age_s are never touched."""
    now = now or time.time()
    total = walk_size(tool_dir).bytes if os.path.isdir(_lp(tool_dir)) else 0
    rows = []
    for u in {_key(p): p for pat in units for p in _match_units(tool_dir, pat)}.values():
        s = walk_size(u, atime=True)
        rows.append((s.used or s.newest, s.bytes, u))
    rows.sort(key=lambda r: r[0])
    left, items = total, []
    for used, size, u in rows:
        age = now - used if used else float("inf")
        if age < min_age_s:
            continue
        if age > max_age_s:
            why = f"unused for {age / 86400:.0f} days"
        elif cap and left > cap:
            why = f"cache over its cap ({fmt(left)} > {fmt(cap)}): least recently used first"
        else:
            continue
        items.append({"path": str(u), "bytes": size, "why": why})
        left -= size
    return {"path": str(tool_dir), "bytes": total, "cap": cap, "after": left, "items": items}


def prune_cache(cache_dir: Path, tool_dir: Path, plan: dict) -> tuple[int, list[dict]]:
    """Carry out a prune_plan: every deletion stays inside the tool's folder of the shared cache (refused through a
    link or outside it). Returns (bytes freed, done items)."""
    freed, done = 0, []
    if is_link(cache_dir) or is_link(tool_dir) or not inside(tool_dir, cache_dir):
        return 0, []
    for it in plan["items"]:
        f, _err, why = safe_delete(tool_dir, it["path"])
        if why is None:
            freed += f
            done.append({**it, "freed": f})
    return freed, done


def dedupe(root: Path, min_size: int = 64 * 1024, min_age_s: float = 3600, now: float | None = None,
           dry_run: bool = False) -> dict:
    """Replace identical files (same size and SHA-256, same device) below `root` by hard links to one copy. Only
    files untouched for min_age_s; links are never followed. Returns {saved, linked}."""
    now = now or time.time()
    by_size: dict[tuple[int, int], list[tuple[Path, os.stat_result]]] = {}
    stack = [Path(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(_lp(d)) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if _entry_dir(e):
                    stack.append(Path(_plain(e.path)))
                    continue
                st = os.lstat(_lp(e.path))  # (DirEntry.stat leaves st_ino / st_dev 0 on Windows)
                if st.st_size >= min_size and now - st.st_mtime >= min_age_s:
                    by_size.setdefault((st.st_dev, st.st_size), []).append((Path(_plain(e.path)), st))
            except OSError:
                pass
    saved = linked = 0
    for (_dev, size), group in by_size.items():
        if len({st.st_ino for _p, st in group}) < 2:
            continue
        keep: dict[str, tuple[Path, int]] = {}
        for p, st in group:
            try:
                h = hashlib.sha256()
                with open(_lp(p), "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                digest = h.hexdigest()
            except OSError:
                continue
            first = keep.get(digest)
            if first is None:
                keep[digest] = (p, st.st_ino)
                continue
            if first[1] == st.st_ino:
                continue
            if not dry_run:
                tmp = p.with_name(p.name + ".dedupe-tmp")
                try:
                    os.link(_lp(first[0]), _lp(tmp))
                    os.replace(_lp(tmp), _lp(p))
                except OSError:
                    try:
                        os.unlink(_lp(tmp))
                    except OSError:
                        pass
                    continue
            saved += size
            linked += 1
    return {"saved": saved, "linked": linked}


def _link_dir(link: Path, target: Path) -> bool:
    """`link` -> `target` (a directory symlink). An existing correct link is kept; an existing empty folder is
    replaced; anything else is left alone (False)."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        if os.path.islink(link):
            if _key(os.path.realpath(link)) == _key(os.path.realpath(target)):
                return True
            return False
        if link.exists():
            if link.is_dir() and not any(link.iterdir()):
                link.rmdir()
            else:
                return False
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def instance_env(home: Path, cfg: dict, user_home: Path) -> dict[str, str]:
    """Environment for a user's instance (and its runs): package caches in `<server-home>/cache/<tool>`. With
    run_as_users the caches stay per user (other OS users can't share the folders): `<user home>/cache/<tool>`.
    Cargo and Gradle keep a per-user home (credentials, config) whose registry / module folders link to the shared
    cache; when links can't be made they stay per user."""
    if not cfg.get("shared_caches", True):
        return {}
    shared = not cfg.get("run_as_users")
    root = cache_root(home) if shared else Path(user_home) / "cache"
    env = {"npm_config_cache": str(root / "npm"), "PIP_CACHE_DIR": str(root / "pip"), "UV_CACHE_DIR": str(root / "uv"),
           "GOMODCACHE": str(root / "go")}
    cargo_home, gradle_home = Path(user_home) / "cargo-home", Path(user_home) / "gradle-home"
    try:
        for d in (root, cargo_home, gradle_home / "caches"):
            d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    if all(_link_dir(cargo_home / sub, root / "cargo" / sub) for sub in ("registry", "git")) or not shared:
        env["CARGO_HOME"] = str(cargo_home)
    if _link_dir(gradle_home / "caches" / "modules-2", root / "gradle") or not shared:
        env["GRADLE_USER_HOME"] = str(gradle_home)
    return env


# ----------------------------------------------------------------------------- the server's storage manager

def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_state(home: Path) -> dict:
    try:
        data = json.loads((Path(home) / STATE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Storage:
    """Background measuring, gates, policies and cleanup for one server home.

    users: a users.UserStore; audit: users.Audit; tool(user record, *args) -> dict runs the project tool for that
    user (agentdesk.gateway.projtool: `clean --dry-run|--apply`); busy(username) -> bool says an instance is
    working (cache purges wait for quiet)."""

    def __init__(self, home: Path, cfg: dict, users, audit, tool: Callable | None = None,
                 busy: Callable[[str], bool] | None = None, clock: Callable[[], float] = time.time):
        self.home, self.cfg, self.users, self.audit, self.tool, self.busy, self.clock = Path(home), cfg, users, audit, tool, busy, clock
        self.state = load_state(self.home)
        self.lock = threading.Lock()
        self.scanning = False
        self.job: dict = {}
        self.last_auto = 0.0

    # -- measuring
    def scan(self) -> dict:
        """Walk every user's home, the shared caches and the spool folders; compute the budget, write storage.json
        and every user's policy. Slow on a big disk: runs in the background thread (tick) or the console tool."""
        now = self.clock()
        t0 = time.time()
        users = {}
        for u in self.users.list():
            m = measure_home(Path(u["home"]))
            q = quota_bytes(self.cfg, u)
            users[u["username"]] = {**m, "quota": q, "level": user_level(m["bytes"], q), "disabled": u["disabled"],
                                    "quota_custom": u.get("quota_gb") is not None}
        caches = {}
        cr = cache_root(self.home)
        for tool in CACHE_TOOLS:
            d = cr / tool
            caches[tool] = {"bytes": walk_size(d).bytes if os.path.isdir(_lp(d)) else 0, "cap": int(caps(self.cfg)[tool] * GB)}
        spool = {n: (walk_size(self.home / n).bytes if (self.home / n).is_dir() else 0) for n in ("uploads", "imports", "logs")}
        try:
            du = shutil.disk_usage(self.home)
            fs = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            fs = {"total": 0, "used": 0, "free": 0}
        ours = sum(x["bytes"] for x in users.values()) + sum(c["bytes"] for c in caches.values()) + sum(spool.values())
        bud = budget(self.cfg, fs["total"], fs["used"], fs["free"], ours)
        state = {"at": dt.datetime.fromtimestamp(now).isoformat(timespec="seconds"), "ts": now,
                 "took_s": round(time.time() - t0, 2), "fs": fs, "agentdesk": ours, "budget": bud,
                 "users": users, "caches": caches, "spool": spool,
                 "plan": self.state.get("plan"), "last_clean": self.state.get("last_clean")}
        with self.lock:
            self.state = state
        try:
            write_json(self.home / STATE, state)
        except OSError:
            pass
        for u in self.users.list():
            try:
                self.write_policy(u["username"], Path(u["home"]))
            except OSError:
                pass
        return state

    def fs_now(self) -> dict:
        """The real file-system numbers right now (cheap: one statvfs) and the budget level from them."""
        try:
            du = shutil.disk_usage(self.home)
        except OSError:
            return self.state.get("budget") or {}
        return budget(self.cfg, du.total, du.used, du.free, int(self.state.get("agentdesk") or 0))

    # -- per user
    def usage(self, username: str) -> dict:
        u = (self.state.get("users") or {}).get(username) or {}
        user = self.users.get(username) if self.users else None
        q = quota_bytes(self.cfg, user)
        used = int(u.get("bytes") or 0)
        return {"used": used, "quota": q, "level": user_level(used, q), "measured": bool(u)}

    def gate_for(self, username: str) -> str | None:
        """Why this user's new runs / clones / imports / uploads are refused (cached numbers; the real free space is
        looked at now since a full disk can't wait for the next scan)."""
        us = self.usage(username)
        return gate(self.fs_now(), us["used"], us["quota"])

    def policy(self, username: str, user_home: Path) -> dict:
        c = self.cfg
        why = self.gate_for(username)
        every = float(c["storage_scan_minutes"]) * 60
        shared_target = str(Path(user_home) / "cargo-target") if c.get("shared_target") else None
        return {
            "version": 1, "written": self.clock(),
            "hygiene": {"auto": bool(c["auto_clean"]), "artifacts": True, "stale_days": float(c["build_stale_days"]),
                        "max_target_gb": float(c["shared_target_max_gb"] if shared_target else c["build_max_target_gb"]),
                        "max_total_gb": float(c["build_max_project_gb"]), "worktree_ttl_hours": float(c["worktree_ttl_hours"]),
                        "max_worktrees": float(c["max_worktrees"]), "git_gc_hours": float(c["git_gc_hours"]),
                        "offpeak_hours": str(c["offpeak_hours"]), "large_file_mb": float(c["large_file_mb"])},
            "gate": {"refuse": bool(why), "reason": why or "", "until": self.clock() + max(3600.0, 4 * every)},
            "env": instance_env(self.home, c, Path(user_home)),
            "git": {"clone_depth": int(c["clone_depth"]), "partial_clone": bool(c["partial_clone"])},
            "max_upload_mb": float(c["max_upload_mb"]),
            "shared_target_dir": shared_target,
            "usage": self.usage(username),
        }

    def write_policy(self, username: str, user_home: Path) -> dict:
        pol = self.policy(username, user_home)
        Path(user_home).mkdir(parents=True, exist_ok=True)
        write_json(Path(user_home) / POLICY, pol)
        return pol

    # -- audit of deletions done by instances / the project tool
    def ingest_audit(self, username: str, user_home: Path) -> int:
        """Move `<user home>/storage-audit.jsonl` (deletions done by disk hygiene in that user's processes) into the
        audit log."""
        f = Path(user_home) / "storage-audit.jsonl"
        tmp = f.with_name(f.name + ".ingest")
        try:
            os.replace(f, tmp)
        except OSError:
            return 0
        n = 0
        try:
            for line in tmp.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    self.audit.log("storage deleted", user=username, by="disk hygiene", project=r.get("project"),
                                   kind=r.get("kind"), path=r.get("path"), bytes=r.get("bytes"), why=r.get("why"))
                    n += 1
        finally:
            tmp.unlink(missing_ok=True)
        return n

    # -- cleaning
    def cache_plans(self) -> dict:
        c, now = self.cfg, self.clock()
        cr = cache_root(self.home)
        out = {}
        for tool, spec in CACHE_TOOLS.items():
            d = cr / tool
            if not os.path.isdir(_lp(d)) or is_link(d):
                continue
            out[tool] = prune_plan(d, spec["units"], int(caps(c)[tool] * GB), float(c["cache_max_age_days"]) * 86400,
                                   float(c["cache_min_age_hours"]) * 3600, now)
        return out

    def spool_plan(self) -> list[dict]:
        out, now = [], self.clock()
        for name in ("uploads", "imports"):
            root = self.home / name
            if not root.is_dir() or is_link(root):
                continue
            for e in os.scandir(_lp(root)):
                p = Path(_plain(e.path))
                s = walk_size(p)
                if s.newest and now - s.newest >= float(self.cfg["temp_max_age_hours"]) * 3600:
                    out.append({"path": str(p), "bytes": s.bytes, "root": str(root),
                                "why": f"temporary upload / import older than {float(self.cfg['temp_max_age_hours']):g} h"})
        return out

    def order(self, only: list[str] | None = None) -> list[dict]:
        """Users to clean, over-quota ones first, then by usage (largest first)."""
        rows = [u for u in self.users.list() if only is None or u["username"] in only]
        st = self.state.get("users") or {}
        rank = {"over": 0, "warn": 1, "ok": 2}
        return sorted(rows, key=lambda u: (rank.get((st.get(u["username"]) or {}).get("level", "ok"), 2),
                                           -int((st.get(u["username"]) or {}).get("bytes") or 0)))

    def clean(self, dry_run: bool = True, only: list[str] | None = None, by: str = "auto", caches: bool = True) -> dict:
        """One cleanup pass (or its dry run): each user's projects through disk hygiene (the project tool, with every
        hygiene safety rule: never outside the project's folders, never while a run uses them), the shared caches,
        the upload / import spool. Every deletion is audited."""
        res: dict = {"at": dt.datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds"), "dry_run": dry_run,
                     "by": by, "users": {}, "caches": {}, "spool": [], "bytes": 0, "errors": []}
        for u in self.order(only):
            if self.tool is None:
                break
            try:
                r = self.tool(u, "clean", "--dry-run" if dry_run else "--apply")
            except Exception as e:  # one user's broken data must not stop the others
                res["errors"].append(f"{u['username']}: {e}")
                continue
            projects = r.get("projects", []) if isinstance(r, dict) else []
            res["users"][u["username"]] = projects
            res["bytes"] += sum(int(p.get("reclaimable" if dry_run else "freed") or 0) for p in projects)
            if not dry_run:
                self.ingest_audit(u["username"], Path(u["home"]))
        if caches:
            busy = any(self.busy(u["username"]) for u in self.users.list()) if self.busy else False
            for tool, plan in self.cache_plans().items():
                if dry_run:
                    res["caches"][tool] = plan
                    res["bytes"] += sum(i["bytes"] for i in plan["items"])
                    continue
                freed, done = prune_cache(cache_root(self.home), cache_root(self.home) / tool, plan)
                for it in done:
                    self.audit.log("storage deleted", by=by, kind=f"cache:{tool}", path=it["path"], bytes=it["freed"], why=it["why"])
                saved = {}
                if self.cfg.get("cache_dedupe") and not busy:
                    saved = dedupe(cache_root(self.home) / tool, min_age_s=float(self.cfg["cache_min_age_hours"]) * 3600)
                    if saved.get("linked"):
                        self.audit.log("storage dedupe", by=by, kind=f"cache:{tool}", files=saved["linked"], bytes=saved["saved"])
                res["caches"][tool] = {**plan, "freed": freed, "done": len(done), "dedupe": saved}
                res["bytes"] += freed
        for it in self.spool_plan():
            if not dry_run:
                f, _e, why = safe_delete(it["root"], it["path"])
                if why:
                    continue
                it = {**it, "freed": f}
                self.audit.log("storage deleted", by=by, kind="spool", path=it["path"], bytes=f, why=it["why"])
            res["spool"].append(it)
            res["bytes"] += it.get("freed", it["bytes"])
        key = "plan" if dry_run else "last_clean"
        with self.lock:
            self.state[key] = res
        if not dry_run:
            self.audit.log("storage clean", by=by, freed=res["bytes"], users=len(res["users"]))
        return res

    def start_job(self, kind: str, by: str, only: list[str] | None = None) -> dict:
        """Run clean(dry_run=kind == "plan") in a background thread (the Storage page polls); one at a time."""
        with self.lock:
            if self.job.get("running"):
                return {"ok": False, "message": f"a storage {self.job.get('kind')} is already running"}
            self.job = {"running": True, "kind": kind, "started": self.clock(), "by": by}

        def go() -> None:
            err = None
            try:
                self.clean(dry_run=kind == "plan", only=only, by=by)
                if kind != "plan":
                    self.scan()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.audit.log("storage error", by=by, error=err[:300])
            with self.lock:
                self.job = {**self.job, "running": False, "finished": self.clock(), "error": err}

        threading.Thread(target=go, daemon=True, name=f"storage-{kind}").start()
        return {"ok": True}

    # -- the background loop (called every few seconds by the gateway's supervisor; never blocks it)
    def due(self) -> bool:
        return self.clock() - float(self.state.get("ts") or 0) >= float(self.cfg["storage_scan_minutes"]) * 60

    def tick(self) -> None:
        with self.lock:
            if self.scanning or self.job.get("running") or not self.due():
                return
            self.scanning = True
        threading.Thread(target=self._scan_and_act, daemon=True, name="storage-scan").start()

    def _scan_and_act(self) -> None:
        try:
            st = self.scan()
            for u in self.users.list():
                self.ingest_audit(u["username"], Path(u["home"]))
            if not self.cfg.get("auto_clean"):
                return
            over = [n for n, x in st["users"].items() if x["level"] == "over"]
            soft = st["budget"]["level"] in ("soft", "hard")
            if (over or soft) and self.clock() - self.last_auto >= float(self.cfg["auto_clean_interval_min"]) * 60:
                self.last_auto = self.clock()
                self.audit.log("storage auto clean", reason=st["budget"]["level"] if soft else "over quota",
                               users=",".join(over) if not soft else "all", pct=st["budget"]["pct"])
                self.clean(dry_run=False, only=None if soft else over, by="auto")
                self.scan()
        except Exception as e:  # never let the background job die
            self.audit.log("storage error", error=repr(e)[:300])
        finally:
            with self.lock:
                self.scanning = False

    # -- views
    def view(self) -> dict:
        """The Storage page: totals, per user, per project, per cache, the last dry run and cleanup, the job."""
        with self.lock:
            st = json.loads(json.dumps(self.state))
            job = dict(self.job)
        st["budget_now"] = self.fs_now()
        st["job"] = job
        st["scanning"] = self.scanning
        st["cfg"] = {k: self.cfg.get(k) for k in DEFAULTS}
        return st


def bar_html(usage: dict) -> str:
    """The small usage bar in the dashboard header (gateway bar)."""
    used, q = int(usage.get("used") or 0), int(usage.get("quota") or 0)
    if not usage.get("measured"):
        return ""
    pct = min(100, int(100 * used / q)) if q else 0
    color = "#d64545" if usage.get("level") == "over" or usage.get("gate") else "#c27c0e" if usage.get("level") == "warn" else "#1f9d55"
    title = f"Disk: {fmt(used)}" + (f" of your {fmt(q)} quota" if q else "") + (f" - {usage['gate']}" if usage.get("gate") else "")
    bar = (f'<span style="display:inline-block;width:46px;height:6px;border-radius:3px;background:rgba(127,127,127,.3);'
           f'overflow:hidden;vertical-align:middle"><span style="display:block;height:100%;width:{pct}%;background:{color}"></span></span>') if q else ""
    import html as _h
    return f'<span title="{_h.escape(title)}" style="opacity:.85">{bar} {_h.escape(fmt(used))}{" / " + _h.escape(fmt(q)) if q else ""}</span>'


__all__ = ["DEFAULTS", "CACHE_TOOLS", "Storage", "budget", "gate", "quota_bytes", "user_level", "measure_home",
           "prune_plan", "prune_cache", "dedupe", "instance_env", "validate", "caps", "bar_html", "MB"]
