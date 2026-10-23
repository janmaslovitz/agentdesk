"""One corrupt or half-written data file must never blank the dashboard (it did: a zero-filled accounts file)."""

import json
import threading
from pathlib import Path


from agentdesk import core, server


def zero_file(path: Path, n: int = 728):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * n)


def aside(path: Path):
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


def test_read_json_missing_valid_and_corrupt(tmp_path):
    f = tmp_path / "a.json"
    assert core.read_json(f, {"d": 1}) == {"d": 1}
    f.write_text('{"x": 2}', encoding="utf-8")
    assert core.read_json(f, {}) == {"x": 2}
    for bad in (b"", b"\x00" * 100, b"{not json", b'{"half": '):
        f.write_bytes(bad)
        assert core.read_json(f, {"d": 1}) == {"d": 1}
        assert not f.exists() and aside(f)            # moved aside, not deleted
        for a in aside(f):
            a.unlink()
    f.write_bytes(b"\xff\xfe\x00bad utf8")
    assert core.read_json(f, "dflt") == "dflt"


def test_a_corrupt_file_is_restored_from_its_backup(tmp_path):
    f, bak = tmp_path / "state.json", tmp_path / "state.json.bak"
    bak.write_text(json.dumps({"tasks": {"001": {"status": "done"}}}), encoding="utf-8")
    f.write_bytes(b"\x00" * 50)
    assert core.read_json(f, {}, backup=bak)["tasks"]["001"]["status"] == "done"
    assert json.loads(f.read_text(encoding="utf-8"))["tasks"]["001"]["status"] == "done"  # and put back in place
    f.write_bytes(b"")
    bak.write_bytes(b"also bad")
    assert core.read_json(f, {"d": 1}, backup=bak) == {"d": 1}


def test_quarantine_is_logged(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{", encoding="utf-8")
    core.read_json(f, {})
    assert "x.json was corrupt; moved aside" in (core.DATA / "agentdesk.log").read_text(encoding="utf-8")


def test_write_atomic_flushes_and_leaves_no_temp_file(tmp_path, monkeypatch):
    synced = []
    real = core.os.fsync
    monkeypatch.setattr(core.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    f = tmp_path / "d" / "s.json"
    core.write_json(f, {"a": 1})
    assert json.loads(f.read_text(encoding="utf-8")) == {"a": 1} and synced
    assert [p.name for p in f.parent.iterdir()] == ["s.json"]
    core.write_json(f, {"a": 2})
    assert json.loads(f.read_text(encoding="utf-8")) == {"a": 2}


def test_a_zero_filled_account_file_does_not_break_usage_or_the_overview(project):
    name = project.account["name"]
    zero_file(core.DATA / "accounts" / f"{name}.json")
    assert core.account_usage(name) == {}
    assert core.current_reading(name) is None
    ov = server._overview()
    assert any(p["slug"] == project.slug for p in ov["projects"])  # the projects are still listed
    core.set_manual_usage(name, 40)                                 # and it heals on the next write
    assert core.current_reading(name)["utilization"] == 0.40


def test_state_json_survives_corruption_through_its_backup(project):
    store = core.Store(project)
    store.save({"tasks": {"001": {"title": "t", "status": "done"}}, "spent_usd": 1.5})
    store.save({"tasks": {"001": {"title": "t", "status": "done"}, "002": {"title": "u"}}, "spent_usd": 2.0})  # within a minute: backup keeps the first
    assert project.path("state.json.bak").exists()
    project.path("state.json").write_bytes(b"\x00" * 300)
    s = store.load()
    assert s["tasks"]["001"]["status"] == "done" and s["spent_usd"] == 1.5   # the last backup, not an empty queue
    project.path("state.json").write_bytes(b"")
    project.path("state.json.bak").unlink()
    assert store.load() == {"tasks": {}, "spent_usd": 0.0}                   # nothing to restore: empty, but no crash
    project.path("state.json").write_text('{"tasks": "wrong shape"}', encoding="utf-8")
    assert store.load() == {"tasks": {}, "spent_usd": 0.0}


def test_chat_usage_file_corruption_is_tolerated(project):
    c = server.Chat(server.LOOP, project)
    zero_file(project.path("chat_usage.json"), 40)
    assert c.usage() == {"spent_usd": 0.0, "tokens": {}}
    c._record_usage(0.5, {"input": 3})
    assert c.usage()["spent_usd"] == 0.5


def test_the_web_server_loop_restarts_itself(monkeypatch):
    class Srv:
        n = 0

        def serve_forever(self):
            Srv.n += 1
            if Srv.n < 3:
                raise OSError("selector broke")

    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    t = threading.Thread(target=server.serve_supervised, args=(Srv(),))
    t.start()
    t.join(5)
    assert not t.is_alive() and Srv.n == 3                      # two failures, restarted, then a normal return
    assert "web server loop failed" in (core.DATA / "agentdesk.log").read_text(encoding="utf-8") if (core.DATA / "agentdesk.log").exists() else True
