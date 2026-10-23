"""Server reliability helpers (stdlib only; imported by core, the instance and the gateway).

- sd_notify / Watchdog: systemd READY=1 / WATCHDOG=1 over $NOTIFY_SOCKET. Everything is a no-op outside systemd.
  The watchdog thread pings only while the in-process self-check passes, so a hung process is restarted by systemd.
- pid_is_ours / clean_pid_file: a pid file is stale when its process is gone OR the pid was reused by another program
  (the process started after the file was written, or its command line is not what we expect).
- rotate: size-based log rotation (`x.log` -> `x.log.1` ... `x.log.<keep>`), also as copy-truncate for files that
  another process keeps open.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Callable

# ----------------------------------------------------------------------------- sd_notify / watchdog


def sd_notify(msg: str) -> bool:
    """Send `msg` ("READY=1", "WATCHDOG=1", "STOPPING=1", "STATUS=...") to systemd. False when not under systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(addr)
            s.sendall(msg.encode("utf-8"))
        return True
    except OSError:
        return False


def watchdog_interval() -> float | None:
    """Seconds between pings (half of WatchdogSec), or None when systemd does not watch this process."""
    try:
        usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    except ValueError:
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if usec <= 0 or (pid and pid.isdigit() and int(pid) != os.getpid()):
        return None
    return max(1.0, usec / 2_000_000)


def strip_systemd_env(env: dict) -> dict:
    """The environment for a child process: without the notify socket / watchdog variables of our own service."""
    return {k: v for k, v in env.items() if k not in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")}


class Watchdog(threading.Thread):
    """Pings systemd every `interval` seconds, but only when `check()` returns (True, ...) in time. `check` runs in
    this thread: if it hangs, no ping is sent either, which is the point."""

    def __init__(self, check: Callable[[], tuple[bool, str]], interval: float, notify: Callable[[str], object] = sd_notify,
                 log: Callable[[str], object] = lambda m: print(m, flush=True), name: str = "watchdog"):
        super().__init__(daemon=True, name=name)
        self.check, self.interval, self.notify, self.log = check, interval, notify, log
        self.stop_ev = threading.Event()
        self.pings = 0
        self.failures = 0

    def tick(self) -> bool:
        try:
            ok, why = self.check()
        except Exception as e:  # a crashing check is a failing check
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            self.notify("WATCHDOG=1")
            self.pings += 1
        else:
            self.failures += 1
            self.log(f"watchdog: self-check failed ({why}); not pinging systemd")
        return ok

    def run(self) -> None:
        while not self.stop_ev.wait(self.interval):
            self.tick()

    def stop(self) -> None:
        self.stop_ev.set()


def start_watchdog(check: Callable[[], tuple[bool, str]], **kw) -> Watchdog | None:
    """Start the watchdog thread when systemd asked for one (WATCHDOG_USEC); None otherwise."""
    iv = watchdog_interval()
    if iv is None or not os.environ.get("NOTIFY_SOCKET"):
        return None
    w = Watchdog(check, iv, **kw)
    w.start()
    return w


def loopback_get(port: int, path: str, headers: dict | None = None, tls: bool = False, timeout: float = 10.0,
                 host_header: str | None = None) -> tuple[bool, str]:
    """GET http(s)://127.0.0.1:port/path: (answered 200, reason)."""
    import http.client
    import ssl
    try:
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            c: http.client.HTTPConnection = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, context=ctx)
        else:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path, headers={"Host": host_header or f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        r.read(65536)
        c.close()
        return (r.status == 200, "" if r.status == 200 else f"{path} answered {r.status}")
    except (OSError, http.client.HTTPException) as e:
        return False, f"{path} did not answer: {e}"


def disk_free_gb(path: str | Path) -> float:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 2**30
    except OSError:
        return float("inf")


def disk_ok(path: str | Path, min_gb: float) -> tuple[bool, str]:
    free = disk_free_gb(path)
    return (free >= min_gb, "" if free >= min_gb else f"only {free:.2f} GB free (minimum {min_gb} GB)")


# ----------------------------------------------------------------------------- processes / pid files


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def proc_start_time(pid: int) -> float | None:
    """Epoch seconds at which the process started (Linux /proc; None elsewhere or when unknown)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def proc_cmdline(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return None


def pid_is_ours(pid: int, since: float | None = None, expect: tuple[str, ...] = (), slack: float = 5.0) -> bool:
    """Is `pid` still the process that wrote a pid file at time `since`? Alive is not enough: pids are reused."""
    if not pid_alive(pid):
        return False
    if since is not None:
        st = proc_start_time(pid)
        if st is not None and st > since + slack:  # it started after the file was written: another program
            return False
    if expect:
        cmd = proc_cmdline(pid)
        if cmd is not None and not any(e.lower() in cmd.lower() for e in expect):
            return False
    return True


def clean_pid_file(path: Path, expect: tuple[str, ...] = ()) -> bool:
    """Remove `path` when it names no live process of ours. True if it was removed."""
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
        since = path.stat().st_mtime
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        pid, since = 0, None  # unreadable / garbage: stale
    if pid > 0 and pid_is_ours(pid, since, expect):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def kill_group(pid: int) -> bool:
    """SIGKILL the process group led by `pid` (POSIX; runs and their children are session leaders). False if none."""
    if os.name == "nt":
        return False
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def group_alive(pid: int) -> bool:
    if os.name == "nt" or pid <= 1:
        return False
    try:
        os.killpg(pid, 0)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- log rotation

LOG_MAX_BYTES = int(float(os.environ.get("AGENTDESK_LOG_MAX_MB", "10")) * 1024 * 1024)
LOG_KEEP = int(os.environ.get("AGENTDESK_LOG_KEEP", "5"))


def configure_logs(max_mb: float | None = None, keep: int | None = None) -> None:
    global LOG_MAX_BYTES, LOG_KEEP
    if max_mb is not None:
        LOG_MAX_BYTES = max(1024, int(float(max_mb) * 1024 * 1024))
    if keep is not None:
        LOG_KEEP = max(1, int(keep))


def rotate(path: Path, max_bytes: int | None = None, keep: int | None = None, copytruncate: bool = False) -> bool:
    """If `path` is larger than max_bytes: x.log.(keep-1) -> x.log.keep ... x.log -> x.log.1 (the oldest is dropped).
    copytruncate=True copies to x.log.1 and empties the file in place (for a file another process holds open in
    append mode). True if rotated. Never raises."""
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    keep = LOG_KEEP if keep is None else keep
    path = Path(path)
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    try:
        for i in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
        first = path.with_name(f"{path.name}.1")
        if copytruncate:
            shutil.copyfile(path, first)
            with open(path, "r+b") as f:
                f.truncate(0)
        else:
            os.replace(path, first)
        old = path.with_name(f"{path.name}.{keep + 1}")
        old.unlink(missing_ok=True)
        return True
    except OSError:
        return False
