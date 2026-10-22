"""Disk hygiene: size walker, stale trimming by mtime, refusals, process checks, dry run vs apply, config validation.
Only small tmp_path fixtures; nothing outside them is ever deleted."""

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from agentdesk import core, hygiene, server

DAY = 86400


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def age(path: Path, days: float) -> None:
    """Set the mtime of `path` and everything inside it `days` ago (children first, so folders keep the old time)."""
    t = time.time() - days * DAY
    if path.is_dir():
        for root, dirs, files in os.walk(path, topdown=False):
            for n in files + dirs:
                os.utime(os.path.join(root, n), (t, t))
    os.utime(path, (t, t))


def link_dir(link: Path, to: Path) -> None:
    """A junction on Windows (no admin rights needed), else a symlink."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(to), str(link))
    else:
        os.symlink(to, link, target_is_directory=True)


def make_target(root: Path, old: int = 3000, new: int = 1000) -> Path:
    """A small cargo target dir: CACHEDIR.TAG, debug/{deps,build,.fingerprint,incremental} with old and new entries."""
    write(root / "CACHEDIR.TAG", 43)
    d = root / "debug"
    write(d / "deps" / "libold-1111.rlib", old // 3)
    write(d / ".fingerprint" / "old-1111" / "lib-old", old // 3)
    write(d / "incremental" / "old-2222" / "s-1" / "query.bin", old - 2 * (old // 3))
    for x in (d / "deps" / "libold-1111.rlib", d / ".fingerprint" / "old-1111", d / "incremental" / "old-2222"):
        age(x, 10)
    write(d / "deps" / "libnew-3333.rlib", new // 2)
    write(d / "build" / "new-4444" / "output", new - new // 2)
    write(d / "app.exe", 100)  # outside the trim zones: never trimmed
    return root


def cfg_for(p, **kw) -> dict:
    cfg = core.hygiene_cfg(p)
    cfg.update(kw)
    return cfg


def set_env(p, **env) -> core.Project:
    text = core._project_text(p)
    for k, v in env.items():
        text = core._set_toml_key(text, "gates.env", k, core.toml_str(v))
    core._write_project(p, text)
    return core.project(p.slug)


# ----------------------------------------------------------------------------- config

def test_config_defaults_and_overrides():
    cfg = core.hygiene_cfg(None, {}, {})
    assert cfg["enabled"] is True and cfg["auto"] is False and cfg["max_target_gb"] == 60 and cfg["max_total_gb"] == 150
    assert cfg["stale_days"] == 3 and cfg["errors"] == []
    cfg = core.hygiene_cfg(None, {"max_target_gb": 40, "auto": True}, {"max_target_gb": 20, "target_dirs": ["../t"]})
    assert cfg["max_target_gb"] == 20 and cfg["auto"] is True and cfg["target_dirs"] == ["../t"]


@pytest.mark.parametrize("bad", [{"max_target_gb": "lots"}, {"stale_days": -1}, {"auto": "yes"}, {"enabled": 1},
                                 {"interval_min": 0}, {"target_dirs": "C:/x"}, {"target_dirs": [1]}, {"nope": 1},
                                 {"max_total_gb": float("nan")}, {"max_runs_mb": True}])
def test_config_invalid_values_never_crash(bad):
    cfg = core.hygiene_cfg(None, {}, bad)
    key = next(iter(bad))
    assert cfg["errors"] and key in cfg["errors"][0]
    if key in core.HYGIENE_DEFAULTS:
        assert cfg[key] == core.HYGIENE_DEFAULTS[key]  # the default stays


def test_config_not_a_table_and_project_file(project):
    assert core.hygiene_cfg(None, "x", ["y"])["errors"] == ["settings.toml: [hygiene] is not a table; ignored",
                                                           "project.toml: [hygiene] is not a table; ignored"]
    core._write_project(project, core._project_text(project) + '\n[hygiene]\nmax_target_gb = "big"\nstale_days = 7\n')
    cfg = core.hygiene_cfg(core.project(project.slug))
    assert cfg["stale_days"] == 7 and cfg["max_target_gb"] == 60 and "max_target_gb" in cfg["errors"][0]


def test_default_settings_have_hygiene_section():
    import tomllib
    assert tomllib.loads(core.DEFAULT_SETTINGS)["hygiene"]["auto"] is False


# ----------------------------------------------------------------------------- walking

def test_walk_size_counts_files_and_skips(tmp_path):
    write(tmp_path / "a" / "f1", 100)
    write(tmp_path / "a" / "b" / "f2", 250)
    write(tmp_path / "c" / "f3", 1000)
    s = hygiene.walk_size(tmp_path)
    assert (s.bytes, s.files) == (1350, 3)
    assert hygiene.walk_size(tmp_path, skip=[tmp_path / "c"]).bytes == 350
    assert hygiene.walk_size(tmp_path / "a" / "f1").bytes == 100
    assert hygiene.walk_size(tmp_path / "missing").errors == 1


def test_walk_never_follows_links(tmp_path):
    outside = tmp_path / "outside"
    write(outside / "big", 5000)
    t = tmp_path / "target"
    write(t / "small", 10)
    link_dir(t / "link", outside)
    assert hygiene.walk_size(t).bytes == 10
    assert hygiene.is_link(t / "link")
    freed, errs = hygiene.delete_path(t)
    assert not t.exists() and (outside / "big").exists() and freed == 10 and errs == 0


def test_delete_read_only_and_long_paths(tmp_path):
    t = tmp_path / "target"
    deep = t / ("d" * 60) / ("e" * 60) / ("f" * 60) / ("g" * 60) / ("h" * 60)
    os.makedirs(hygiene._lp(deep))
    f = hygiene._lp(deep / "ro.bin")
    with open(f, "wb") as fh:
        fh.write(b"y" * 77)
    os.chmod(f, stat.S_IREAD)
    assert len(str(deep / "ro.bin")) > 260 or os.name != "nt"
    assert hygiene.walk_size(t).bytes == 77
    assert hygiene.delete_path(t) == (77, 0)
    assert not t.exists()


# ----------------------------------------------------------------------------- stale trimming

def test_measure_and_trim_by_mtime(tmp_path):
    t = make_target(tmp_path / "target-x", old=3000, new=1000)
    m = hygiene.measure_target(t, time.time() - 3 * DAY)
    assert m["bytes"] == 43 + 3000 + 1000 + 100
    assert m["stale_bytes"] == 3000 and m["profiles"][0]["name"] == "debug" and m["profiles"][0]["stale_items"] == 3
    freed, items, errs = hygiene.trim_stale(t, time.time() - 3 * DAY)
    assert (freed, items, errs) == (3000, 3, 0)
    d = t / "debug"
    assert not (d / "deps" / "libold-1111.rlib").exists() and not (d / ".fingerprint" / "old-1111").exists()
    assert (d / "deps" / "libnew-3333.rlib").exists() and (d / "build" / "new-4444").exists() and (d / "app.exe").exists()


def test_profiles_under_target_triple(tmp_path):
    t = tmp_path / "target"
    write(t / ".rustc_info.json", 2)
    write(t / "x86_64-pc-windows-msvc" / "release" / "deps" / "a.rlib", 10)
    write(t / "release" / ".fingerprint" / "b" / "c", 5)
    assert sorted(p.relative_to(t).as_posix() for p in hygiene.profiles(t)) == ["release", "x86_64-pc-windows-msvc/release"]


# ----------------------------------------------------------------------------- refusals

def test_refuses_non_target_and_outside_paths(project, tmp_path, tmp_path_factory):
    cfg = cfg_for(project)
    good = make_target(tmp_path / "target-agents")
    assert hygiene.refuse_target(project, cfg, good) is None
    assert hygiene.refuse_target(project, cfg, good, good / "debug" / "deps") is None
    plain = tmp_path / "stuff"
    write(plain / "notes.txt", 5)
    assert "not clearly a cargo target dir" in hygiene.refuse_target(project, cfg, plain)
    src = tmp_path / "target-src"
    write(src / "Cargo.toml", 5)
    assert "holds sources" in hygiene.refuse_target(project, cfg, src)
    elsewhere = make_target(tmp_path_factory.mktemp("elsewhere") / "target")
    assert "outside the project's configured locations" in hygiene.refuse_target(project, cfg, elsewhere)
    assert "not inside the target dir" in hygiene.refuse_target(project, cfg, good, tmp_path / "stuff")
    assert "one of the project's own folders" in hygiene.refuse_target(project, cfg, project.repo)
    link_dir(good / "debug" / "jump", plain)
    assert "symlink/junction" in hygiene.refuse_target(project, cfg, good, good / "debug" / "jump" / "notes.txt")


def test_apply_never_deletes_refused_path(project, tmp_path):
    """A plan entry that points somewhere it shouldn't (e.g. tampered ids) is skipped by the re-check."""
    plain = tmp_path / "stuff"
    write(plain / "keep.txt", 5)
    res = hygiene.apply(project, [f"orphan:{hygiene._key(plain)}"], cfg_for(project), run_active=lambda p: False, procs=lambda: [])
    assert res["freed"] == 0 and res["skipped"] and (plain / "keep.txt").exists()


