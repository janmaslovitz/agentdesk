"""Disk hygiene: how much disk a project uses (its cargo target dirs, its worktrees, its AgentDesk data folder) and
cleaning up what is only a stale build cache, a finished worktree or an old run log.

Careful by design:
- measuring walks with os.scandir (the size and mtime come with the directory listing on Windows: no stat storm)
  and never follows symlinks or junctions (reparse points on Windows);
- deleting happens only inside the project's configured locations, only in folders that are clearly cargo target
  dirs (CACHEDIR.TAG, .rustc_info.json or named target*), never in a folder holding sources (.git, Cargo.toml),
  never while a run is active for the project or a cargo/rustc/rustup process uses the folder; when that can't be
  checked, nothing is deleted;
- by default ([hygiene] auto = false) nothing is deleted by itself: the dashboard shows what would be deleted and
  deletes it only when the user confirms. project.toml is never edited here.

Measuring runs in a background thread and is cached (CACHE_TTL_S); dashboard requests only read the cache.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import core

GB = 1024 ** 3
MB = 1024 ** 2
CACHE_TTL_S = 300                  # a dashboard request older than this starts a new measurement in the background
TRIM_ZONES = ("deps", "build", ".fingerprint", "incremental")  # per profile: the parts cargo-sweep trims by age
TARGET_MARKERS = ("CACHEDIR.TAG", ".rustc_info.json")
SOURCE_MARKERS = (".git", "Cargo.toml")  # a folder holding these is never a build cache
CARGO_NAMES = {"cargo", "rustc", "rustup", "rustdoc", "clippy-driver", "cargo-clippy", "cargo-fmt", "rustfmt"}
TOOLS_WITH_PATHS = {"rustc", "rustdoc", "clippy-driver"}  # their command line names every folder they write to
REPARSE = 0x400                    # FILE_ATTRIBUTE_REPARSE_POINT: symlink, junction, mount point ...
ACTIVE = ("working", "routing")
ORDER = ("logs", "worktree", "branch", "orphan", "trim", "profile")  # the order actions are applied in


def fmt(n: float) -> str:
    n = float(n or 0)
    for unit, size in (("GB", GB), ("MB", MB), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


# ----------------------------------------------------------------------------- paths

def _lp(path) -> str:
    """Absolute path; on Windows in the \\\\?\\ form so paths longer than 260 characters work."""
    s = os.path.abspath(str(path))
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _plain(path) -> str:
    s = str(path)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    return s[4:] if s.startswith("\\\\?\\") else s


def _key(path) -> str:
    """Comparable form of a path (case and separators normalized on Windows, no long-path prefix)."""
    return os.path.normcase(os.path.normpath(os.path.abspath(_plain(path))))


def inside(path, root) -> bool:
    """`path` is `root` or below it (by name only: links are checked separately)."""
    p, r = _key(path), _key(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _st_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & REPARSE)


def is_link(path) -> bool:
    """A symlink, junction or other reparse point (unreadable counts as one: never walked)."""
    try:
        return _st_link(os.lstat(_lp(path)))
    except OSError:
        return True


def _entry_dir(e: os.DirEntry) -> bool:
    """A real directory to descend into: not a symlink, junction or other reparse point."""
    try:
        if not e.is_dir(follow_symlinks=False) or e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
            return False
        return not _st_link(e.stat(follow_symlinks=False))
    except OSError:
        return False


def _subdirs(path) -> list[Path]:
    try:
        with os.scandir(_lp(path)) as it:
            return [Path(_plain(e.path)) for e in it if _entry_dir(e)]
    except OSError:
        return []


def _links_between(root, path) -> bool:
    """Is any folder from `root` down to `path` (both included) a link? Then deleting there could leave the folder."""
    cur = Path(_plain(os.path.abspath(_plain(path))))
    stop = _key(root)
    while True:
        if is_link(cur):
            return True
        if _key(cur) == stop or cur.parent == cur:
            return False
        cur = cur.parent


# ----------------------------------------------------------------------------- measuring

@dataclass
class Size:
    bytes: int = 0
    files: int = 0
    newest: float = 0.0   # newest mtime of anything inside (files and folders), 0 = nothing seen
    errors: int = 0

    def add(self, o: "Size") -> "Size":
        self.bytes += o.bytes
        self.files += o.files
        self.newest = max(self.newest, o.newest)
        self.errors += o.errors
        return self


def walk_size(path, skip=()) -> Size:
    """Total size of the files under `path` with os.scandir, never following links/junctions; folders in `skip`
    (measured elsewhere) are left out. A plain file gives its own size."""
    out = Size()
    skip_k = {_key(s) for s in skip}
    try:
        st = os.lstat(_lp(path))
    except OSError:
        out.errors += 1
        return out
    if _st_link(st):
        return out
    out.newest = st.st_mtime
    if not stat.S_ISDIR(st.st_mode):
        out.bytes, out.files = st.st_size, 1
        return out
    stack = [_lp(path)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            out.errors += 1
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if not _entry_dir(e) or (skip_k and _key(e.path) in skip_k):
                            continue
                        out.newest = max(out.newest, e.stat(follow_symlinks=False).st_mtime)
                        stack.append(e.path)
                    else:
                        s = e.stat(follow_symlinks=False)
                        if _st_link(s):
                            continue
                        out.bytes += s.st_size
                        out.files += 1
                        out.newest = max(out.newest, s.st_mtime)
                except OSError:
                    out.errors += 1
    return out


def is_profile(d) -> bool:
    return any(os.path.isdir(_lp(Path(d) / z)) for z in (".fingerprint", "deps"))


def profiles(target) -> list[Path]:
    """Profile folders of a target dir (debug, release, custom ones), also those under a target-triple folder."""
    out = []
    for c in _subdirs(target):
        if is_profile(c):
            out.append(c)
        else:
            out.extend(cc for cc in _subdirs(c) if is_profile(cc))
    return out


def _zone_entries(profile) -> list[str]:
    out = []
    for z in TRIM_ZONES:
        zp = Path(profile) / z
        if is_link(zp):
            continue
        try:
            with os.scandir(_lp(zp)) as it:
                out.extend(e.path for e in it if not e.is_symlink())
        except OSError:
            pass
    return out


def measure_target(target, cutoff: float, skip=()) -> dict:
    """Size of a target dir, per profile, and how much of each profile's deps/build/.fingerprint/incremental is older
    than `cutoff` (by the newest mtime inside each entry: an entry is stale only if nothing in it changed since)."""
    target = Path(target)
    profs = profiles(target)
    rest = walk_size(target, skip=[*profs, *skip])
    total = Size().add(rest)
    rows = []
    for pr in profs:
        zones = [pr / z for z in TRIM_ZONES]
        size = walk_size(pr, skip=[*zones, *skip])
        stale_b = stale_n = 0
        for e in _zone_entries(pr):
            if any(inside(e, s) for s in skip):
                continue
            s = walk_size(e)
            size.add(s)
            if s.newest and s.newest < cutoff:
                stale_b += s.bytes
                stale_n += 1
        total.add(size)
        rows.append({"name": os.path.relpath(pr, target).replace("\\", "/"), "path": str(pr), "bytes": size.bytes,
                     "stale_bytes": stale_b, "stale_items": stale_n})
    return {"bytes": total.bytes, "files": total.files, "newest": total.newest, "errors": total.errors, "profiles": rows,
            "stale_bytes": sum(r["stale_bytes"] for r in rows)}


# ----------------------------------------------------------------------------- the project's locations

def gate_env(p: core.Project) -> dict[str, str]:
    env = (p.cfg.get("gates") or {}).get("env") or {}
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def _abs(s: str, base: Path) -> Path:
    q = Path(os.path.expandvars(os.path.expanduser(s.strip())))
    return Path(os.path.normpath(q if q.is_absolute() else base / q))


def worktrees_dir(p: core.Project) -> Path | None:
    w = (p.cfg.get("repo") or {}).get("worktrees")
    return Path(os.path.normpath(str(w))) if w else None


def configured_targets(p: core.Project, cfg: dict) -> list[Path]:
    """CARGO_TARGET_DIR from [gates.env] and [hygiene] target_dirs (relative ones are taken from the repository)."""
    out = []
    env = gate_env(p).get("CARGO_TARGET_DIR", "").strip()
    if env:
        out.append(_abs(env, p.repo))
    out += [_abs(t, p.repo) for t in cfg.get("target_dirs", [])]
    return out


def locations(p: core.Project, cfg: dict) -> list[Path]:
    """Where anything may be deleted at all: the configured target dirs and the folders next to them, the repository
    and the folder next to it, the worktrees folder and the folder holding it, and the project's data folder."""
    out = [p.repo, p.repo.parent, p.dir]
    w = worktrees_dir(p)
    if w:
        out += [w, w.parent]
    for t in configured_targets(p, cfg):
        out += [t, t.parent]
    return out


