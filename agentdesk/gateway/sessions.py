"""Server-side login sessions and the failed-login throttle (both in memory: a gateway restart logs everyone out)."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field


def _key(sid: str) -> str:
    # The table holds a hash of the cookie value, not the value itself.
    return hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()


@dataclass
class Session:
    username: str
    csrf: str
    created: float
    last_seen: float
    ip: str = ""


@dataclass
class SessionTable:
    max_age_s: float = 12 * 3600
    idle_s: float = 2 * 3600
    clock: object = time.time
    _rows: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, username: str, ip: str = "") -> tuple[str, Session]:
        """A new random session id (the cookie value) and its row."""
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        s = Session(username, secrets.token_urlsafe(24), now, now, ip)
        with self._lock:
            self._rows[_key(sid)] = s
        return sid, s

    def get(self, sid: str | None) -> Session | None:
        """The live session for a cookie value (and mark it used), or None when unknown or expired."""
        if not sid or len(sid) > 200:
            return None
        k, now = _key(sid), self.clock()
        with self._lock:
            s = self._rows.get(k)
            if s is None:
                return None
            if now - s.created > self.max_age_s or now - s.last_seen > self.idle_s:
                del self._rows[k]
                return None
            s.last_seen = now
            return s

    def drop(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            return self._rows.pop(_key(sid), None)

    def drop_user(self, username: str) -> int:
        """End every session of a user (password change, disabled, removed)."""
        with self._lock:
            gone = [k for k, s in self._rows.items() if s.username == username]
            for k in gone:
                del self._rows[k]
        return len(gone)

    def sweep(self) -> None:
        now = self.clock()
        with self._lock:
            for k in [k for k, s in self._rows.items() if now - s.created > self.max_age_s or now - s.last_seen > self.idle_s]:
                del self._rows[k]

    def users(self) -> set[str]:
        with self._lock:
            return {s.username for s in self._rows.values()}


@dataclass
class Throttle:
    """Failed logins counted per (user, IP) and per IP within a sliding window; past the limit that key is locked
    for `lockout_s`. A success clears the (user, IP) counter."""
    max_failures: int = 5
    ip_max_failures: int = 30
    window_s: float = 15 * 60
    lockout_s: float = 15 * 60
    clock: object = time.time
    _fails: dict = field(default_factory=dict)   # key -> [failure times]
    _locked: dict = field(default_factory=dict)  # key -> locked until
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _keys(self, user: str, ip: str):
        return (("u", user.lower()[:64], ip), self.max_failures), (("ip", ip), self.ip_max_failures)

    def blocked_for(self, user: str, ip: str) -> float:
        """Seconds until a login for user from ip may be tried again (0 = now)."""
        now = self.clock()
        with self._lock:
            return max([0.0] + [self._locked.get(k, 0) - now for k, _ in self._keys(user, ip)])

    def failure(self, user: str, ip: str) -> bool:
        """Count a failed login; True when this one triggered a lockout."""
        now, locked = self.clock(), False
        with self._lock:
            for k, limit in self._keys(user, ip):
                times = [t for t in self._fails.get(k, []) if now - t < self.window_s] + [now]
                if len(times) >= limit:
                    self._locked[k] = now + self.lockout_s
                    times, locked = [], True
                self._fails[k] = times
            if len(self._fails) > 10_000:  # bound memory against a flood of made-up names
                for k in [k for k, v in self._fails.items() if not v or now - v[-1] >= self.window_s]:
                    del self._fails[k]
                for k in [k for k, until in self._locked.items() if until <= now]:
                    del self._locked[k]
        return locked

    def success(self, user: str, ip: str) -> None:
        with self._lock:
            self._fails.pop(self._keys(user, ip)[0][0], None)