# ----------------------------------------------------------------------------- processes

def test_procs_using(tmp_path):
    t = tmp_path / "target-agents"
    other = tmp_path / "elsewhere"
    proc = lambda **kw: {"pid": 7, "name": "x", "exe": None, "cmdline": "", "cwd": None, "target_env": None, **kw}
    assert hygiene.procs_using([t], None) is None
    assert hygiene.procs_using([t], [proc(name="python.exe", cmdline="python x.py")]) == []
    assert hygiene.procs_using([t], [proc(name="rustc.exe", cmdline=f"rustc --out-dir {t / 'debug' / 'deps'} x.rs")])
    assert hygiene.procs_using([t], [proc(name="rustc.exe", cmdline=f"rustc --out-dir {other} x.rs")]) == []
    assert hygiene.procs_using([t], [proc(name="cargo.exe", cmdline="cargo test", cwd=str(t / "sub"))])
    assert hygiene.procs_using([t], [proc(name="cargo", cmdline="cargo test", cwd=str(other), target_env=str(t))])
    assert hygiene.procs_using([t], [proc(name="cargo.exe", cmdline="cargo build")])  # folder unknown: assume it might
    assert hygiene.procs_using([t], [proc(name="cargo.exe", cmdline="cargo build", cwd=str(other))]) == []
    assert hygiene.procs_using([t], [proc(name="mytest-abc.exe", exe=str(t / "debug" / "deps" / "mytest-abc.exe"))])