def has_marker(d) -> bool:
    return any(os.path.isfile(_lp(Path(d) / m)) for m in TARGET_MARKERS)


def looks_like_target(d) -> bool:
    return has_marker(d) or Path(d).name.lower().startswith("target")


def _task_of(name: str, task_ids: set[str]) -> str | None:
    """`target-017` -> "017": the task a per-task target dir belongs to (a task id, or a name starting with a digit)."""
    m = re.fullmatch(r"target[-_](.+)", name, re.I)
    if not m:
        return None
    sfx = m.group(1)
    return sfx if sfx in task_ids or re.match(r"\d", sfx) else None


def git_worktrees(p: core.Project) -> list[dict] | None:
    """`git worktree list --porcelain` as [{path, branch (short name or None), detached, locked, prunable}]."""
    code, out = core.git(["worktree", "list", "--porcelain"], p.repo)
    if code != 0:
        return None
    rows, cur = [], None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:].strip(), "branch": None, "detached": False, "locked": False, "prunable": False}
        elif cur is None:
            continue
        elif line.startswith("branch "):
            cur["branch"] = line[7:].strip().removeprefix("refs/heads/")
        elif line.strip() == "detached":
            cur["detached"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
        elif not line.strip():
            rows.append(cur)
            cur = None
    return rows


def _task_ids(p: core.Project, state: dict) -> set[str]:
    ids = set(state.get("tasks", {}))
    try:
        ids |= {t.id for t in core.load_tasks(p)}
    except OSError:
        pass
    return ids


def discover(p: core.Project, cfg: dict, state: dict) -> list[dict]:
    """Every cargo target dir the project uses: configured ones, <repo>/target, <worktree>/target, and target*
    folders (with a cargo marker) next to the repository, the worktrees folder and the configured target dirs, and
    the dashboard build's <data>/build/src/target."""
    found: dict[str, dict] = {}
    ids = _task_ids(p, state)

    def add(path: Path, kind: str) -> None:
        k = _key(path)
        if k in found or not os.path.isdir(_lp(path)) or is_link(path):
            return
        found[k] = {"path": str(path), "kind": kind, "task": _task_of(path.name, ids) if kind == "sibling" else None}

    configured = configured_targets(p, cfg)
    for t in configured:
        add(t, "configured")
    add(p.repo / "target", "repo")
    add(p.dir / "build" / "src" / "target", "build")
    w = worktrees_dir(p)
    if w:
        for c in _subdirs(w):
            add(c / "target", "worktree")
    parents = [p.repo.parent] + ([w.parent] if w else []) + [t.parent for t in configured]
    for par in dict.fromkeys(parents):
        for c in _subdirs(par):
            if c.name.lower().startswith("target") and has_marker(c):
                add(c, "sibling")
    return list(found.values())


def refuse_target(p: core.Project, cfg: dict, target, path=None) -> str | None:
    """Why `path` (default: the whole `target`) must not be deleted, or None when it may be. Checked again right
    before every deletion."""
    target = Path(_plain(target))
    path = Path(_plain(path)) if path is not None else target
    if not any(inside(target, loc) for loc in locations(p, cfg)):
        return f"{target} is outside the project's configured locations"
    if not inside(path, target):
        return f"{path} is not inside the target dir {target}"
    w = worktrees_dir(p)
    for loc in (p.repo, p.repo.parent, p.dir, *([w, w.parent] if w else []), *[t.parent for t in configured_targets(p, cfg)]):
        if _key(target) == _key(loc) or inside(loc, target):
            return f"{target} is (or holds) one of the project's own folders"
    if not looks_like_target(target):
        return f"{target} is not clearly a cargo target dir (no CACHEDIR.TAG or .rustc_info.json, not named target*)"
    if any(os.path.exists(_lp(target / m)) for m in SOURCE_MARKERS):
        return f"{target} holds sources ({' or '.join(SOURCE_MARKERS)}), not only a build cache"
    if not os.path.isdir(_lp(target)):
        return f"{target} does not exist"
    if _links_between(target, path):
        return f"{path} is or goes through a symlink/junction"
    return None


# ----------------------------------------------------------------------------- processes

def list_processes() -> list[dict] | None:
    """Running processes as [{pid, ppid, name, exe, cmdline, cwd, target_env}] (unknown fields None), or None when
    they can't be listed. psutil if installed, else PowerShell Get-CimInstance on Windows, else /proc."""
    try:
        import psutil  # optional
    except ImportError:
        psutil = None
    try:
        if psutil is not None:
            out = []
            for pr in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "cwd"], ad_value=None):
                i = pr.info
                env = None
                if Path(str(i.get("name") or "")).stem.lower() in CARGO_NAMES:
                    try:
                        env = pr.environ().get("CARGO_TARGET_DIR")
                    except Exception:
                        env = None
                out.append({"pid": i.get("pid"), "ppid": i.get("ppid"), "name": i.get("name"), "exe": i.get("exe"),
                            "cmdline": " ".join(i["cmdline"]) if i.get("cmdline") else None, "cwd": i.get("cwd"), "target_env": env})
            return out
        if os.name == "nt":
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:
                return None
            rows = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
            rows = rows if isinstance(rows, list) else [rows]
            return [{"pid": x.get("ProcessId"), "ppid": x.get("ParentProcessId"), "name": x.get("Name"), "exe": x.get("ExecutablePath"),
                     "cmdline": x.get("CommandLine"), "cwd": None, "target_env": None} for x in rows if isinstance(x, dict)]
        if os.path.isdir("/proc"):
            out = []
            for d in os.listdir("/proc"):
                if not d.isdigit():
                    continue
                def rd(f, link=False):
                    try:
                        return os.readlink(f"/proc/{d}/{f}") if link else Path(f"/proc/{d}/{f}").read_bytes()
                    except OSError:
                        return None
                cmd = rd("cmdline")
                comm = rd("comm")
                st = rd("stat")
                try:
                    ppid = int(st.rsplit(b")", 1)[1].split()[1]) if st else None
                except (IndexError, ValueError):
                    ppid = None
                out.append({"pid": int(d), "ppid": ppid, "name": comm.decode(errors="replace").strip() if comm else None, "exe": rd("exe", True),
                            "cmdline": cmd.replace(b"\0", b" ").decode(errors="replace").strip() if cmd else None,
                            "cwd": rd("cwd", True), "target_env": None})
            return out
    except Exception:
        return None
    return None


