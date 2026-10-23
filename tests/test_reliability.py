"""Server reliability: sd_notify / watchdog, stale pid files, crash recovery, run guards, log rotation, health."""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from agentdesk import core, reliability, server
from agentdesk.gateway import app, config, instances, users


# ----------------------------------------------------------------------------- sd_notify / watchdog

@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs unix sockets")
def test_sd_notify_sends_to_notify_socket(tmp_path, monkeypatch):
    path = str(tmp_path / "n.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert reliability.sd_notify("READY=1")
    assert srv.recv(100) == b"READY=1"
    srv.close()


def test_sd_notify_is_noop_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert reliability.sd_notify("READY=1") is False
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert reliability.watchdog_interval() is None
    assert reliability.start_watchdog(lambda: (True, "")) is None


def test_watchdog_interval_is_half_of_watchdogsec(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert reliability.watchdog_interval() == 30
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 12345))
    assert reliability.watchdog_interval() is None  # meant for another process


def test_watchdog_pings_only_while_self_check_passes():
    sent, logs, ok = [], [], [True]
    w = reliability.Watchdog(lambda: (ok[0], "disk full"), 1, notify=sent.append, log=logs.append)
    assert w.tick() and sent == ["WATCHDOG=1"]
    ok[0] = False
    assert not w.tick() and sent == ["WATCHDOG=1"] and "disk full" in logs[0]


def test_watchdog_crashing_check_is_a_failure():
    sent = []

    def boom():
        raise RuntimeError("x")
    w = reliability.Watchdog(boom, 1, notify=sent.append, log=lambda m: None)
    assert not w.tick() and sent == []


def test_hung_self_check_sends_no_ping():
    sent, release = [], threading.Event()

    def hung():
        release.wait(30)
        return True, ""
    w = reliability.Watchdog(hung, 0.05, notify=sent.append, log=lambda m: None)
    w.start()
    time.sleep(0.4)
    assert sent == []  # the check never returned: systemd would restart us
    w.stop()
    release.set()


def test_loopback_get_fails_on_closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ok, why = reliability.loopback_get(port, "/gateway/health", timeout=1)
    assert not ok and why


def test_disk_ok_threshold(tmp_path):
    assert reliability.disk_ok(tmp_path, 0)[0]
    ok, why = reliability.disk_ok(tmp_path, 10**9)
    assert not ok and "free" in why


# ----------------------------------------------------------------------------- pid files

def test_stale_pid_file_removed_live_one_kept(tmp_path):
    dead = tmp_path / "dead.pid"
    dead.write_text("999999", encoding="utf-8")
    assert reliability.clean_pid_file(dead) and not dead.exists()
    live = tmp_path / "live.pid"
    live.write_text(str(os.getpid()), encoding="utf-8")
    assert not reliability.clean_pid_file(live) and live.exists()
    junk = tmp_path / "junk.pid"
    junk.write_text("not a pid", encoding="utf-8")
    assert reliability.clean_pid_file(junk) and not junk.exists()


def test_pid_file_with_wrong_command_line_is_stale(tmp_path):
    f = tmp_path / "x.pid"
    f.write_text(str(os.getpid()), encoding="utf-8")
    if reliability.proc_cmdline(os.getpid()) is None:
        pytest.skip("no /proc")
    assert reliability.clean_pid_file(f, ("definitely-not-this-program",)) and not f.exists()


def test_reused_pid_is_not_trusted(tmp_path):
    if reliability.proc_start_time(os.getpid()) is None:
        pytest.skip("no /proc")
    # a pid file written long before this process started names our pid: an old file of someone else
    f = tmp_path / "old.pid"
    f.write_text(str(os.getpid()), encoding="utf-8")
    old = time.time() - 10 * 365 * 86400
    os.utime(f, (old, old))
    assert reliability.clean_pid_file(f) and not f.exists()


# ----------------------------------------------------------------------------- crash recovery

