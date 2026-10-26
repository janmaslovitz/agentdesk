import io
import json
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

from agentdesk import core, server

KEY = "sk-ant-api03-SECRETSECRETSECRET1234"
LOGIN = "C:/Users/someone/.claude-work-login"


def g(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def ident(monkeypatch, tmp_path):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "nogc"), "GIT_CONFIG_SYSTEM": str(tmp_path / "nogc")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AD_TEST_KEY", KEY)


@pytest.fixture
def full(project, monkeypatch):
    """A project with tasks, notes, state, reports, chat history, a log, and secrets lying around."""
    monkeypatch.setattr(core, "settings", lambda: {"accounts": [
        {"name": "default", "kind": "subscription", "config_dir": LOGIN},
        {"name": "work", "kind": "api_key", "key_env": "AD_TEST_KEY"}]})
    p = project
    core.add_task(p, "001", "First", "do it")
    core.add_task(p, "002", "Second", "then this", depends=["001"])
    (p.dir / "notes.md").write_text("# notes\nfact one\n", encoding="utf-8")
    core.Store(p).save({"tasks": {"001": {"status": "done"}}, "spent_usd": 1.5})
    (p.dir / "reports").mkdir()
    (p.dir / "reports" / "001.md").write_text("report text", encoding="utf-8")
    (p.dir / "chat_history.json").write_text(json.dumps([{"role": "user", "text": f"my key is {KEY} in {LOGIN}"}]), encoding="utf-8")
    (p.dir / "agentdesk.log").write_text("log line\n", encoding="utf-8")
    (p.dir / "runner.pid").write_text("123")
    (p.dir / "worktrees").mkdir()
    (p.dir / "worktrees" / "wt.txt").write_text("worktree file")
    (p.dir / "project.toml").write_text((p.dir / "project.toml").read_text(encoding="utf-8") + f'API_TOKEN = "{KEY}"\n', encoding="utf-8")
    return core.project(p.slug)


