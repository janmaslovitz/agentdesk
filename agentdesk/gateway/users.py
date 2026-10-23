"""User store (`<server-home>/users.json`), password hashing and the audit log (`<server-home>/audit.log`).

Password hashes never leave this module: public() is what the rest of the gateway (and any output) sees.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .. import reliability

try:
    import fcntl  # POSIX
except ImportError:  # Windows
    fcntl = None
    import msvcrt

# scrypt cost: N=2^15, r=8, p=1 (32 MiB, ~0.1 s); stored with each hash so it can be raised later.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 15, 8, 1, 32
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
# Becomes a folder name and the OS account agentdesk-<username> (Linux user names: at most 32 characters).
USERNAME = re.compile(r"[a-z][a-z0-9_-]{0,21}")
ROLES = ("admin", "user")
ITEM = re.compile(r"(?!\.+$)[A-Za-z0-9_.-]{1,80}")  # project slugs / account names


# ----------------------------------------------------------------------------- durable files

def write_atomic(target: Path, text: str) -> None:
    """Like core.write_atomic: the file is always either the old or the new content (flushed before the rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    for i in range(40):  # Windows: the rename fails while someone else has the target open
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if i == 39:
                raise
            time.sleep(0.05)


def chmod_posix(path: Path, mode: int) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def harden_home(home: Path, per_user_dirs: bool = True) -> None:
    """POSIX: the server home and its secrets are for the gateway's OS user only (users.json, accounts.json,
    audit.log, token files 0600; users/<name> 0700)."""
    if per_user_dirs:
        chmod_posix(home, 0o700)
    for name in ("users.json", "accounts.json", "audit.log", "users.json.lock"):
        if (home / name).exists():
            chmod_posix(home / name, 0o600)
    for d in ("secret", "secrets", "instances", "logs"):
        if (home / d).is_dir():
            chmod_posix(home / d, 0o700)
            for f in (home / d).iterdir():
                if f.is_file():
                    chmod_posix(f, 0o600)
    for f in home.glob("*token*"):
        if f.is_file():
            chmod_posix(f, 0o600)
    if per_user_dirs and (home / "users").is_dir():
        for d in (home / "users").iterdir():
            if d.is_dir() and not d.is_symlink():
                chmod_posix(d, 0o700)


@contextmanager
def file_lock(path: Path):
    """An exclusive lock on `path` across processes (fcntl.flock on POSIX, msvcrt.locking on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # tries for ~10 s per call
                    break
                except OSError:
                    time.sleep(0.05)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                try:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    finally:
        os.close(fd)


# ----------------------------------------------------------------------------- passwords

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, n: int | None = None, r: int | None = None, p: int | None = None) -> str:
    """"scrypt$N$r$p$salt$hash" with a fresh random 16-byte salt."""
    n, r, p = n or SCRYPT_N, r or SCRYPT_R, p or SCRYPT_P
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_LEN, maxmem=256 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison of the derived key; False for anything malformed."""
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        want_b = base64.b64decode(want)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(want_b), maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, want_b)


_DUMMY: list[str] = []


def burn_time(password: str) -> None:
    """Spend as long as a real check, so a missing user name answers no faster than a wrong password."""
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(12)))
    verify_password(password, _DUMMY[0])


def check_password_rules(password: str) -> None:
    if not isinstance(password, str) or not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise ValueError(f"the password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")


# ----------------------------------------------------------------------------- audit

class Audit:
    """One JSON object per line: time, event, user, ip and details. Never passwords or hashes."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def log(self, event: str, user: str = "", ip: str = "", **details) -> None:
        rec = {"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, "user": user, "ip": ip}
        rec.update({k: v for k, v in details.items() if k not in ("password", "hash")})
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                reliability.rotate(self.path)
                new = not self.path.exists()
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
                if new:
                    chmod_posix(self.path, 0o600)
            except OSError:
                pass


# ----------------------------------------------------------------------------- users

def existing_projects(user_home: Path) -> set[str]:
    """Project slugs that exist in a user's home (`<home>/projects/<slug>/`)."""
    d = user_home / "projects"
    try:
        return {p.name for p in d.iterdir() if p.is_dir() and not p.is_symlink()}
    except OSError:
        return set()