def test_cargo_with_unknown_folder_building_elsewhere(tmp_path):
    """Windows without psutil: cargo's folder is unknown, but the rustc processes below it show where it builds."""
    t, other = tmp_path / "target-agents", tmp_path / "target-coworker"
    proxy = {"pid": 1, "ppid": 0, "name": "cargo.exe", "exe": None, "cmdline": "cargo check", "cwd": None, "target_env": None}
    real = {**proxy, "pid": 2, "ppid": 1}

    def rustc(out, pid=3):
        return {"pid": pid, "ppid": 2, "name": "rustc.exe", "exe": None, "cwd": None, "target_env": None,
                "cmdline": f"rustc --crate-name x --out-dir {out / 'debug' / 'deps'} src/lib.rs"}

    assert hygiene.procs_using([t], [proxy, real, rustc(other)]) == []
    assert hygiene.procs_using([t], [proxy, real]) != []  # no evidence: assume it might use the folder
    assert hygiene.procs_using([t], [proxy, real, rustc(other), rustc(t, 4)]) != []


def test_skip_while_a_process_uses_the_dir(project, tmp_path):
    t = make_target(tmp_path / "target-agents")
    p = set_env(project, CARGO_TARGET_DIR=str(t))
    cfg = cfg_for(p, max_target_gb=1e-9)
    ids = [a["id"] for a in hygiene.measure(p, cfg)["actions"]]
    assert ids
    cargo = [{"pid": 42, "name": "cargo.exe", "exe": None, "cmdline": "cargo test", "cwd": str(t), "target_env": None}]
    res = hygiene.apply(p, ids, cfg, run_active=lambda p: False, procs=lambda: cargo)
    assert res["freed"] == 0 and not res["done"] and "pid 42" in res["skipped"][0]["reason"]
    res = hygiene.apply(p, ids, cfg, run_active=lambda p: False, procs=lambda: None)  # can't tell: skip
    assert res["freed"] == 0 and "can't tell" in res["skipped"][0]["reason"]
    res = hygiene.apply(p, ids, cfg, run_active=lambda p: True, procs=lambda: [])
    assert res["freed"] == 0 and "run is active" in res["skipped"][0]["reason"]
    assert (t / "debug" / "deps" / "libold-1111.rlib").exists()


# ----------------------------------------------------------------------------- dry run vs apply