def _norm_text(s: str) -> str:
    s = _plain(s)
    return s.replace("/", "\\").lower() if os.name == "nt" else s


def procs_using(paths, procs: list[dict] | None) -> list[str] | None:
    """Processes that use any of `paths`: a cargo/rustc/rustup process whose command line, folder or CARGO_TARGET_DIR
    is inside one of them (or whose folder can't be told), or any program running from inside one (a test binary).
    A cargo whose folder is unknown (Windows without psutil) counts as using them unless a rustc/rustdoc below it
    shows by its --out-dir that it builds somewhere else. None when the process list is unknown: then don't delete."""
    if procs is None:
        return None
    roots = [Path(_plain(x)) for x in paths]
    names_root = lambda cmd: any(_norm_text(str(r)).rstrip("\\/") in _norm_text(cmd) for r in roots)
    kids: dict = {}
    for pr in procs:
        if pr.get("ppid") is not None and pr.get("ppid") != pr.get("pid"):
            kids.setdefault(pr.get("ppid"), []).append(pr)

    def builds_elsewhere(pid) -> bool:
        stack, seen, elsewhere = [pid], set(), False
        while stack:
            for c in kids.get(stack.pop(), []):
                if c.get("pid") in seen:
                    continue
                seen.add(c.get("pid"))
                stack.append(c.get("pid"))
                cmd = c.get("cmdline")
                if Path(str(c.get("name") or "")).stem.lower() in TOOLS_WITH_PATHS and cmd and "--out-dir" in cmd:
                    if names_root(cmd):
                        return False
                    elsewhere = True
        return elsewhere

    hits = []
    for pr in procs:
        name = Path(str(pr.get("name") or "")).stem.lower()
        label = f"{pr.get('name') or '?'} (pid {pr.get('pid')})"
        exe, cwd, cmd, env = pr.get("exe"), pr.get("cwd"), pr.get("cmdline"), pr.get("target_env")
        if exe and any(inside(exe, r) for r in roots):
            hits.append(f"{label} runs from inside the folder")
            continue
        if name not in CARGO_NAMES:
            continue
        if (cwd and any(inside(cwd, r) for r in roots)) or (env and any(inside(env, r) for r in roots)):
            hits.append(f"{label} works in the folder")
        elif cmd and names_root(cmd):
            hits.append(f"{label} names the folder on its command line")
        elif cmd is None or (name not in TOOLS_WITH_PATHS and cwd is None and env is None and not builds_elsewhere(pr.get("pid"))):
            hits.append(f"{label} is running and its folder can't be checked")
    return hits