def new_home(monkeypatch, tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    monkeypatch.setattr(core, "DATA", home)
    return home


def check_recreated(q, repo):
    assert [t.id for t in core.load_tasks(q)] == ["001", "002"]
    assert core.load_tasks(q)[1].depends == ["001"]
    assert "fact one" in (q.dir / "notes.md").read_text(encoding="utf-8")
    assert core.Store(q).load()["tasks"]["001"]["status"] == "done"
    assert (q.dir / "reports" / "001.md").read_text(encoding="utf-8") == "report text"
    assert q.repo.as_posix() == repo.as_posix()
    assert q.cfg["repo"]["worktrees"].startswith(q.dir.as_posix())
    assert "account" not in q.cfg


def no_secrets(blob: bytes):
    for secret in (KEY, LOGIN, "someone", "AD_TEST_KEY"):
        assert secret.encode() not in blob, secret


def test_zip_roundtrip_and_no_secrets(full, tmp_path, monkeypatch):
    out = core.export_project(full, tmp_path / "x.zip")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
        man = json.loads(z.read("manifest.json"))
    assert man["format"] == 1 and man["project"] == full.name and man["app_version"] and man["exported_at"]
    assert "agentdesk.log" not in names and "runner.pid" not in names and not any("worktrees" in n for n in names)
    assert "chat_history.json" in names
    no_secrets(blob)
    assert str(full.repo).encode() not in blob and full.repo.as_posix().encode() not in blob
    assert b"account =" not in blob
    assert "agentdesk.log" in zipfile.ZipFile(core.export_project(full, tmp_path / "l.zip", include_logs=True)).namelist()

    new_home(monkeypatch, tmp_path)
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    q = core.import_project(out, str(repo2), account_name="work")
    assert q.cfg["account"] == "work"
    assert [t.id for t in core.load_tasks(q)] == ["001", "002"]
    assert core.import_project(out, str(repo2), new_name="Other", account_name="nonexistent").cfg.get("account") is None


def test_git_roundtrip_via_bare_repo(full, tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    g(full.repo, "remote", "add", "origin", str(bare))
    res = core.export_to_git(full, push=True)
    assert res["ok"] and res["committed"] and res["pushed"], res
    assert core.export_to_git(full)["committed"] is False
    assert (full.repo / ".agentdesk" / "manifest.json").exists()
    blob = "".join(p.read_text(encoding="utf-8") for p in (full.repo / ".agentdesk").rglob("*") if p.is_file())
    no_secrets(blob.encode())

    new_home(monkeypatch, tmp_path)
    clone = core.clone_for_import(str(bare))
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    check_recreated(core.import_project(clone, str(repo2), pull=True), repo2)


def test_config_repo_export(full, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=cfg, check=True)
    assert core.export_to_git(full, str(cfg))["committed"]
    assert core.export_to_git(full, str(cfg), push=True)["ok"] is False  # no remote: nothing pushed
    new_home(monkeypatch, tmp_path)
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    check_recreated(core.import_project(cfg, str(repo2)), repo2)


def mkzip(path, entries, manifest=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest or {"format": 1, "project": "Zed"}))
        z.writestr("project.toml", 'name = "Zed"\n[repo]\nbase = "main"\n')
        for n, d in entries.items():
            z.writestr(n, d)
    return path


def test_rejections(tmp_path, monkeypatch, project):
    new_home(monkeypatch, tmp_path)
    repo = tmp_path / "r"
    repo.mkdir()
    for bad in ("../evil.txt", "tasks/../../evil.txt", "/abs.txt", "C:/win.txt", "tasks\\..\\..\\evil.txt"):
        with pytest.raises(core.BadExport, match="unsafe"):
            core.import_project(mkzip(tmp_path / "s.zip", {bad: "x"}), str(repo))
    assert not (tmp_path / "evil.txt").exists() and not list(core.DATA.glob("projects/*"))
    with pytest.raises(core.BadExport, match="format version"):
        core.import_project(mkzip(tmp_path / "v.zip", {}, {"format": 99, "project": "Zed"}), str(repo))
    zipfile.ZipFile(tmp_path / "n.zip", "w").close()
    with pytest.raises(core.BadExport, match="manifest"):
        core.import_project(tmp_path / "n.zip", str(repo))
    monkeypatch.setattr(core, "MAX_FILE_BYTES", 100)
    with pytest.raises(core.BadExport, match="too large"):
        core.import_project(mkzip(tmp_path / "big.zip", {"notes.md": "x" * 500}), str(repo))
    assert not list(core.DATA.glob("projects/*"))


def test_name_clash_never_overwrites(full, tmp_path, monkeypatch):
    out = core.export_project(full, tmp_path / "x.zip")
    (full.dir / "notes.md").write_text("changed", encoding="utf-8")
    with pytest.raises(core.NameClash) as e:
        core.import_project(out, str(full.repo))
    assert (full.dir / "notes.md").read_text(encoding="utf-8") == "changed"
    assert e.value.suggested.endswith(" 2")
    q = core.import_project(out, str(full.repo), new_name=e.value.suggested)
    assert q.slug != full.slug


def test_cli(full, tmp_path, monkeypatch):
    from agentdesk import __main__ as cli
    f = tmp_path / "c.zip"
    monkeypatch.setattr("sys.argv", ["agentdesk", "export", full.slug, str(f)])
    cli.main()
    assert f.exists()
    monkeypatch.setattr("sys.argv", ["agentdesk", "import-file", str(f), "--repo", str(full.repo)])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "--name" in str(e.value)
    monkeypatch.setattr("sys.argv", ["agentdesk", "import-file", str(f), "--repo", str(full.repo), "--name", "From CLI"])
    cli.main()
    assert core.project("from-cli")


def test_http_routes(full, tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/api/projects"

    def post(path, body=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(body or {}).encode()
        req = urllib.request.Request(base + path, data=data, method="POST", headers={"Content-Type": ctype})
        try:
            r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r = e
        return r.status, r.read(), r.headers.get("Content-Type", "")
    try:
        st, data, ct = post(f"/{full.slug}/export")
        assert st == 200 and "zip" in ct and zipfile.ZipFile(io.BytesIO(data)).testzip() is None
        assert post(f"/{full.slug}/export", {"mode": "git"})[0] == 400  # git export needs confirmation
        with monkeypatch.context() as m:
            m.setattr(server.Runner, "running", lambda self: True)
            assert json.loads(post(f"/{full.slug}/export", {"mode": "git", "confirm": True})[1])["ok"] is False
        q = f"?repo={tmp_path}&new_name=Via+Http"
        st, body, _ = post("/import" + q, raw=data, ctype="application/zip")
        res = json.loads(body)
        assert res["ok"] and res["slug"] == "via-http" and res["repo"] == str(tmp_path) and res["tasks"] >= 0 and "account" in res
        # no repo given: AgentDesk picks a fresh folder under its data folder
        st, body, _ = post("/import?new_name=Auto+Home", raw=data, ctype="application/zip")
        res = json.loads(body)
        assert res["ok"] and Path(res["repo"]) == core.DATA / "repos" / "Auto-Home" and Path(res["repo"]).is_dir()
        st, body, _ = post("/import?new_name=Auto+Home", raw=data, ctype="application/zip")  # clash: the folder made for it is removed
        assert json.loads(body)["name_clash"] and sorted(p.name for p in (core.DATA / "repos").iterdir()) == ["Auto-Home"]
        monkeypatch.setattr(server, "overview", lambda: {})
        for window, want in ((None, False), (object(), True)):  # a browser / headless server has no native window
            monkeypatch.setattr(server, "_WINDOW", window)
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/overview") as o:
                assert json.loads(o.read())["desktop"] is want
        r = json.loads(post("/import" + q, raw=data, ctype="application/zip")[1])
        assert r["name_clash"] and r["suggested"]
    finally:
        srv.shutdown()


def test_branch_export_leaves_checkout_alone(full, tmp_path, monkeypatch):
    repo = full.repo
    (repo / "wip.txt").write_text("uncommitted", encoding="utf-8")
    cur, head = g(repo, "rev-parse", "--abbrev-ref", "HEAD"), g(repo, "rev-parse", "HEAD")
    status = g(repo, "status", "--porcelain")
    res = core.export_to_branch(full)
    br = "agentdesk-export/" + full.slug
    assert res["ok"] and res["committed"] and res["branch"] == br and not res["pushed"], res
    assert g(repo, "rev-parse", "--abbrev-ref", "HEAD") == cur and g(repo, "rev-parse", "HEAD") == head
    assert g(repo, "status", "--porcelain") == status and (repo / "wip.txt").read_text(encoding="utf-8") == "uncommitted"
    assert not (repo / ".agentdesk").exists()
    tip = g(repo, "rev-parse", br)
    assert tip.startswith(res["commit"])
    blob = g(repo, "show", f"{br}:manifest.json") + g(repo, "show", f"{br}:project.toml")
    no_secrets(blob.encode())
    # no changes: no new commit
    again = core.export_to_branch(full)
    assert again["ok"] and again["committed"] is False and g(repo, "rev-parse", br) == tip
    # a change: fast-forward child of the previous tip
    (full.dir / "notes.md").write_text("changed\n", encoding="utf-8")
    third = core.export_to_branch(full)
    assert third["committed"] and g(repo, "rev-parse", br + "^") == tip
    # the branch checked out in the repo is refused, not touched
    g(repo, "checkout", "-q", br)
    assert core.export_to_branch(full)["ok"] is False


def test_branch_export_push_and_import(full, tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    assert core.export_to_branch(full, push=True)["ok"] is False  # no remote
    g(full.repo, "remote", "add", "origin", str(bare))
    res = core.export_to_branch(full, push=True)
    br = res["branch"]
    assert res["ok"] and res["pushed"], res
    assert g(bare, "rev-parse", br) == g(full.repo, "rev-parse", br)
    # a diverged remote branch is refused (never forced)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    g(other, "checkout", "-q", br)
    (other / "x.txt").write_text("x")
    g(other, "add", "-A")
    g(other, "commit", "-qm", "remote work")
    g(other, "push", "-q", "origin", br)
    (full.dir / "notes.md").write_text("changed again\n", encoding="utf-8")
    bad = core.export_to_branch(full, push=True)
    assert bad["ok"] is False and bad["committed"] and not bad["pushed"]

    new_home(monkeypatch, tmp_path)
    clone = core.clone_for_import(str(bare), branch=br)
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    q = core.import_project(clone, str(repo2))
    assert [t.id for t in core.load_tasks(q)] == ["001", "002"]