class UserStore:
    """users.json: {"version": 1, "users": {name: {...}}}. Read from disk on every lookup (cheap; changes made with the
    CLI while the gateway runs take effect at once), written atomically under a lock."""

    def __init__(self, home: Path, audit: Audit | None = None):
        self.home = home
        self.path = home / "users.json"
        self.audit = audit or Audit(home / "audit.log")
        self.lock = threading.RLock()
        self._lockfile = home / "users.json.lock"

    @contextmanager
    def _locked(self):
        """Thread lock + cross-process file lock around load-modify-save (the CLI and the gateway both write)."""
        with self.lock:
            with file_lock(self._lockfile):
                yield

    # -- reading
    def _load(self) -> dict:
        for i in range(20):  # Windows: reading fails for a moment while a write renames the new file into place
            try:
                text = self.path.read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                return {"version": 1, "users": {}}
            except PermissionError as e:
                if i == 19:
                    raise RuntimeError(f"{self.path} can't be read ({e})") from e
                time.sleep(0.05)
            except OSError as e:
                raise RuntimeError(f"{self.path} can't be read ({e})") from e
        try:
            data = json.loads(text)
        except ValueError as e:
            # Never start over with an empty store (that would silently drop every account): refuse instead.
            raise RuntimeError(f"{self.path} is unreadable ({e}); fix or restore it") from e
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise RuntimeError(f"{self.path} has an unexpected layout")
        return data

    def _save(self, data: dict) -> None:
        write_atomic(self.path, json.dumps(data, indent=2, ensure_ascii=False))

    def user_home(self, name: str) -> Path:
        return self.home / "users" / name

    @staticmethod
    def public(name: str, u: dict) -> dict:
        """Everything about a user except the password hash."""
        return {"username": name, "role": u.get("role", "user"), "disabled": bool(u.get("disabled")),
                "created": u.get("created", ""), "home": u.get("home", ""), "projects": list(u.get("projects", [])),
                "accounts": list(u.get("accounts", [])), "sessions_after": float(u.get("sessions_after") or 0),
                "quota_gb": u.get("quota_gb")}

    def get(self, name: str) -> dict | None:
        u = self._load()["users"].get(name)
        return self.public(name, u) if u else None

    def list(self) -> list[dict]:
        return [self.public(n, u) for n, u in sorted(self._load()["users"].items())]

    def count(self) -> int:
        return len(self._load()["users"])

    def authenticate(self, name: str, password: str) -> dict | None:
        """The public user record when name + password are right and the user is enabled, else None.
        Takes about the same time whether or not the user exists."""
        u = self._load()["users"].get(name) if isinstance(name, str) else None
        if not u or not isinstance(password, str) or len(password) > MAX_PASSWORD:
            burn_time(password if isinstance(password, str) else "")
            return None
        ok = verify_password(password, str(u.get("password", "")))
        if not ok or u.get("disabled"):
            return None
        return self.public(name, u)

    # -- changes (each one is audited)
    def _change(self, name: str, fn, event: str, by: str = "", **details) -> dict:
        with self._locked():
            data = self._load()
            u = data["users"].get(name)
            if u is None:
                raise KeyError(f"no user {name!r}")
            fn(u, data)
            self._save(data)
        self.audit.log(event, user=name, by=by, **details)
        return self.public(name, u)

    def add(self, name: str, password: str, role: str = "user", by: str = "") -> dict:
        if not isinstance(name, str) or not USERNAME.fullmatch(name):
            raise ValueError("user names are 1-22 characters: a lowercase letter, then lowercase letters, digits, - or _")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        check_password_rules(password)
        hashed = hash_password(password)
        with self._locked():
            data = self._load()
            if name in data["users"]:
                raise ValueError(f"user {name!r} already exists")
            home = self.user_home(name)
            home.mkdir(parents=True, exist_ok=True)
            chmod_posix(home, 0o700)
            u = {"role": role, "password": hashed, "disabled": False,
                 "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "home": str(home), "projects": [], "accounts": [], "sessions_after": time.time()}  # cookies of an earlier same-named user stay dead
            data["users"][name] = u
            self._save(data)
        self.audit.log("user added", user=name, by=by, role=role)
        return self.public(name, u)

    def set_password(self, name: str, password: str, by: str = "") -> dict:
        check_password_rules(password)
        hashed = hash_password(password)

        def fn(u, _d):
            u["password"] = hashed
            u["sessions_after"] = time.time()  # logins made before the change end
        return self._change(name, fn, "password changed", by)

    def set_disabled(self, name: str, disabled: bool, by: str = "") -> dict:
        return self._change(name, lambda u, _d: u.__setitem__("disabled", bool(disabled)),
                            "user disabled" if disabled else "user enabled", by)

    def set_quota(self, name: str, quota_gb: float | None, by: str = "") -> dict:
        """The user's own disk quota in GB (0 = unlimited), or None for the server default (quota_gb)."""
        if quota_gb is not None:
            if isinstance(quota_gb, bool) or not isinstance(quota_gb, (int, float)) or not 0 <= float(quota_gb) <= 1e6:
                raise ValueError("the quota is a number of GB from 0 (no limit) up")
            quota_gb = float(quota_gb)

        def fn(u, _d):
            if quota_gb is None:
                u.pop("quota_gb", None)
            else:
                u["quota_gb"] = quota_gb
        return self._change(name, fn, "user quota", by, quota_gb="default" if quota_gb is None else quota_gb)

    def set_role(self, name: str, role: str, by: str = "") -> dict:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return self._change(name, lambda u, _d: u.__setitem__("role", role), "role changed", by, role=role)

    def assign(self, name: str, projects: list[str] | None = None, accounts: list[str] | None = None, by: str = "") -> dict:
        """Replace the user's assigned project slugs and/or account names (None leaves a list as it is)."""
        for v in (projects or []) + (accounts or []):
            if not isinstance(v, str) or not ITEM.fullmatch(v):
                raise ValueError(f"bad project or account name: {v!r}")

        def fn(u, _d):
            if projects is not None:
                have = existing_projects(Path(u.get("home") or self.user_home(name)))
                missing = [p for p in projects if p not in have]
                if missing:
                    raise ValueError(f"no such project in {name}'s home: {', '.join(missing)}")
                u["projects"] = sorted(set(projects))
            if accounts is not None:
                u["accounts"] = sorted(set(accounts))
        return self._change(name, fn, "assignments changed", by, projects=projects, accounts=accounts)

    def prune_projects(self, name: str, by: str = "") -> list[str]:
        """Drop assigned project slugs that no longer exist in the user's home; returns the dropped ones."""
        u = self.get(name)
        if u is None or set(u["projects"]) <= existing_projects(Path(u["home"] or self.user_home(name))):
            return []  # nothing to drop: no write, no audit line
        dropped: list[str] = []

        def fn(rec, _d):  # recomputed under the lock
            have = existing_projects(Path(rec.get("home") or self.user_home(name)))
            dropped.extend(p for p in rec.get("projects", []) if p not in have)
            rec["projects"] = [p for p in rec.get("projects", []) if p in have]
        self._change(name, fn, "assignments changed", by, dropped_projects=dropped)
        return dropped

    def remove(self, name: str, by: str = "", delete_data: bool = False) -> None:
        """Delete the account. The user's data folder is kept unless delete_data (only ever a folder under users/)."""
        with self._locked():
            data = self._load()
            if data["users"].pop(name, None) is None:
                raise KeyError(f"no user {name!r}")
            self._save(data)
        gone = False
        home = self.user_home(name)
        if delete_data and USERNAME.fullmatch(name) and home.is_dir() and not home.is_symlink():
            shutil.rmtree(home, ignore_errors=True)
            gone = not home.exists()
        self.audit.log("user removed", user=name, by=by, data_deleted=gone)