# ----------------------------------------------------------------------------- deleting

def _chmod_w(lp: str) -> None:
    try:
        os.chmod(lp, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(lp) else 0))
    except OSError:
        pass


def delete_path(path) -> tuple[int, int]:
    """Delete a file or folder tree; returns (bytes freed, errors). Read-only files are made writable, long paths
    work, and a link/junction inside is removed itself, never followed."""
    lp = _lp(path)
    try:
        st = os.lstat(lp)
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return 0, 1
    attrs = getattr(st, "st_file_attributes", 0)
    if _st_link(st):
        try:
            if os.name == "nt" and attrs & 0x10:  # a junction or directory symlink: remove the link only
                os.rmdir(lp)
            else:
                os.unlink(lp)
            return 0, 0
        except OSError:
            return 0, 1
    if stat.S_ISDIR(st.st_mode):
        freed = errs = 0
        try:
            with os.scandir(lp) as it:
                entries = [e.path for e in it]
        except OSError:
            _chmod_w(lp)
            try:
                with os.scandir(lp) as it:
                    entries = [e.path for e in it]
            except OSError:
                return 0, 1
        for e in entries:
            f, er = delete_path(e)
            freed, errs = freed + f, errs + er
        for attempt in (0, 1):
            try:
                os.rmdir(lp)
                break
            except OSError:
                if attempt:
                    errs += 1
                else:
                    _chmod_w(lp)
        return freed, errs
    for attempt in (0, 1):
        try:
            os.unlink(lp)
            return st.st_size, 0
        except FileNotFoundError:
            return 0, 0
        except OSError:
            if attempt:
                return 0, 1
            _chmod_w(lp)
    return 0, 1


def trim_stale(target, cutoff: float, check=None) -> tuple[int, int, int]:
    """Delete the entries of every profile's deps/build/.fingerprint/incremental whose newest mtime is older than
    `cutoff` (cargo rebuilds what it needs). `check(path)` -> reason refuses one entry. Returns (bytes, items, errors)."""
    freed = items = errs = 0
    for pr in profiles(target):
        for e in _zone_entries(pr):
            s = walk_size(e)
            if not s.newest or s.newest >= cutoff:
                continue
            if check and check(e):
                errs += 1
                continue
            f, er = delete_path(e)
            freed, errs, items = freed + f, errs + er, items + 1
    return freed, items, errs