def test_recovery_resets_orphan_working_tasks_and_stale_pid(project):
    store = core.Store(project)
    state = store.load()
    state["tasks"]["t1"] = {"status": "working"}
    state["tasks"]["t2"] = {"status": "done"}
    store.save(state)
    project.path("runner.pid").write_text("999999", encoding="utf-8")
    did = server.recover_project(project, log=lambda m: None)
    st = store.load()["tasks"]
    assert st["t1"]["status"] == "pending" and st["t1"]["crashes"] == 1 and "unexpectedly" in st["t1"]["last_failure"]
    assert st["t2"]["status"] == "done"
    assert not project.path("runner.pid").exists()
    assert any("t1" in d for d in did) and any("runner.pid" in d for d in did)
    assert server.recover_project(project, log=lambda m: None) == []  # idempotent


def test_recovery_keeps_tasks_of_a_live_run(project):
    store = core.Store(project)
    state = store.load()
    state["tasks"]["t1"] = {"status": "working"}
    store.save(state)
    project.path("runner.pid").write_text(str(os.getpid()), encoding="utf-8")  # a live "run" (this process)
    server.recover_project(project, log=lambda m: None)
    assert store.load()["tasks"]["t1"]["status"] == "working"
    project.path("runner.pid").unlink()


def test_recovery_prunes_half_created_worktrees(project):
    half = os.path.join(project.cfg["repo"]["worktrees"], "t9")
    os.makedirs(half)
    open(os.path.join(half, "stray.txt"), "w").close()
    did = server.recover_project(project, log=lambda m: None)
    assert not os.path.exists(half) and any("half-created" in d for d in did)


def test_mid_turn_chat_is_marked_interrupted(project):
    f = project.path("chat.json")
    f.write_text(json.dumps([{"role": "user", "text": "hi", "seq": 1}]), encoding="utf-8")
    assert server.mark_interrupted_chat(project)
    hist = json.loads(f.read_text(encoding="utf-8"))
    assert hist[-1]["kind"] == "interrupted" and server.INTERRUPTED in hist[-1]["text"]
    assert server.mark_interrupted_chat(project) == []  # now it ends with the note
    f.write_text(json.dumps([{"role": "user", "text": "x"}, {"role": "assistant", "text": "done"}]), encoding="utf-8")
    assert server.mark_interrupted_chat(project) == []


def test_recovered_task_is_retried_within_retry_crashed(project):
    state = {"tasks": {"t1": {"status": "working"}}}
    core.recover_stale(state, False)
    assert state["tasks"]["t1"]["crashes"] == 1
    assert core.autorunner_cfg(project)["retry_crashed"] >= 1  # one crash is within budget: next_startable picks it up


def test_disk_guard_refuses_to_start_a_run(project, monkeypatch):
    monkeypatch.setattr(reliability, "disk_free_gb", lambda p: 0.1)
    err = server.runner(project).start(["run", project.slug])
    assert err and "disk" in err
    assert "DISK FULL" in project.path("agentdesk.log").read_text(encoding="utf-8")


def test_guard_kills_run_over_hard_timeout(project, monkeypatch):
    killed = []
    monkeypatch.setattr(core, "kill_tree", lambda proc: killed.append(proc.pid))
    r = server.runner(project)
    r.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        project.path("runner.pid").write_text(str(r.proc.pid), encoding="utf-8")
        assert server.guard_pass() == []  # young
        old = time.time() - 3 * 3600
        os.utime(project.path("runner.pid"), (old, old))
        real = core.autorunner_cfg
        monkeypatch.setattr(core, "autorunner_cfg", lambda p=None, **k: {**real(p), "run_timeout_min": 60})
        did = server.guard_pass()
        assert killed == [r.proc.pid] and "hard timeout" in did[0]
    finally:
        r.proc.kill()
        r.proc.wait()
        r.proc = None


# ----------------------------------------------------------------------------- log rotation

def test_rotate_keeps_n_files(tmp_path):
    f = tmp_path / "a.log"
    for i in range(6):
        f.write_text(f"gen{i}" + "x" * 100, encoding="utf-8")
        assert reliability.rotate(f, 50, 3)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.log.1", "a.log.2", "a.log.3"]
    assert (tmp_path / "a.log.1").read_text().startswith("gen5")
    assert not reliability.rotate(tmp_path / "missing.log", 50, 3)
    f.write_text("small")
    assert not reliability.rotate(f, 50, 3)


