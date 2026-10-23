"""Server disk budget: limits and quotas with fake sizes, refusal at the hard limit, build kinds and worktree caps in
disk hygiene, shared cache pruning, symlink safety, the console tool. Only tmp_path fixtures are ever deleted."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from agentdesk import core, fsutil, hygiene, server, storage
from agentdesk.gateway import admin_cli, config
from agentdesk.gateway.users import Audit, UserStore

GB, DAY = 1024 ** 3, 86400
CFG = dict(storage.DEFAULTS)


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size) if size < 4096 else b"x" * size)
    return path


def age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    for root, dirs, files in os.walk(path, topdown=False):
        for n in files + dirs:
            os.utime(os.path.join(root, n), (t, t))
    os.utime(path, (t, t))


def link_dir(link: Path, to: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(to), str(link))
    else:
        os.symlink(to, link, target_is_directory=True)


@pytest.fixture
def policy():
    """Write <data>/server-policy.json for the test, removed afterwards (core.DATA is shared by all tests)."""
    f = core.DATA / core.SERVER_POLICY

    def put(**data):
        f.write_text(json.dumps(data), encoding="utf-8")
        os.utime(f, (time.time() + 1, time.time() + 1))  # a new mtime: the policy cache reloads
    yield put
    f.unlink(missing_ok=True)
    core._POLICY.update(key=None, data={})


# ----------------------------------------------------------------------------- budget and quota logic

def test_budget_levels_with_fake_sizes():
    b = storage.budget(CFG, 512 * GB, 100 * GB, 412 * GB)
    assert b["level"] == "ok" and b["pct"] == pytest.approx(19.5, abs=0.1)
    b = storage.budget(CFG, 512 * GB, 420 * GB, 92 * GB)
    assert b["level"] == "soft" and "soft limit" in b["message"]
    b = storage.budget(CFG, 512 * GB, 480 * GB, 32 * GB)
    assert b["level"] == "hard" and "refused" in b["message"] and "running work goes on" in b["message"]


def test_reserve_alone_triggers_the_hard_limit():
    # 70 % used, but only 30 GB really free (another program filled the disk): below the 40 GB reserve
    b = storage.budget(CFG, 512 * GB, 358 * GB, 30 * GB)
    assert b["level"] == "hard"


def test_bigger_shared_disk_counts_agentdesk_usage():
    b = storage.budget(CFG, 4000 * GB, 3500 * GB, 500 * GB, agentdesk_bytes=100 * GB)
    assert b["total"] == 512 * GB and b["used"] == 100 * GB and b["level"] == "ok"
    assert storage.budget(CFG, 4000 * GB, 3500 * GB, 500 * GB, agentdesk_bytes=480 * GB)["level"] == "hard"


def test_quota_default_override_and_gate():
    assert storage.quota_bytes(CFG, {"quota_gb": None}) == 50 * GB
    assert storage.quota_bytes(CFG, {"quota_gb": 5}) == 5 * GB
    assert storage.quota_bytes(CFG, {"quota_gb": 0}) == 0
    assert storage.user_level(46 * GB, 50 * GB) == "warn" and storage.user_level(51 * GB, 50 * GB) == "over"
    assert storage.user_level(10**15, 0) == "ok"  # no quota
    ok = storage.budget(CFG, 512 * GB, 100 * GB, 412 * GB)
    hard = storage.budget(CFG, 512 * GB, 500 * GB, 12 * GB)
    assert storage.gate(ok, 10 * GB, 50 * GB) is None
    assert "quota" in storage.gate(ok, 60 * GB, 50 * GB)
    assert "tell the administrator" in storage.gate(hard, 1, 50 * GB)


def test_config_defaults_and_validation(tmp_path):
    cfg = config.load(tmp_path)
    assert cfg["disk_total_gb"] == 512 and cfg["disk_reserve_gb"] == 40 and cfg["quota_gb"] == 50
    assert cfg["soft_limit_pct"] < cfg["hard_limit_pct"] and cfg["cache_caps_gb"]["cargo"] == 30
    (tmp_path / "server.toml").write_text("soft_limit_pct = 95\nhard_limit_pct = 90\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        config.load(tmp_path)
    (tmp_path / "server.toml").write_text("quota_gb = 5\ncache_caps_gb = { npm = 2 }\n", encoding="utf-8")
    cfg = config.load(tmp_path)
    assert cfg["quota_gb"] == 5.0 and storage.caps(cfg)["npm"] == 2.0 and storage.caps(cfg)["cargo"] == 30.0


# ----------------------------------------------------------------------------- the gate in the instance

def test_runner_refuses_at_hard_limit(project, policy):
    policy(gate={"refuse": True, "reason": "the server disk is 95% full", "until": time.time() + 600})
    err = server.Runner(project).start(["run", "--once"])
    assert err and "95% full" in err
    assert "DISK BUDGET" in project.path("agentdesk.log").read_text(encoding="utf-8")


def test_stale_gate_and_no_policy_do_not_refuse(policy):
    assert core.disk_gate() is None  # desktop: no policy file
    policy(gate={"refuse": True, "reason": "x", "until": time.time() - 10})
    assert core.disk_gate() is None


def test_import_refused_at_hard_limit(policy):
    policy(gate={"refuse": True, "reason": "over quota", "until": time.time() + 600})

    class H:
        headers = {"Content-Type": "application/json"}
        close_connection = False
    res = server.Handler.import_route(H(), {"source_url": "https://example.invalid/x.git", "repo": "x"}, {})
    assert res["ok"] is False and res.get("disk_budget") and "over quota" in res["message"]


def test_server_policy_turns_build_cleanup_on(policy):
    assert core.hygiene_cfg(None)["artifacts"] is False  # desktop default
    policy(hygiene={"auto": True, "artifacts": True, "max_worktrees": 3, "offpeak_hours": "01-06"})
    cfg = core.hygiene_cfg(None)
    assert cfg["auto"] is True and cfg["artifacts"] is True and cfg["max_worktrees"] == 3 and cfg["errors"] == []
    assert core.hygiene_cfg(None, {}, {})["auto"] is False  # explicit layers (tests, callers) are not affected


def test_clone_options_from_policy(policy):
    assert core.clone_options() == []
    policy(git={"clone_depth": 1, "partial_clone": True})
    assert core.clone_options() == ["--depth", "1", "--no-single-branch", "--filter=blob:none"]


# ----------------------------------------------------------------------------- build kinds, worktrees

def git(*a, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True)


def node_worktree(p, tid: str) -> Path:
    if not (p.repo / ".gitignore").exists():
        (p.repo / ".gitignore").write_text("node_modules/\n__pycache__/\n", encoding="utf-8")
        (p.repo / "package.json").write_text("{}", encoding="utf-8")
        (p.repo / "package-lock.json").write_text("{}", encoding="utf-8")
        git("add", "-A", cwd=p.repo)
        git("commit", "-q", "-m", "node", cwd=p.repo)
    w = Path(p.cfg["repo"]["worktrees"])
    core.add_worktree(p, w / tid, f"auto/{tid}", "main")
    write(w / tid / "src.js", 10)  # unmerged work: the worktree itself stays
    git("add", "-A", cwd=w / tid)
    git("commit", "-q", "-m", "work", cwd=w / tid)
    write(w / tid / "node_modules" / "left-pad" / "index.js", 2000)
    return w / tid


def art_cfg(p, **kw):
    cfg = core.hygiene_cfg(p)
    cfg.update(artifacts=True, **kw)
    return cfg


def test_build_kind_registry():
    names = {k.name for k in hygiene.BUILD_KINDS}
    assert {"cargo", "node_modules", "python-venv", "pycache", "gradle", "maven", "python-dist"} <= names


def test_node_modules_of_done_worktree_cleaned_outside_untouched(project, tmp_path):
    p = project
    wt = node_worktree(p, "101")
    keep = write(tmp_path / "outside" / "node_modules" / "x.js", 100)  # not in the repo or a worktree
    core.Store(p).save({"tasks": {"101": {"status": "done"}}, "spent_usd": 0})
    cfg = art_cfg(p)
    rep = hygiene.measure(p, cfg)
    arts = [a for a in rep["actions"] if a["kind"] == "artifact"]
    assert [Path(a["path"]).name for a in arts] == ["node_modules"]
    assert hygiene.refuse_artifact(p, keep.parent) is not None
    res = hygiene.apply(p, [a["id"] for a in arts], cfg, run_active=lambda p: False, procs=lambda: [])
    assert res["freed"] >= 2000 and not (wt / "node_modules").exists() and keep.exists()
    assert (wt / "package.json").exists()


def test_artifacts_wait_for_running_task_and_active_run(project):
    p = project
    wt = node_worktree(p, "102")
    core.Store(p).save({"tasks": {"102": {"status": "working"}}, "spent_usd": 0})
    rep = hygiene.measure(p, art_cfg(p))
    assert not [a for a in rep["actions"] if a["kind"] == "artifact"]  # its task is running
    core.Store(p).save({"tasks": {"102": {"status": "done"}}, "spent_usd": 0})
    res = hygiene.apply(p, None, art_cfg(p), run_active=lambda p: True, procs=lambda: [])
    assert res["done"] == [] and (wt / "node_modules").exists()  # a run is active: nothing at all
    busy = [{"pid": 7, "name": "node", "cwd": str(wt), "exe": None, "cmdline": "node test.js"}]
    res = hygiene.apply(p, None, art_cfg(p), run_active=lambda p: False, procs=lambda: busy)
    assert (wt / "node_modules").exists() and any("works in" in s.get("reason", "") for s in res["skipped"])


def test_tracked_or_unlocked_node_modules_is_refused(project):
    p = project
    wt = node_worktree(p, "103")
    os.remove(wt / "package-lock.json")
    assert "rebuilt exactly" in hygiene.refuse_artifact(p, wt / "node_modules")
    (wt / "package-lock.json").write_text("{}", encoding="utf-8")
    git("add", "-f", "node_modules", cwd=wt)
    assert "tracked" in hygiene.refuse_artifact(p, wt / "node_modules")


def test_worktree_cap_blocks_new_worktrees_and_plans_done_ones(project):
    p = project
    w = Path(p.cfg["repo"]["worktrees"])
    for tid in ("201", "202"):
        core.add_worktree(p, w / tid, f"auto/{tid}", "main")
    text = core._set_toml_key(core._project_text(p), "hygiene", "max_worktrees", "2")
    core._write_project(p, text)
    p = core.project(p.slug)
    assert "max_worktrees = 2" in core.worktree_cap_reason(p, "203")
    assert core.worktree_cap_reason(p, "201") is None  # its worktree exists already
    with pytest.raises(RuntimeError):
        core.ensure_worktree(p, core.Store(p), core.Task("203", "t", ""))
    core.Store(p).save({"tasks": {"201": {"status": "done"}, "202": {"status": "pending"}}, "spent_usd": 0})
    rep = hygiene.measure(p, core.hygiene_cfg(p))
    wts = [Path(a["path"]).name for a in rep["actions"] if a["kind"] == "worktree"]
    assert wts == ["201"]


def test_worktree_ttl_removes_done_worktree_keeps_branch(project):
    p = project
    w = Path(p.cfg["repo"]["worktrees"])
    core.add_worktree(p, w / "301", "auto/301", "main")
    write(w / "301" / "a.txt", 10)
    git("add", "-A", cwd=w / "301")
    git("commit", "-q", "-m", "unmerged work", cwd=w / "301")
    age(w / "301", 2)
    core.Store(p).save({"tasks": {"301": {"status": "done"}}, "spent_usd": 0})
    cfg = core.hygiene_cfg(p)
    cfg["worktree_ttl_hours"] = 24.0
    rep = hygiene.measure(p, cfg)
    ids = [a["id"] for a in rep["actions"] if a["kind"] == "worktree"]
    assert len(ids) == 1
    res = hygiene.apply(p, ids, cfg, run_active=lambda p: False, procs=lambda: [])
    assert not (w / "301").exists(), res["skipped"]
    assert "auto/301" in subprocess.run(["git", "branch"], cwd=p.repo, capture_output=True, text=True).stdout


def test_half_created_worktree_removed(project):
    p = project
    w = Path(p.cfg["repo"]["worktrees"])
    half = write(w / "401" / "partial.txt", 50).parent
    age(half, 1)
    rep = hygiene.measure(p, core.hygiene_cfg(p))
    acts = [a for a in rep["actions"] if a["kind"] == "halfwt"]
    assert [Path(a["path"]).name for a in acts] == ["401"]
    hygiene.apply(p, [a["id"] for a in acts], core.hygiene_cfg(p), run_active=lambda p: False, procs=lambda: [])
    assert not half.exists()


def test_cleaning_lock_is_seen_across_processes(project):
    f = project.dir / "hygiene.lock"
    f.write_text("999999", encoding="utf-8")  # a dead process: stale
    assert not hygiene.cleaning(project.slug)
    assert hygiene._take_lock(project) and f.read_text(encoding="utf-8") == str(os.getpid())
    hygiene._drop_lock(project)
    assert not f.exists()


def test_offpeak_window():
    assert hygiene.offpeak("01-06", 3) and not hygiene.offpeak("01-06", 7)
    assert hygiene.offpeak("22-05", 23) and hygiene.offpeak("22-05", 2) and not hygiene.offpeak("22-05", 12)
    assert hygiene.offpeak("", 12)


def test_large_files_listed(project):
    p = project
    w = Path(p.cfg["repo"]["worktrees"])
    core.add_worktree(p, w / "501", "auto/501", "main")
    write(w / "501" / "big.bin", 2 * 1024 * 1024)
    write(w / "501" / "small.txt", 10)
    git("add", "-A", cwd=w / "501")
    git("commit", "-q", "-m", "big", cwd=w / "501")
    assert core.large_files(w / "501", "main", 1.0) == [("big.bin", 2 * 1024 * 1024)]
    assert core.large_files(w / "501", "main", 0) == []


def test_import_spool_swept_after_a_day(tmp_path):
    root = tmp_path / "imports"
    old = write(root / "agentdesk-import-1" / "x", 100).parent
    new = write(root / "upload-2.zip", 100)
    age(old, 2)
    outside = write(tmp_path / "keep.txt", 5)
    assert hygiene.sweep_temp(root, 24.0) >= 100
    assert not old.exists() and new.exists() and outside.exists()


# ----------------------------------------------------------------------------- shared caches

def test_cache_prune_by_age_and_cap(tmp_path):
    cargo = tmp_path / "cache" / "cargo"
    old = write(cargo / "registry" / "cache" / "idx" / "old-1.0.crate", 3000)
    mid = write(cargo / "registry" / "cache" / "idx" / "mid-1.0.crate", 3000)
    new = write(cargo / "registry" / "cache" / "idx" / "new-1.0.crate", 3000)
    src = write(cargo / "registry" / "src" / "idx" / "old-1.0" / "lib.rs", 3000).parent.parent / "old-1.0"
    for f, d in ((old, 40), (mid, 5), (src, 40)):
        age(f, d)
    plan = storage.prune_plan(cargo, storage.CACHE_TOOLS["cargo"]["units"], cap=5000, max_age_s=30 * DAY, min_age_s=3600)
    paths = [Path(i["path"]).name for i in plan["items"]]
    assert "old-1.0.crate" in paths and "old-1.0" in paths  # unused for 40 days
    assert "mid-1.0.crate" in paths and "new-1.0.crate" not in paths  # least recently used, until under the cap
    freed, done = storage.prune_cache(tmp_path / "cache", cargo, plan)
    assert freed >= 9000 and new.exists() and not old.exists() and not mid.exists() and not src.exists()


def test_cache_prune_never_follows_links(tmp_path):
    cache = tmp_path / "cache"
    npm = cache / "npm"
    victim = write(tmp_path / "precious" / "data.bin", 500)
    (npm / "_npx").mkdir(parents=True)
    link_dir(npm / "_npx" / "evil", tmp_path / "precious")
    age(tmp_path / "precious", 100)
    plan = storage.prune_plan(npm, storage.CACHE_TOOLS["npm"]["units"], cap=1, max_age_s=DAY, min_age_s=0)
    assert all("evil" not in i["path"] for i in plan["items"])
    storage.prune_cache(cache, npm, {"items": [{"path": str(npm / "_npx" / "evil" / "data.bin"), "bytes": 500, "why": "x"}]})
    assert victim.exists()  # through a link: refused


def test_safe_delete_refuses_outside_root_and_through_links(tmp_path):
    root = tmp_path / "root"
    write(root / "a" / "f", 10)
    other = write(tmp_path / "other" / "f", 10)
    link_dir(root / "lnk", tmp_path / "other")
    assert fsutil.safe_delete(root, other)[2] is not None
    assert fsutil.safe_delete(root, root / "lnk" / "f")[2] is not None and other.exists()
    assert fsutil.safe_delete(root, root)[2] is not None and root.exists()
    f, _e, why = fsutil.safe_delete(root, root / "a")
    assert why is None and f == 10 and not (root / "a").exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hard links")
def test_dedupe_hardlinks_identical_files(tmp_path):
    a = tmp_path / "c" / "a.bin"
    b = tmp_path / "c" / "sub" / "b.bin"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    data = os.urandom(100_000)
    a.write_bytes(data)
    b.write_bytes(data)
    write(tmp_path / "c" / "other.bin", 100_000)
    age(tmp_path / "c", 1)
    res = storage.dedupe(tmp_path / "c", min_size=1000)
    assert res == {"saved": 100_000, "linked": 1}
    assert os.stat(a).st_ino == os.stat(b).st_ino and b.read_bytes() == data


def test_instance_env_points_tools_at_one_cache(tmp_path):
    home, uh = tmp_path / "srv", tmp_path / "srv" / "users" / "ann"
    env = storage.instance_env(home, CFG, uh)
    assert env["npm_config_cache"] == str(home / "cache" / "npm") and env["PIP_CACHE_DIR"] == str(home / "cache" / "pip")
    assert env["UV_CACHE_DIR"].endswith("uv") and env["GOMODCACHE"].endswith("go")
    if "CARGO_HOME" in env:  # links could be made: registry and git are the shared folders
        assert env["CARGO_HOME"] == str(uh / "cargo-home") and fsutil.is_link(uh / "cargo-home" / "registry")
    per_user = storage.instance_env(home, {**CFG, "run_as_users": True}, uh)
    assert per_user["npm_config_cache"] == str(uh / "cache" / "npm")
    assert storage.instance_env(home, {**CFG, "shared_caches": False}, uh) == {}


# ----------------------------------------------------------------------------- measuring, the manager, the console

def test_measure_home_categories_and_links(tmp_path):
    h = tmp_path / "u"
    write(h / "repos" / "app" / "src" / "main.rs", 1000)
    write(h / "repos" / "app" / "Cargo.toml", 10)
    write(h / "repos" / "app" / "target" / "CACHEDIR.TAG", 40)
    write(h / "repos" / "app" / "target" / "debug" / "app", 5000)
    write(h / "projects" / "app" / "worktrees" / "001" / "f.txt", 300)
    write(h / "projects" / "app" / "chat.json", 200)
    write(h / "projects" / "app" / "agentdesk.log", 100)
    write(h / "cache" / "npm" / "x", 700)
    write(tmp_path / "shared" / "huge", 9000)
    link_dir(h / "cargo-home", tmp_path / "shared")
    m = storage.measure_home(h)
    c = m["categories"]
    assert c["builds"] == 5040 and c["repos"] == 1010 and c["worktrees"] == 300 and c["chats"] == 200
    assert c["logs"] == 100 and c["caches"] == 700 and m["bytes"] == 7350  # the linked 9000 are not counted
    assert m["projects"]["app"] == 1010 + 5040 + 300 + 200 + 100
    assert m["top"][0]["category"] == "builds"


class FakeUsers:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return self.rows

    def get(self, name):
        return next((u for u in self.rows if u["username"] == name), None)


def test_storage_manager_gate_policy_and_clean(tmp_path, monkeypatch):
    home = tmp_path / "srv"
    ann = {"username": "ann", "home": str(home / "users" / "ann"), "disabled": False, "quota_gb": 0.000001}
    bob = {"username": "bob", "home": str(home / "users" / "bob"), "disabled": False, "quota_gb": None}
    write(Path(ann["home"]) / "repos" / "x" / "f", 5000)
    calls = []

    def tool(u, *args):
        calls.append((u["username"], args))
        return {"ok": True, "projects": [{"slug": "x", "reclaimable": 123, "actions": [{"kind": "artifact", "path": "p", "bytes": 123, "why": "w"}]}]}

    monkeypatch.setattr(storage.shutil, "disk_usage", lambda p: type("U", (), {"total": 512 * GB, "used": 100 * GB, "free": 412 * GB})())
    sm = storage.Storage(home, dict(CFG), FakeUsers([ann, bob]), Audit(tmp_path / "audit.log"), tool=tool)
    st = sm.scan()
    assert st["users"]["ann"]["level"] == "over" and st["users"]["bob"]["level"] == "ok"
    assert "quota" in sm.gate_for("ann") and sm.gate_for("bob") is None
    pol = json.loads((Path(ann["home"]) / storage.POLICY).read_text(encoding="utf-8"))
    assert pol["gate"]["refuse"] is True and pol["hygiene"]["artifacts"] is True and pol["hygiene"]["auto"] is True
    assert (home / storage.STATE).is_file()
    plan = sm.clean(dry_run=True)
    assert [c[0] for c in calls] == ["ann", "bob"] and calls[0][1] == ("clean", "--dry-run")  # over quota first
    assert plan["bytes"] >= 246 and sm.state["plan"] is plan
    # the hard limit refuses everyone at once, from the real free space (not the last scan)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda p: type("U", (), {"total": 512 * GB, "used": 490 * GB, "free": 22 * GB})())
    assert "refused" in sm.gate_for("bob")


def test_gateway_refuses_clone_at_hard_limit_and_shows_usage(tmp_path, monkeypatch):
    from agentdesk.gateway import admin, app
    gw = app.Gateway(tmp_path, {**config.DEFAULTS, "host": "127.0.0.1", "port": 0})
    gw.users.add("ann", "correct-horse-battery", "user")
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda p: type("U", (), {"total": 512 * GB, "used": 500 * GB, "free": 12 * GB})())

    class H:
        command, path, close_connection = "POST", "/admin/api/users/ann/projects/git", False
    with pytest.raises(admin.AdminError) as e:
        admin.route_api(H(), gw.admin, {"username": "root"}, ["users", "ann", "projects", "git"])
    assert e.value.code == 507 and "refused" in str(e.value)
    page = app.inject_bar(b"<html><body>x</body></html>", "ann", False,
                          {"used": 5 * GB, "quota": 10 * GB, "level": "ok", "measured": True})
    assert b"5.0 GB / 10.0 GB" in page


def test_instance_deletions_are_audited(tmp_path):
    uh = tmp_path / "u"
    uh.mkdir()
    (uh / "storage-audit.jsonl").write_text(json.dumps({"project": "x", "kind": "artifact", "path": "/a", "bytes": 5}) + "\n", encoding="utf-8")
    audit = Audit(tmp_path / "audit.log")
    sm = storage.Storage(tmp_path, dict(CFG), FakeUsers([]), audit)
    assert sm.ingest_audit("ann", uh) == 1 and not (uh / "storage-audit.jsonl").exists()
    rec = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["event"] == "storage deleted" and rec["bytes"] == 5 and rec["user"] == "ann"


def test_cli_user_quota_storage_and_dry_run(tmp_path, capsys, monkeypatch):
    home = tmp_path / "srv"
    pw = tmp_path / "pw"
    pw.write_text("correct-horse-battery\n", encoding="utf-8")
    admin_cli.main(["--home", str(home), "init", "--admin-password-file", str(pw)])
    admin_cli.main(["--home", str(home), "user", "quota", "admin", "7.5"])
    assert UserStore(home).get("admin")["quota_gb"] == 7.5
    admin_cli.main(["--home", str(home), "user", "quota", "admin", "default"])
    assert UserStore(home).get("admin")["quota_gb"] is None
    capsys.readouterr()
    admin_cli.main(["--home", str(home), "storage", "--scan"])
    out = capsys.readouterr().out
    assert "disk:" in out and "admin" in out
    admin_cli.main(["--home", str(home), "clean", "--dry-run"])
    assert "dry run: would free" in capsys.readouterr().out
