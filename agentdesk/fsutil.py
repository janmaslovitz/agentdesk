"""Careful file-system helpers shared by disk hygiene (per project, agentdesk.hygiene) and the server-wide storage
budget (agentdesk.storage). Stdlib only, no import of core, so the gateway can use them.

The rules:
- walking uses os.scandir and never follows symlinks, junctions or other reparse points;
- deleting removes a link itself, never what it points to; `safe_delete` additionally refuses anything that is not
  inside the given root, or that is reached through a link below that root.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

GB = 1024 ** 3
MB = 1024 ** 2
REPARSE = 0x400                    # FILE_ATTRIBUTE_REPARSE_POINT: symlink, junction, mount point ...


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
    used: float = 0.0     # newest max(atime, mtime) (only filled by walk_size(..., atime=True))

    def add(self, o: "Size") -> "Size":
        self.bytes += o.bytes
        self.files += o.files
        self.newest = max(self.newest, o.newest)
        self.errors += o.errors
        self.used = max(self.used, o.used)
        return self


def walk_size(path, skip=(), atime: bool = False) -> Size:
    """Total size of the files under `path` with os.scandir, never following links/junctions; folders in `skip`
    (measured elsewhere) are left out. A plain file gives its own size. atime=True also fills `used` (the newest
    access or change time: what an LRU cache prune goes by)."""
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
    if atime:  # a folder's atime changes by merely listing it (also by this walk): folders count by mtime
        out.used = st.st_mtime if stat.S_ISDIR(st.st_mode) else max(st.st_mtime, st.st_atime)
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
                        s = e.stat(follow_symlinks=False)
                        out.newest = max(out.newest, s.st_mtime)
                        if atime:
                            out.used = max(out.used, s.st_mtime)
                        stack.append(e.path)
                    else:
                        s = e.stat(follow_symlinks=False)
                        if _st_link(s):
                            continue
                        out.bytes += s.st_size
                        out.files += 1
                        out.newest = max(out.newest, s.st_mtime)
                        if atime:
                            out.used = max(out.used, s.st_mtime, s.st_atime)
                except OSError:
                    out.errors += 1
    return out


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


def refuse_delete(root, path) -> str | None:
    """Why `path` must not be deleted as part of `root`, or None: it must be strictly inside `root` (never the root
    itself), and neither the root nor any folder down to the path may be a link."""
    if not inside(path, root) or _key(path) == _key(root):
        return f"{path} is not inside {root}"
    if _links_between(root, path):
        return f"{path} is or goes through a symlink/junction"
    return None


def safe_delete(root, path) -> tuple[int, int, str | None]:
    """delete_path() after refuse_delete(): (bytes freed, errors, refusal reason or None)."""
    why = refuse_delete(root, path)
    if why:
        return 0, 0, why
    f, e = delete_path(path)
    return f, e, None
