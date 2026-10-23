"""One `agentdesk serve` process per user, on a loopback port, started on demand, restarted when it dies and stopped
when idle (never while it reports work in progress at GET /api/busy).

Every instance gets a fresh random secret at each start; the gateway sends it as X-AgentDesk-Gateway-Token. The
instance reads it from a 0600 file under `<server-home>/instances/`, or, when started as another OS user
(run_as_users), from its standard input (that user can't read the gateway's files).
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

GATEWAY_HEADER = "X-AgentDesk-Gateway-Token"  # = agentdesk.server.GATEWAY_HEADER (not imported: that pulls in core)
APP = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent.parent
RESTART_WINDOW_S = 600
MAX_RESTARTS = 5  # automatic restarts within RESTART_WINDOW_S; after that the next request starts it again


class InstanceError(RuntimeError):
    """The user's instance could not be started."""


@dataclass
class Instance:
    username: str
    home: Path
    port: int
    token: str
    proc: subprocess.Popen
    started: float
    last_active: float
    inflight: int = 0
    stopping: bool = False
    log: Path | None = None

    def alive(self) -> bool:
        return self.proc.poll() is None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def answers(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(port: int, token: str, timeout: float = 2.0) -> bool:
    """Does the listener on the port answer an authenticated GET /api/busy as AgentDesk ({"busy": ...})? Only ever
    sends this start's fresh token."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            c.request("GET", "/api/busy", headers={GATEWAY_HEADER: token, "Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            data = json.loads(r.read(65536) or b"{}") if r.status == 200 else None
        finally:
            c.close()
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(data, dict) and "busy" in data


def harden_dir(path: Path) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


@dataclass
class InstanceManager:
    home: Path                        # the server home
    cfg: dict
    audit: object | None = None       # users.Audit
    command: Callable | None = None   # (username, user_home, port, token_arg) -> argv; default: agentdesk serve
    clock: Callable = time.time
    prepare: Callable | None = None   # (username, user_home) -> env var names to keep out of the instance's environment
    instances: dict = field(default_factory=dict)
    restarts: dict = field(default_factory=dict)  # username -> [restart times]
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _user_locks: dict = field(default_factory=dict)

    # -- command line
    def serve_command(self, username: str, user_home: Path, port: int, token_arg: str) -> list[str]:
        exe = self.cfg.get("python") or sys.executable
        base = [exe] if getattr(sys, "frozen", False) and not self.cfg.get("python") else [exe, "-m", "agentdesk"]
        argv = [*base, "serve", "--host", "127.0.0.1", "--port", str(port), "--data", str(user_home),
                "--auth-token-file", token_arg, "--trust-proxy-auth"]
        if not self.cfg.get("run_as_users"):
            return argv
        account = f"agentdesk-{username}"
        if self.cfg.get("run_as") == "systemd-run":
            return ["systemd-run", "--quiet", "--collect", "--pipe", f"--uid={account}", f"--working-directory={APP}",
                    f"--unit=agentdesk-{username}-{port}", "--", *argv]
        return ["sudo", "-n", "-u", account, "--", *argv]

    def _token_arg(self, username: str, token: str) -> str:
        if self.cfg.get("run_as_users"):
            return "/dev/stdin"
        d = self.home / "instances"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{username}.token"
        harden_dir(d)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if os.name == "posix":
            os.chmod(f, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        return str(f)

    def _log(self, event: str, username: str, **kw) -> None:
        if self.audit is not None:
            self.audit.log(event, user=username, **kw)

    # -- start / stop
    def _user_lock(self, username: str) -> threading.Lock:
        with self._lock:
            return self._user_locks.setdefault(username, threading.Lock())

    def _spawn(self, username: str, user_home: Path) -> Instance:
        user_home.mkdir(parents=True, exist_ok=True)
        hidden: set[str] = set()
        if not self.cfg.get("run_as_users"):  # (another OS user must still be able to enter its folder)
            harden_dir(user_home)
        if self.prepare is not None:  # this user's assigned accounts into settings.toml, others' keys out of the env
            hidden = set(self.prepare(username, user_home) or ())
        logs = self.home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        harden_dir(logs)
        logf = logs / f"{username}.log"
        last = ""
        for _attempt in range(3):  # a picked port can be taken before the instance binds it: try another
            port, token = free_port(), secrets.token_urlsafe(32)
            argv = (self.command or self.serve_command)(username, user_home, port, self._token_arg(username, token))
            env = {k: v for k, v in os.environ.items() if k != "AGENTDESK_SERVER_HOME" and k not in hidden}
            env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", AGENTDESK_HOME=str(user_home))
            kw: dict = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                        if sys.platform == "win32" else {"start_new_session": True})
            out = logf.open("ab")
            try:
                proc = subprocess.Popen(argv, cwd=APP, env=env, stdout=out, stderr=out,
                                        stdin=subprocess.PIPE if self.cfg.get("run_as_users") else subprocess.DEVNULL, **kw)
            except OSError as e:
                raise InstanceError(f"cannot start the instance: {e}") from e
            finally:
                out.close()
            if self.cfg.get("run_as_users") and proc.stdin:
                try:
                    proc.stdin.write(token.encode("utf-8"))
                    proc.stdin.close()
                except OSError:
                    pass
            deadline = time.monotonic() + float(self.cfg.get("instance_start_seconds", 60))
            verified = False
            while time.monotonic() < deadline and proc.poll() is None:
                # a listener on the port is only ours when our child is alive AND the listener accepts our token
                if answers(port, 0.25) and probe(port, token):
                    verified = True
                    break
                time.sleep(0.1)
            if verified and proc.poll() is None:
                now = self.clock()
                inst = Instance(username, user_home, port, token, proc, now, now, log=logf)
                self._log("instance started", username, port=port, pid=proc.pid)
                return inst
            if proc.poll() is None:  # never verified: kill it; a foreign listener on the port gets another port
                foreign = answers(port, 0.5)
                self._terminate(proc)
                last = "the listener on its port did not answer as AgentDesk" if foreign else "it did not start listening in time"
                if foreign:
                    continue
                break
            last = f"it exited with code {proc.returncode}"
        self._log("instance failed", username, why=last)
        raise InstanceError(f"the AgentDesk instance for {username} could not be started ({last}); see {logf}")

    def ensure(self, username: str, user_home: Path) -> Instance:
        """The user's running instance, started (or restarted) if needed."""
        with self._user_lock(username):
            with self._lock:
                inst = self.instances.get(username)
            if inst is not None and inst.alive() and not inst.stopping:
                return inst
            if inst is not None:
                self._forget(inst, "instance exited" if not inst.stopping else "", code=inst.proc.poll())
            inst = self._spawn(username, user_home)
            with self._lock:
                self.instances[username] = inst
            return inst

    def acquire(self, username: str, user_home: Path) -> Instance:
        """ensure() and count a request in progress (the instance is not stopped for idleness until release())."""
        for _ in range(3):
            inst = self.ensure(username, user_home)
            with self._lock:
                if self.instances.get(username) is inst and not inst.stopping:
                    inst.inflight += 1
                    inst.last_active = self.clock()
                    return inst
        raise InstanceError("the instance is restarting; try again")

    def release(self, inst: Instance) -> None:
        with self._lock:
            inst.inflight = max(0, inst.inflight - 1)
            inst.last_active = self.clock()

    def mark_dead(self, inst: Instance) -> None:
        """The instance refused a connection: forget it so the next ensure() starts a fresh one."""
        with self._lock:
            if self.instances.get(inst.username) is inst:
                del self.instances[inst.username]
        self._terminate(inst.proc)

    def _forget(self, inst: Instance, event: str, **kw) -> None:
        with self._lock:
            if self.instances.get(inst.username) is inst:
                del self.instances[inst.username]
        if event:
            self._log(event, inst.username, **kw)

    @staticmethod
    def _terminate(proc: subprocess.Popen, wait_s: float = 10) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(wait_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def stop(self, username: str, reason: str = "stopped") -> bool:
        with self._lock:
            inst = self.instances.pop(username, None)
            if inst is not None:
                inst.stopping = True
        if inst is None:
            return False
        self._terminate(inst.proc)
        self._log("instance stopped", username, reason=reason)
        return True

    def stop_all(self) -> None:
        for name in list(self.instances):
            self.stop(name, "gateway shutting down")

    # -- supervision
    def busy_info(self, inst: Instance) -> dict:
        """The instance's GET /api/busy answer ({} when it can't be reached)."""
        try:
            c = http.client.HTTPConnection("127.0.0.1", inst.port, timeout=5)
            c.request("GET", "/api/busy", headers={GATEWAY_HEADER: inst.token, "Host": f"127.0.0.1:{inst.port}"})
            r = c.getresponse()
            data = json.loads(r.read(65536) or b"{}") if r.status == 200 else {}
            c.close()
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, http.client.HTTPException):
            return {}

    def busy(self, inst: Instance) -> bool:
        """Does the instance report a run / chat turn / build in progress? (An unreachable instance is not busy.)"""
        return bool(self.busy_info(inst).get("busy"))

    def tick(self) -> None:
        """One supervision pass: restart crashed instances that were in use, stop idle ones."""
        now = self.clock()
        idle_s = float(self.cfg.get("idle_stop_minutes", 30)) * 60
        with self._lock:
            current = list(self.instances.values())
        for inst in current:
            if inst.stopping:
                continue
            if not inst.alive():
                self._forget(inst, "instance exited", code=inst.proc.returncode)
                recent = [t for t in self.restarts.get(inst.username, []) if now - t < RESTART_WINDOW_S]
                if now - inst.last_active < idle_s and len(recent) < MAX_RESTARTS:
                    self.restarts[inst.username] = recent + [now]
                    try:
                        self.ensure(inst.username, inst.home)
                        self._log("instance restarted", inst.username)
                    except InstanceError:
                        pass
                continue
            with self._lock:
                idle = inst.inflight == 0 and now - inst.last_active >= idle_s
            if not idle:
                continue
            if self.busy(inst):
                with self._lock:
                    inst.last_active = now  # work in progress keeps it alive; look again after another idle period
                continue
            with self._lock:  # a request may have come in meanwhile
                if inst.inflight or self.instances.get(inst.username) is not inst:
                    continue
                inst.stopping = True
                del self.instances[inst.username]
            self._terminate(inst.proc)
            self._log("instance stopped", inst.username, reason="idle")

    def status(self) -> list[dict]:
        with self._lock:
            return [{"username": i.username, "port": i.port, "pid": i.proc.pid, "started": i.started,
                     "last_active": i.last_active, "inflight": i.inflight} for i in self.instances.values()]