def test_dry_run_then_apply_frees_reported_bytes(project, tmp_path):
    t = make_target(tmp_path / "target-agents", old=3000, new=1000)
    p = set_env(project, CARGO_TARGET_DIR=str(t), CARGO_INCREMENTAL="1")
    cfg = cfg_for(p, max_target_gb=1e-9, max_total_gb=1e-9)
    rep = hygiene.measure(p, cfg)
    tr = next(x for x in rep["targets"] if x["kind"] == "configured")
    assert tr["bytes"] == 4143 and tr["over"] and rep["warnings"]
    assert any("CARGO_INCREMENTAL" in a for a in rep["advice"])
    trim = next(a for a in rep["actions"] if a["kind"] == "trim")
    assert trim["bytes"] == 3000
    assert (t / "debug" / "deps" / "libold-1111.rlib").exists()  # measuring deletes nothing
    res = hygiene.apply(p, [trim["id"]], cfg, run_active=lambda p: False, procs=lambda: [])
    assert res["freed"] == trim["bytes"] == 3000 and [a["kind"] for a in res["done"]] == ["trim"]
    assert not (t / "debug" / "deps" / "libold-1111.rlib").exists() and (t / "debug" / "deps" / "libnew-3333.rlib").exists()
    log = p.path("agentdesk.log").read_text(encoding="utf-8")
    assert "disk hygiene: trimmed 3 stale build artifacts" in log and "freed 2.9 KB" in log


def test_profile_removed_when_still_over_limit(project, tmp_path):
    t = make_target(tmp_path / "target-agents", old=3000, new=1000)
    p = set_env(project, CARGO_TARGET_DIR=str(t))
    cfg = cfg_for(p, max_target_gb=1e-9)
    rep = hygiene.measure(p, cfg)
    assert [a["kind"] for a in rep["actions"]] == ["trim", "profile"]
    res = hygiene.apply(p, [a["id"] for a in rep["actions"]], cfg, run_active=lambda p: False, procs=lambda: [])
    assert res["freed"] == 3000 + 1000 + 100 and not (t / "debug").exists() and (t / "CACHEDIR.TAG").exists()


def test_under_limits_nothing_planned(project, tmp_path):
    t = make_target(tmp_path / "target-agents")
    p = set_env(project, CARGO_TARGET_DIR=str(t))
    rep = hygiene.measure(p, cfg_for(p))
    assert rep["actions"] == [] and rep["warnings"] == [] and rep["reclaimable"] == 0


def test_orphan_per_task_target(project, tmp_path):
    p = set_env(project, CARGO_TARGET_DIR=str(tmp_path / "target-agents"))
    make_target(tmp_path / "target-agents")
    orphan = make_target(tmp_path / "target-017")
    live = make_target(tmp_path / "target-018")
    fresh = make_target(tmp_path / "target-019")
    age(orphan, 10)
    age(live, 10)
    core.add_worktree(p, Path(p.cfg["repo"]["worktrees"]) / "018", "auto/018", "main")
    cfg = cfg_for(p)
    rep = hygiene.measure(p, cfg)
    assert {x["task"] for x in rep["targets"] if x["kind"] == "sibling"} == {"017", "018", "019"}
    orphans = [a for a in rep["actions"] if a["kind"] == "orphan"]
    assert [Path(a["path"]).name for a in orphans] == ["target-017"]
    res = hygiene.apply(p, [orphans[0]["id"]], cfg, run_active=lambda p: False, procs=lambda: [])
    assert res["freed"] == orphans[0]["bytes"] and not orphan.exists() and live.exists() and fresh.exists()


def test_finished_worktree_and_merged_branch(project):
    p = project
    w = Path(p.cfg["repo"]["worktrees"])
    core.add_worktree(p, w / "005", "auto/005", "main")
    write(w / "005" / "f.txt", 10)
    git = lambda *a, cwd=p.repo: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)
    git("add", "-A", cwd=w / "005")
    git("commit", "-q", "-m", "work", cwd=w / "005")
    git("merge", "-q", "--ff-only", "auto/005")
    core.add_worktree(p, w / "006", "auto/006", "main")  # not done: kept
    store = core.Store(p)
    store.save({"tasks": {"005": {"status": "done", "branch": "auto/005"}, "006": {"status": "pending"}}, "spent_usd": 0})
    core._MERGED_CACHE.clear()
    cfg = cfg_for(p)
    rep = hygiene.measure(p, cfg)
    kinds = {(a["kind"], Path(a["path"]).name) for a in rep["actions"]}
    assert ("worktree", "005") in kinds and ("branch", "005") in {(k, n.split("/")[-1]) for k, n in kinds}
    assert not any(n == "006" for _, n in kinds)
    res = hygiene.apply(p, [a["id"] for a in rep["actions"]], cfg, run_active=lambda p: False, procs=lambda: [])
    assert {a["kind"] for a in res["done"]} == {"worktree", "branch"}, res["skipped"]
    assert not (w / "005").exists() and (w / "006").exists()
    assert "auto/005" not in subprocess.run(["git", "branch"], cwd=p.repo, capture_output=True, text=True).stdout