def trim_log(f: Path, cap: int) -> int:
    """Keep the newest half of a log file over `cap` bytes (cut at a line start). Returns bytes freed."""
    try:
        size = f.stat().st_size
    except OSError:
        return 0
    if size <= cap:
        return 0
    keep = max(1, cap // 2)
    with f.open("rb") as fh:
        fh.seek(size - keep)
        data = fh.read()
    nl = data.find(b"\n")
    data = data[nl + 1:] if 0 <= nl < len(data) - 1 else data
    head = f"[{dt.datetime.now():%m-%d %H:%M:%S}] disk hygiene: older lines removed ({fmt(size - len(data))})\n".encode()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(head + data)
    core.replace_file(tmp, f)
    return max(0, size - len(head) - len(data))


# ----------------------------------------------------------------------------- report and plan

def _active_tasks(state: dict) -> set[str]:
    return {k for k, e in state.get("tasks", {}).items() if e.get("status") in ACTIVE}


def _dependents_open(p: core.Project, state: dict) -> set[str]:
    """Task ids some not-done task depends on (their auto/* branch is still a starting point)."""
    try:
        tasks = core.load_tasks(p)
    except OSError:
        return set()
    out = set()
    for t in tasks:
        if state.get("tasks", {}).get(t.id, {}).get("status") != "done":
            out |= set(t.depends)
    return out


def _uses_cargo(p: core.Project) -> bool:
    g = p.cfg.get("gates") or {}
    cmds = [str(c) for k in ("quick", "final") for c in (g.get(k) or [])]
    return any("cargo" in c for c in cmds) or "CARGO_TARGET_DIR" in gate_env(p) or (p.repo / "Cargo.toml").exists()


def advice(p: core.Project, worktree_count: int) -> list[str]:
    """Settings in [gates.env] that make build caches grow; only reported, project.toml is never edited here."""
    if not _uses_cargo(p):
        return []
    env = gate_env(p)
    off = lambda v: str(v).strip().lower() in ("0", "false", "none")
    out = []
    if not off(env.get("CARGO_INCREMENTAL", "")):
        out.append("CARGO_INCREMENTAL is not 0 in [gates.env]: incremental caches (debug/incremental) grow quickly "
                   "and help little for one-off checks. Consider CARGO_INCREMENTAL = \"0\".")
    if not (off(env.get("CARGO_PROFILE_DEV_DEBUG", "")) and off(env.get("CARGO_PROFILE_TEST_DEBUG", ""))):
        out.append("Debug info is not disabled for checks: CARGO_PROFILE_DEV_DEBUG = \"0\" and "
                   "CARGO_PROFILE_TEST_DEBUG = \"0\" in [gates.env] make test binaries and .pdb files much smaller.")
    if env.get("CARGO_TARGET_DIR") and worktree_count >= 3:
        out.append(f"One target dir ({env['CARGO_TARGET_DIR']}) is shared by {worktree_count} worktrees on different "
                   "branches: each one's sources produce new build hashes, so stale artifacts pile up (cache churn). "
                   "Keep stale_days low, or give long-lived worktrees their own target.")
    return out


def measure(p: core.Project, cfg: dict | None = None, now: float | None = None) -> dict:
    """Walk everything the project uses on disk and work out the cleanup plan (nothing is deleted). Slow on big
    target dirs: call it off the dashboard's request threads (see `report`)."""
    t0 = time.time()
    cfg = cfg or core.hygiene_cfg(p)
    now = now or time.time()
    cutoff = now - cfg["stale_days"] * 86400
    state = core.Store(p).load()
    registered = git_worktrees(p)
    targets = discover(p, cfg, state)
    w = worktrees_dir(p)
    roots = [t["path"] for t in targets] + ([str(w)] if w and os.path.isdir(_lp(w)) else []) + [str(p.dir)]
    nested = lambda r: [o for o in roots if _key(o) != _key(r) and inside(o, r)]
    for t in targets:
        t.update(measure_target(t["path"], cutoff, skip=nested(t["path"])))
        t["over"] = t["bytes"] > cfg["max_target_gb"] * GB
    wts = {"path": str(w) if w else None, "bytes": 0, "items": []}
    reg_by_key = {_key(r["path"]): r for r in registered or []}
    if w and os.path.isdir(_lp(w)):
        wts["bytes"] = walk_size(w, skip=[*_subdirs(w), *nested(str(w))]).bytes  # loose files
        for c in _subdirs(w):
            s = walk_size(c, skip=nested(str(c)))
            inner = sum(t["bytes"] for t in targets if inside(t["path"], c))
            r = reg_by_key.get(_key(c))
            wts["items"].append({"name": c.name, "path": str(c), "bytes": s.bytes, "target_bytes": inner, "newest": s.newest,
                                 "registered": r is not None if registered is not None else None,
                                 "branch": r["branch"] if r else None, "detached": bool(r and r["detached"]),
                                 "locked": bool(r and r["locked"])})
            wts["bytes"] += s.bytes
    parts = {}
    for name in ("runs", "reports", "build"):
        d = p.dir / name
        parts[name] = walk_size(d, skip=nested(str(d))).bytes if d.is_dir() else 0
    rest = walk_size(p.dir, skip=[p.dir / n for n in parts] + nested(str(p.dir))).bytes
    parts["logs"] = sum(walk_size(p.dir / f).bytes for f in ("agentdesk.log", "runner.log") if (p.dir / f).is_file())
    parts["other"] = max(0, rest - parts["logs"])
    data = {"path": str(p.dir), "bytes": sum(parts.values()), "parts": parts}
    total = sum(t["bytes"] for t in targets) + wts["bytes"] + data["bytes"]
    rep = {"at": dt.datetime.fromtimestamp(now).isoformat(timespec="seconds"), "ts": now, "took_s": 0.0,
           "cfg": {k: v for k, v in cfg.items() if k != "errors"}, "errors": list(cfg.get("errors", [])),
           "targets": targets, "worktrees": wts, "data": data, "total": total,
           "over_total": total > cfg["max_total_gb"] * GB, "advice": advice(p, len([i for i in wts["items"] if i["name"] != core.COWORK]))}
    rep["warnings"] = [f"{Path(t['path']).name} is {fmt(t['bytes'])} (limit {cfg['max_target_gb']:g} GB per target dir)"
                       for t in targets if t["over"]]
    if rep["over_total"]:
        rep["warnings"].append(f"The project uses {fmt(total)} in all (limit {cfg['max_total_gb']:g} GB)")
    rep["actions"] = plan(p, cfg, rep, state, registered, cutoff)
    rep["reclaimable"] = sum(a["bytes"] for a in rep["actions"])
    rep["took_s"] = round(time.time() - t0, 2)
    return rep


def _merged_fresh(p: core.Project, branch: str, base: str) -> bool | None:
    code, _ = core.git(["merge-base", "--is-ancestor", branch, base], p.repo)
    return True if code == 0 else False if code == 1 else None


def _heads(p: core.Project) -> set[str] | None:
    code, out = core.git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], p.repo)
    return set(out.split()) if code == 0 else None