def test_rotate_copytruncate_keeps_the_open_file(tmp_path):
    f = tmp_path / "r.log"
    fh = open(f, "ab")
    fh.write(b"y" * 200)
    fh.flush()
    assert reliability.rotate(f, 100, 2, copytruncate=True)
    assert f.stat().st_size == 0 and (tmp_path / "r.log.1").stat().st_size == 200
    fh.write(b"after")
    fh.flush()
    fh.close()
    assert f.read_bytes() == b"after"


def test_store_log_and_audit_rotate(project, tmp_path, monkeypatch):
    monkeypatch.setattr(reliability, "LOG_MAX_BYTES", 300)
    for i in range(30):
        core.Store(project).log("line %d %s" % (i, "z" * 40))
    assert project.path("agentdesk.log.1").exists() and project.path("agentdesk.log").stat().st_size <= 400
    a = users.Audit(tmp_path / "audit.log")
    for _ in range(30):
        a.log("ev", note="n" * 40)
    assert (tmp_path / "audit.log.1").exists()


# ----------------------------------------------------------------------------- gateway

def gateway(tmp_path):
    return app.Gateway(tmp_path, {**config.DEFAULTS, "host": "127.0.0.1", "port": 0})


def test_gateway_health_json_no_auth(tmp_path):
    gw = gateway(tmp_path)
    h = gw.health()
    assert h["ok"] is True and h["version"] and h["instances"] == {"up": 0, "down": 0}
    srv = gw.make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert gw.self_check(port) == (True, "")
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/gateway/health", timeout=5).read())
        assert body["ok"] and set(body) == {"ok", "version", "instances"}
    finally:
        srv.shutdown()
        srv.server_close()
    assert not gw.self_check(port)[0]  # nothing answers any more -> no watchdog ping


def test_self_check_fails_on_full_disk(tmp_path, monkeypatch):
    gw = gateway(tmp_path)
    srv = gw.make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(reliability, "disk_free_gb", lambda p: 0.0)
        ok, why = gw.self_check(srv.server_address[1])
        assert not ok and "free" in why
    finally:
        srv.shutdown()
        srv.server_close()


def test_gateway_recover_drops_stale_pid_and_tokens(tmp_path):
    gw = gateway(tmp_path)
    d = tmp_path / "instances"
    d.mkdir(exist_ok=True)
    (d / "alice.pid").write_text("999999", encoding="utf-8")
    (d / "alice.token").write_text("secret", encoding="utf-8")
    gw.recover()
    assert not list(d.glob("*.pid")) and not list(d.glob("*.token"))


def test_was_active_marker_and_resume(tmp_path):
    home = tmp_path / "u"
    home.mkdir()
    assert not instances.was_active(home)
    instances.mark_active(home, True)
    assert instances.was_active(home)
    m = instances.InstanceManager(tmp_path, dict(config.DEFAULTS))
    started = []
    m.ensure = lambda name, h: started.append(name)  # type: ignore[method-assign]
    idle = tmp_path / "idle"
    idle.mkdir()
    assert m.resume([("alice", home), ("bob", idle)]) == ["alice"] and started == ["alice"]
    instances.mark_active(home, False)
    assert not instances.was_active(home)


def test_instance_env_loses_systemd_variables():
    env = reliability.strip_systemd_env({"NOTIFY_SOCKET": "/x", "WATCHDOG_USEC": "1", "PATH": "p"})
    assert env == {"PATH": "p"}


# ----------------------------------------------------------------------------- SIGTERM

@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_serve_shuts_down_on_sigterm(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.Popen([sys.executable, "-m", "agentdesk", "serve", "--port", "0", "--data", str(tmp_path / "d")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=root)
    try:
        assert "listening" in p.stdout.readline()
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == 0
    finally:
        if p.poll() is None:
            p.kill()