def test_dirty_worktree_is_kept(project):
    p = project
    w = Path(p.cfg["repo"]["worktrees"])
    core.add_worktree(p, w / "007", "auto/007", "main")
    core.Store(p).save({"tasks": {"007": {"status": "done", "branch": "auto/007"}}, "spent_usd": 0})
    cfg = cfg_for(p)
    ids = [a["id"] for a in hygiene.measure(p, cfg)["actions"] if a["kind"] == "worktree"]
    assert ids  # auto/007 has no own commits: it counts as merged
    write(w / "007" / "new.rs", 3)  # ... but now holds work nobody committed
    res = hygiene.apply(p, ids, cfg, run_active=lambda p: False, procs=lambda: [])
    assert not res["done"] and "uncommitted" in res["skipped"][0]["reason"] and (w / "007" / "new.rs").exists()


def test_old_run_timelines_trimmed(project):
    p = project
    runs = p.path("runs")
    for i in range(5):
        write(runs / f"00{i}.jsonl", 1000)
        age(runs / f"00{i}.jsonl", 20 - i)
    write(runs / "009.jsonl", 1000)  # fresh: kept
    write(p.path("reports") / "001.md", 50)
    cfg = cfg_for(p, max_runs_mb=2500 / hygiene.MB)
    acts = [a for a in hygiene.measure(p, cfg)["actions"] if a["kind"] == "logs"]
    assert [a["id"] for a in acts] == ["logs:runs"] and acts[0]["bytes"] == 4000
    res = hygiene.apply(p, ["logs:runs"], cfg, run_active=lambda p: False, procs=lambda: [])
    assert res["freed"] == 4000
    assert sorted(f.name for f in runs.iterdir()) == ["004.jsonl", "009.jsonl"] and (p.path("reports") / "001.md").exists()


def test_trim_log_keeps_newest_lines(tmp_path):
    f = tmp_path / "agentdesk.log"
    f.write_bytes(b"".join(b"line %05d\n" % i for i in range(1000)))
    freed = hygiene.trim_log(f, 2000)
    text = f.read_text()
    assert freed > 0 and text.endswith("line 00999\n") and "older lines removed" in text.splitlines()[0]
    assert len(text) <= 1100


# ----------------------------------------------------------------------------- dashboard routes

def test_server_dry_run_and_confirmation(project, tmp_path, monkeypatch):
    t = make_target(tmp_path / "target-agents")
    p = set_env(project, CARGO_TARGET_DIR=str(t))
    monkeypatch.setattr(server, "project_busy", lambda p: False)
    hygiene._measure_now(p)
    out = server.disk_clean(p, {})
    assert out["dry_run"] and out["report"]["total"] > 0 and (t / "debug" / "deps" / "libold-1111.rlib").exists()
    assert server.disk_clean(p, {"apply": True, "ids": ["x"]})["message"] == "this needs confirmation"
    assert server.disk_clean(p, {"apply": True, "confirm": True, "ids": []})["ok"] is False
    monkeypatch.setattr(server, "project_busy", lambda p: True)
    assert "in progress" in server.disk_clean(p, {"apply": True, "confirm": True, "ids": ["x"]})["message"]
    assert hygiene.summary(p.slug)["total"] == out["report"]["total"]


def test_loop_pass_logs_config_errors_and_respects_auto_off(project, tmp_path):
    t = make_target(tmp_path / "target-agents")
    p = set_env(project, CARGO_TARGET_DIR=str(t))
    core._write_project(p, core._project_text(p) + '\n[hygiene]\nmax_target_gb = 0.1\nstale_days = "soon"\n')
    hygiene._CACHE.pop(p.slug, None)
    logged = []
    hygiene.loop_pass(run_active=lambda p: False, log=logged.append)
    assert hygiene._CACHE[p.slug]["total"] > 0
    assert (t / "debug" / "deps" / "libold-1111.rlib").exists()  # auto = false: nothing deleted
    assert any("stale_days" in x for x in logged)