def plan(p: core.Project, cfg: dict, rep: dict, state: dict, registered: list[dict] | None, cutoff: float) -> list[dict]:
    """What a cleanup would do, from a measurement: [{id, kind, path, bytes, why}]. Re-checked before applying."""
    acts: list[dict] = []
    max_t, base = cfg["max_target_gb"] * GB, (p.cfg.get("repo") or {}).get("base", "main")
    active = _active_tasks(state)
    ids = _task_ids(p, state)
    reg_paths = [r["path"] for r in registered or []]
    live_names = {Path(x).name for x in reg_paths}
    for t in rep["targets"]:
        if refuse_target(p, cfg, t["path"]):
            continue
        tid = t.get("task")
        if t["kind"] == "sibling" and tid and registered is not None:
            live = any(n == tid or n.startswith(tid + "-") for n in live_names)
            open_task = tid in ids and state.get("tasks", {}).get(tid, {}).get("status") != "done"
            if not live and not open_task and tid not in active and t["newest"] and t["newest"] < cutoff:
                acts.append({"id": f"orphan:{_key(t['path'])}", "kind": "orphan", "path": t["path"], "bytes": t["bytes"],
                             "why": f"per-task target dir of {tid}: no worktree or open task uses it, untouched for "
                                    f"{cfg['stale_days']:g}+ days"})
                continue
        if not (t["over"] or rep["over_total"]):
            continue
        if t["stale_bytes"]:
            acts.append({"id": f"trim:{_key(t['path'])}", "kind": "trim", "path": t["path"], "bytes": t["stale_bytes"],
                         "why": f"build artifacts not touched for {cfg['stale_days']:g}+ days (deps, build, .fingerprint, incremental)"})
        left = t["bytes"] - t["stale_bytes"]
        for pr in sorted(t["profiles"], key=lambda r: r["bytes"] - r["stale_bytes"], reverse=True):
            if left <= max_t:
                break
            b = pr["bytes"] - pr["stale_bytes"]
            acts.append({"id": f"profile:{_key(pr['path'])}", "kind": "profile", "path": pr["path"], "bytes": b,
                         "target": t["path"], "why": f"{Path(t['path']).name} stays over {cfg['max_target_gb']:g} GB after "
                                                     f"trimming: remove the whole {pr['name']} build cache (rebuilt on next build)"})
            left -= b
    heads = _heads(p) if registered is not None else None
    wt_branches = {r["branch"] for r in registered or [] if r["branch"]}
    removed_wt_branches = set()
    deps_open = _dependents_open(p, state)
    w = worktrees_dir(p)
    for it in rep["worktrees"]["items"]:
        if not it["registered"] or it["locked"] or it["name"] == core.COWORK or it["name"] in active or heads is None:
            continue
        e = state.get("tasks", {}).get(it["name"], {})
        br = it["branch"]
        why = None
        if br and e.get("status") == "done" and br == f"auto/{it['name']}" and _merged_fresh(p, br, base):
            why = f"task {it['name']} is done and {br} is merged into {base}"
        elif it["detached"] or (br and br not in heads):
            why = f"its branch is gone ({'detached HEAD' if it['detached'] else br + ' no longer exists'})"
        if why and w and inside(it["path"], w):
            acts.append({"id": f"worktree:{_key(it['path'])}", "kind": "worktree", "path": it["path"],
                         "bytes": it["bytes"] + it["target_bytes"], "why": why + "; git worktree remove --force, then prune"})
            if br:
                removed_wt_branches.add(br)
    if heads is not None:
        for br in sorted(heads):
            if not br.startswith("auto/") or br == f"auto/{core.COWORK}" or br == base:
                continue
            if (br in wt_branches and br not in removed_wt_branches) or br[5:] in deps_open or br[5:] in active:
                continue
            if _merged_fresh(p, br, base):
                acts.append({"id": f"branch:{br}", "kind": "branch", "path": br, "bytes": 0,
                             "why": f"merged into {base}" + (" (its worktree is removed first)" if br in removed_wt_branches else "")})
    acts += _log_actions(p, cfg, state, cutoff)
    acts.sort(key=lambda a: ORDER.index(a["kind"]))
    return acts


def _log_actions(p: core.Project, cfg: dict, state: dict, cutoff: float) -> list[dict]:
    out = []
    runs = p.dir / "runs"
    if runs.is_dir():
        files = sorted((f for f in runs.glob("*.jsonl") if f.is_file()), key=lambda f: f.stat().st_mtime)
        total, cap, b, n = sum(f.stat().st_size for f in files), cfg["max_runs_mb"] * MB, 0, 0
        active = _active_tasks(state)
        for f in files:
            if total - b <= cap:
                break
            if f.stat().st_mtime >= cutoff or f.stem in active:
                continue
            b, n = b + f.stat().st_size, n + 1
        if n:
            out.append({"id": "logs:runs", "kind": "logs", "path": str(runs), "bytes": b,
                        "why": f"run timelines over {cfg['max_runs_mb']:g} MB: remove the {n} oldest (older than {cfg['stale_days']:g} days)"})
    for name in ("agentdesk.log", "runner.log"):
        f = p.dir / name
        cap = int(cfg["max_log_mb"] * MB)
        if f.is_file() and f.stat().st_size > cap:
            out.append({"id": f"logs:{name}", "kind": "logs", "path": str(f), "bytes": f.stat().st_size - cap // 2,
                        "why": f"{name} is over {cfg['max_log_mb']:g} MB: keep its newest {fmt(cap // 2)}"})
    return out


# ----------------------------------------------------------------------------- applying

