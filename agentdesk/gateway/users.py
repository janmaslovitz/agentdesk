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
import threading
import time
from pathlib import Path

# scrypt cost: N=2^15, r=8, p=1 (32 MiB, ~0.1 s); stored with each hash so it can be raised later.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 15, 8, 1, 32
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
# Becomes a folder name and the OS account agentdesk-<username> (Linux user names: at most 32 characters).
USERNAME = re.compile(r"[a-z][a-z0-9_-]{0,21}")
ROLES = ("admin", "user")
ITEM = re.compile(r"[A-Za-z0-9_.-]{1,80}")  # project slugs / account names


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
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


# ----------------------------------------------------------------------------- users

class UserStore:
    """users.json: {"version": 1, "users": {name: {...}}}. Read from disk on every lookup (cheap; changes made with the
    CLI while the gateway runs take effect at once), written atomically under a lock."""

    def __init__(self, home: Path, audit: Audit | None = None):
        self.home = home
        self.path = home / "users.json"
        self.audit = audit or Audit(home / "audit.log")
        self.lock = threading.RLock()

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
                "accounts": list(u.get("accounts", [])), "sessions_after": float(u.get("sessions_after") or 0)}

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
        with self.lock:
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
        with self.lock:
            data = self._load()
            if name in data["users"]:
                raise ValueError(f"user {name!r} already exists")
            home = self.user_home(name)
            home.mkdir(parents=True, exist_ok=True)
            u = {"role": role, "password": hashed, "disabled": False,
                 "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "home": str(home), "projects": [], "accounts": []}
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
                u["projects"] = sorted(set(projects))
            if accounts is not None:
                u["accounts"] = sorted(set(accounts))
        return self._change(name, fn, "assignments changed", by, projects=projects, accounts=accounts)

    def remove(self, name: str, by: str = "") -> None:
        """Delete the account. The user's data folder is kept (delete it by hand if wanted)."""
        with self.lock:
            data = self._load()
            if data["users"].pop(name, None) is None:
                raise KeyError(f"no user {name!r}")
            self._save(data)
        self.audit.log("user removed", user=name, by=by)