def default_run_active(p: core.Project) -> bool:
    try:
        pid = int(p.path("runner.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return core.pid_alive(pid)


def apply(p: core.Project, ids: list[str] | None = None, cfg: dict | None = None, run_active=None, procs=None,
          log=None, now: float | None = None) -> dict:
    """Carry out the cleanup: measures again, then does the planned actions whose id is in `ids` (None = all), each
    checked again right before it runs. `procs`: a function returning the process list (default list_processes).
    Every action and the bytes it freed go to the project's agentdesk.log. Returns {freed, done, skipped}."""
    cfg = cfg or core.hygiene_cfg(p)
    run_active = run_active or default_run_active
    procs = procs or list_processes
    store = core.Store(p)
    log = log or store.log
    now = now or time.time()
    cutoff = now - cfg["stale_days"] * 86400
    res = {"freed": 0, "done": [], "skipped": []}

    def skip(a: dict, why: str) -> None:
        res["skipped"].append({**a, "reason": why})
        log(f"disk hygiene: skipped {a['kind']} {a['path']}: {why}")

    def did(a: dict, freed: int, text: str) -> None:
        res["freed"] += freed
        res["done"].append({**a, "freed": freed})
        log(f"disk hygiene: {text} (freed {fmt(freed)})")

    if not cfg["enabled"]:
        res["skipped"].append({"kind": "all", "path": str(p.dir), "reason": "disk hygiene is turned off ([hygiene] enabled = false)"})
        return res
    if run_active(p):
        res["skipped"].append({"kind": "all", "path": str(p.dir), "reason": "a run is active for this project"})
        log("disk hygiene: nothing done, a run is active for this project")
        return res
    rep = measure(p, cfg, now)
    wanted = None if ids is None else set(ids)
    for missing in sorted((wanted or set()) - {a["id"] for a in rep["actions"]}):
        res["skipped"].append({"id": missing, "kind": missing.split(":")[0], "path": missing.split(":", 1)[-1],
                               "reason": "no longer needed (measured again)"})
    base = (p.cfg.get("repo") or {}).get("base", "main")

    def busy(path) -> str | None:
        hits = procs_using([path], procs())
        if hits is None:
            return "can't tell which processes use it (process list unavailable)"
        return "; ".join(hits[:3]) if hits else None

    for a in rep["actions"]:
        if wanted is not None and a["id"] not in wanted:
            continue
        if run_active(p):
            skip(a, "a run started for this project")
            continue
        try:
            k = a["kind"]
            if k in ("trim", "orphan", "profile"):
                target = a.get("target") or a["path"]
                why = refuse_target(p, cfg, target, a["path"]) or busy(target)
                if why:
                    skip(a, why)
                    continue
                check = lambda e, t=target: refuse_target(p, cfg, t, e)
                if k == "trim":
                    f, n, err = trim_stale(target, cutoff, check)
                    did(a, f, f"trimmed {n} stale build artifacts in {target}" + (f", {err} could not be deleted" if err else ""))
                elif k == "orphan":
                    f, err = delete_path(a["path"])
                    did(a, f, f"removed orphan target dir {a['path']}" + (f", {err} entries could not be deleted" if err else ""))
                else:
                    left = measure_target(target, cutoff)["bytes"]
                    if left <= cfg["max_target_gb"] * GB:
                        skip(a, f"{Path(target).name} is under the limit now ({fmt(left)})")
                        continue
                    f, err = delete_path(a["path"])
                    did(a, f, f"removed build cache {a['path']} ({Path(target).name} was {fmt(left)})"
                              + (f", {err} entries could not be deleted" if err else ""))
            elif k == "worktree":
                why = busy(a["path"]) or _worktree_refusal(p, a["path"])
                if why:
                    skip(a, why)
                    continue
                f = 0
                inner = Path(a["path"]) / "target"
                if os.path.isdir(_lp(inner)) and not refuse_target(p, cfg, inner):
                    f += delete_path(inner)[0]  # git can't remove very long paths on Windows; the cache goes first
                f += walk_size(a["path"]).bytes
                code, out = core.git(["worktree", "remove", "--force", a["path"]], p.repo)
                core.git(["worktree", "prune"], p.repo)
                if code != 0:
                    skip(a, f"git worktree remove failed: {out.strip()[:300]}")
                    continue
                did(a, f, f"removed worktree {a['path']} ({a['why']})")
            elif k == "branch":
                if a["path"] in {r["branch"] for r in git_worktrees(p) or [] if r["branch"]}:
                    skip(a, "it is checked out in a worktree")
                    continue
                if _merged_fresh(p, a["path"], base) is not True:
                    skip(a, f"not merged into {base}")
                    continue
                code, out = core.git(["branch", "-D", a["path"]], p.repo)  # merged into base: checked just above
                if code != 0:
                    skip(a, f"git branch failed: {out.strip()[:300]}")
                    continue
                did(a, 0, f"deleted branch {a['path']} (merged into {base})")
            elif k == "logs":
                f = _apply_logs(p, cfg, a, state=core.Store(p).load(), cutoff=cutoff)
                did(a, f, f"trimmed {a['path']}")
        except OSError as e:
            skip(a, f"{type(e).__name__}: {e}")
    with _LOCK:
        _CACHE.pop(p.slug, None)
    return res


def _worktree_refusal(p: core.Project, path: str) -> str | None:
    w = worktrees_dir(p)
    if not w or not inside(path, w) or _key(Path(path).parent) != _key(w):
        return "not a folder of the project's worktrees folder"
    if Path(path).name == core.COWORK:
        return "the Coworker's worktree is kept"
    if not any(_key(r["path"]) == _key(path) for r in git_worktrees(p) or []):
        return "not a registered git worktree"
    code, out = core.git(["status", "--porcelain"], Path(path))
    if code != 0:
        return f"git status failed: {out.strip()[:200]}"
    if out.strip():
        return "it has uncommitted changes or untracked files"
    # ignored files (.env, local notes ...) are lost by `worktree remove --force`; build caches are fine to lose
    code, out = core.git(["status", "--porcelain", "--ignored"], Path(path))
    if code != 0:
        return f"git status failed: {out.strip()[:200]}"
    for line in out.splitlines():
        if line.startswith("!! ") and not re.match(r"(?:target[^/]*|node_modules|__pycache__|\.pytest_cache|\.code-graph)/?($|/)", line[3:].strip().strip('"')):
            return f"it holds ignored files ({line[3:].strip()}) that would be lost"
    # commits that no branch contains (detached HEAD after a rebase / checkout <sha>) would become unreachable
    code, out = core.git(["for-each-ref", "--contains", "HEAD", "refs/heads", "refs/remotes"], Path(path))
    if code != 0:
        return f"git for-each-ref failed: {out.strip()[:200]}"
    if not out.strip():
        return "its HEAD is on no branch (commits would be lost)"
    return None


def _apply_logs(p: core.Project, cfg: dict, a: dict, state: dict, cutoff: float) -> int:
    if a["id"] == "logs:runs":
        runs = p.dir / "runs"
        files = sorted((f for f in runs.glob("*.jsonl") if f.is_file()), key=lambda f: f.stat().st_mtime)
        total, cap, freed = sum(f.stat().st_size for f in files), cfg["max_runs_mb"] * MB, 0
        active = _active_tasks(state)
        for f in files:
            if total - freed <= cap:
                break
            if f.stat().st_mtime >= cutoff or f.stem in active:
                continue
            size = f.stat().st_size
            f.unlink()
            freed += size
        return freed
    f = Path(a["path"])
    if f.parent != p.dir or f.name not in ("agentdesk.log", "runner.log"):
        return 0
    return trim_log(f, int(cfg["max_log_mb"] * MB))


# ----------------------------------------------------------------------------- cache, background jobs, loop

_CACHE: dict[str, dict] = {}
_MEASURING: set[str] = set()
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_LOGGED: set[str] = set()


def cleaning(slug: str) -> bool:
    """A cleanup is running for this project (the auto-runner waits meanwhile)."""
    with _LOCK:
        return bool(_JOBS.get(slug, {}).get("running"))


def _measure_now(p: core.Project, log=None) -> dict | None:
    with _LOCK:
        if p.slug in _MEASURING:
            return None
        _MEASURING.add(p.slug)
    try:
        cfg = core.hygiene_cfg(p)
        _log_cfg_errors(p, cfg, log)
        rep = measure(p, cfg)
        with _LOCK:
            _CACHE[p.slug] = rep
        return rep
    except Exception as e:
        with _LOCK:
            _CACHE[p.slug] = {"ts": time.time(), "error": f"{type(e).__name__}: {e}"}
        if log:
            log(f"disk hygiene: {p.slug}: measuring failed: {type(e).__name__}: {e}")
        return None
    finally:
        with _LOCK:
            _MEASURING.discard(p.slug)


def _log_cfg_errors(p: core.Project, cfg: dict, log=None) -> None:
    for err in cfg.get("errors", []):
        key = f"{p.slug}|{err}"
        if key not in _LOGGED:
            _LOGGED.add(key)
            (log or core.Store(p).log)(f"disk hygiene: config: {err}")


def report(p: core.Project, refresh: bool = False, log=None) -> dict:
    """The cached report (never measures in the caller's thread): starts a measurement in the background when there
    is none, it is older than CACHE_TTL_S, or `refresh`. {report, measuring, job, enabled}."""
    cfg = core.hygiene_cfg(p)
    with _LOCK:
        hit = _CACHE.get(p.slug)
        going = p.slug in _MEASURING
    if cfg["enabled"] and not going and (refresh or hit is None or time.time() - hit.get("ts", 0) >= CACHE_TTL_S):
        threading.Thread(target=_measure_now, args=(p, log), daemon=True, name=f"disk-{p.slug}").start()
        going = True
    with _LOCK:
        job = dict(_JOBS.get(p.slug) or {})
    return {"enabled": cfg["enabled"], "auto": cfg["auto"], "errors": cfg["errors"], "report": hit, "measuring": going, "job": job}


def summary(slug: str) -> dict | None:
    """Total and warning from the cache only (for the project list)."""
    with _LOCK:
        r = _CACHE.get(slug)
    if not r or "total" not in r:
        return None
    return {"total": r["total"], "warn": bool(r["warnings"]), "reclaimable": r["reclaimable"]}


def start_clean(p: core.Project, ids: list[str] | None, run_active=None, log=None) -> dict:
    """Run `apply` in a background thread (the dashboard polls the job). One cleanup per project at a time."""
    with _LOCK:
        if _JOBS.get(p.slug, {}).get("running"):
            return {"ok": False, "message": "a cleanup is already running for this project"}
        _JOBS[p.slug] = {"running": True, "started": time.time(), "result": None, "error": None}

    def go() -> None:
        res, err = None, None
        try:
            res = apply(p, ids, run_active=run_active)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if log:
                log(f"disk hygiene: {p.slug}: cleanup failed: {err}")
        with _LOCK:
            _JOBS[p.slug] = {"running": False, "started": _JOBS[p.slug]["started"], "finished": time.time(), "result": res, "error": err}
        _measure_now(p, log)

    threading.Thread(target=go, daemon=True, name=f"disk-clean-{p.slug}").start()
    return {"ok": True}


def loop_pass(run_active=None, log=None, now: float | None = None) -> None:
    """One pass of the background job: measure each enabled project whose last measurement is older than its
    interval_min, and where [hygiene] auto = true, clean up what the plan suggests. Errors stay per project."""
    now = now or time.time()
    for p in core.projects():
        try:
            cfg = core.hygiene_cfg(p)
            _log_cfg_errors(p, cfg, log)
            if not cfg["enabled"]:
                continue
            with _LOCK:
                hit = _CACHE.get(p.slug)
            if hit and now - hit.get("ts", 0) < cfg["interval_min"] * 60:
                continue
            rep = _measure_now(p, log)
            if rep and cfg["auto"] and rep["actions"] and not cleaning(p.slug):
                core.Store(p).log(f"disk hygiene: auto cleanup of {len(rep['actions'])} item(s), about {fmt(rep['reclaimable'])}")
                with _LOCK:
                    _JOBS[p.slug] = {"running": True, "started": time.time(), "result": None, "error": None}
                try:
                    res = apply(p, [a["id"] for a in rep["actions"]], cfg, run_active=run_active)
                    err = None
                except Exception as e:
                    res, err = None, f"{type(e).__name__}: {e}"
                with _LOCK:
                    _JOBS[p.slug] = {"running": False, "finished": time.time(), "result": res, "error": err, "auto": True}
                _measure_now(p, log)
        except Exception as e:  # one broken project must not stop the others
            if log:
                log(f"disk hygiene: {p.slug}: {type(e).__name__}: {e}")
