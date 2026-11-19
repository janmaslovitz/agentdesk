"""projedit: deleting a project (and maybe its repository) and editing project.toml from the dashboard."""

import json
import os
import shutil
import subprocess

import pytest

from agentdesk import core, projedit
from agentdesk.gateway import projtool


def g(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def nogc(monkeypatch, tmp_path):
    (tmp_path / "nogc").write_text("", encoding="utf-8")
    for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.setenv(k, str(tmp_path / "nogc"))


def make(name, repo_parent):
    repo = repo_parent / name
    repo.mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
    g(repo, "branch", "dev")
    return core.add_project(name, str(repo), "main")


@pytest.fixture
def proj():
    p = make("PeEdit", core.DATA / "repos")
    yield p
    shutil.rmtree(p.dir, ignore_errors=True)
    shutil.rmtree(core.DATA / "repos" / "PeEdit", ignore_errors=True)


def reload(p):
    return core.project(p.slug)


# ----------------------------------------------------------------------------- delete

def test_delete_removes_project_folder_and_keeps_repo(proj):
    (proj.dir / "notes.md").write_text("x")
    r = projedit.delete_project(proj, False)
    assert r["ok"] and r["slug"] == proj.slug and not r["repo_deleted"] and r["repo_note"]
    assert not proj.dir.exists() and (core.DATA / "repos" / "PeEdit").is_dir()
    assert proj.slug in (core.DATA / "agentdesk.log").read_text()


def test_delete_repo_under_data_repos(proj):
    repo = proj.repo
    r = projedit.delete_project(proj, True)
    assert r["repo_deleted"] and not repo.exists() and not proj.dir.exists()


def test_repo_outside_data_repos_is_kept(tmp_path):
    p = make("PeOut", tmp_path / "elsewhere")
    repo = p.repo
    r = projedit.delete_project(p, True)
    assert not r["repo_deleted"] and r["repo_note"] and repo.is_dir() and not p.dir.exists()


def test_symlinked_repo_is_kept(tmp_path):
    real = tmp_path / "real"
    p = make("PeLink", real)
    link = core.DATA / "repos" / "PeLink-link"
    os.symlink(p.repo, link, target_is_directory=True)
    try:
        p.cfg["repo"]["path"] = str(link)
        r = projedit.delete_project(p, True)
        assert not r["repo_deleted"] and r["repo_note"] and p.repo.exists() and (real / "PeLink").is_dir()
    finally:
        link.unlink(missing_ok=True)


def test_kept_repo_gets_worktrees_pruned(tmp_path):
    p = make("PePrune", tmp_path / "elsewhere")
    wt = p.dir / "worktrees" / "t1"
    wt.parent.mkdir(parents=True, exist_ok=True)
    g(p.repo, "worktree", "add", "-q", "-b", "t1", str(wt))
    projedit.delete_project(p, False)
    assert "t1" not in g(p.repo, "worktree", "list")


def test_busy_raises_and_deletes_nothing(proj):
    with pytest.raises(projedit.ProjectBusy):
        projedit.delete_project(proj, True, busy="a run is going")
    assert issubclass(projedit.ProjectBusy, ValueError) and proj.dir.exists()


def test_running_build_blocks(proj, monkeypatch):
    class Job:
        def running(self):
            return True
    monkeypatch.setattr(core, "build_job", lambda p: Job())
    with pytest.raises(projedit.ProjectBusy):
        projedit.delete_project(proj, False)
    assert proj.dir.exists()


def test_bad_slug_refused(proj):
    proj.slug = "a b"
    with pytest.raises(ValueError):
        projedit.delete_project(proj, False)


def test_audit_written_on_server(proj, monkeypatch):
    monkeypatch.setattr(core, "server_policy", lambda: {"x": 1})
    f = core.DATA / "storage-audit.jsonl"
    f.unlink(missing_ok=True)
    projedit.delete_project(proj, True)
    recs = [json.loads(x) for x in f.read_text().splitlines()]
    assert {"project", "repo"} <= {r["kind"] for r in recs if r["project"] == proj.slug}


def test_no_audit_on_desktop(proj, monkeypatch):
    monkeypatch.setattr(core, "server_policy", lambda: {})
    f = core.DATA / "storage-audit.jsonl"
    before = f.read_text() if f.exists() else ""
    projedit.delete_project(proj, False)
    assert (f.read_text() if f.exists() else "") == before


def test_projtool_remove_delegates(proj):
    assert projtool.remove(core, proj.slug, True) == {}
    assert not proj.dir.exists()
    with pytest.raises(KeyError):
        projtool.remove(core, proj.slug, False)


# ----------------------------------------------------------------------------- settings

def commented(p):
    t = core._project_text(p)
    core._write_project(p, "# my comment\n" + t.rstrip("\n") + "\n\n[extra]\n# keep\nfoo = 1\n")
    return reload(p)


def test_view(proj):
    v = projedit.settings_view(proj)
    assert v["slug"] == proj.slug and v["name"] == "PeEdit" and v["base"] == "main" and v["timeout_s"] == 3600
    assert v["account"] in v["accounts"] and "[repo]" in v["toml"]


def test_name(proj):
    proj = commented(proj)
    r = projedit.update_settings(proj, {"name": " New Name "})
    assert r["ok"] and r["settings"]["name"] == "New Name"
    t = core._project_text(proj)
    assert "# my comment" in t and "foo = 1" in t
    q = reload(proj)
    assert q.name == "New Name" and q.slug == proj.slug and q.dir == proj.dir
    for bad in ("", "  ", "x" * 81, "a\x01b", "a\nb", 5):
        r = projedit.update_settings(proj, {"name": bad})
        assert not r["ok"] and r["field"] == "name"
    assert reload(proj).name == "New Name"


def test_base(proj):
    proj = commented(proj)
    assert projedit.update_settings(proj, {"base": "dev"})["ok"]
    assert reload(proj).cfg["repo"]["base"] == "dev" and "# keep" in core._project_text(proj)
    for bad in ("nope", "bad..name", "-x", "", "a b"):
        r = projedit.update_settings(proj, {"base": bad})
        assert not r["ok"] and r["field"] == "base"


def test_base_remote_tracking_ref(proj):
    g(proj.repo, "update-ref", "refs/remotes/origin/rel", "HEAD")
    assert projedit.update_settings(proj, {"base": "origin/rel"})["ok"]


def test_account(proj):
    name = core.settings()["accounts"][0]["name"]
    assert projedit.update_settings(proj, {"account": name})["ok"] and reload(proj).cfg["account"] == name
    r = projedit.update_settings(proj, {"account": "no-such-account"})
    assert not r["ok"] and r["field"] == "account"


def test_commands(proj):
    proj = commented(proj)
    r = projedit.update_settings(proj, {"quick": ["echo a", 'echo "b"'], "final": "make\n\nmake test\n"})
    assert r["ok"]
    q = reload(proj)
    assert q.cfg["gates"]["quick"] == ["echo a", 'echo "b"'] and q.cfg["gates"]["final"] == ["make", "make test"]
    assert "# my comment" in core._project_text(q) and q.cfg["extra"]["foo"] == 1
    assert projedit.update_settings(q, {"quick": []})["settings"]["quick"] == []
    for bad in (["x"] * 21, ["ok", ""], ["a" * 1001], ["a\nb"], ["a\x00b"], [1], 7):
        r = projedit.update_settings(q, {"final": bad})
        assert not r["ok"] and r["field"] == "final"


def test_multiline_array_replaced(proj):
    t = core._project_text(proj).replace("quick = []", 'quick = [\n  "a",\n  "b",\n]')
    core._write_project(proj, t)
    assert reload(proj).cfg["gates"]["quick"] == ["a", "b"]
    assert projedit.update_settings(reload(proj), {"quick": ["c"]})["ok"]
    assert reload(proj).cfg["gates"]["quick"] == ["c"]


def test_timeout(proj):
    proj = commented(proj)
    assert projedit.update_settings(proj, {"timeout_s": 120})["settings"]["timeout_s"] == 120
    assert reload(proj).cfg["gates"]["timeout_s"] == 120 and reload(proj).cfg["gates"]["max_feedback_chars"] == 6000
    for bad in (59, 86401, "x", True, 1.5):
        r = projedit.update_settings(proj, {"timeout_s": bad})
        assert not r["ok"] and r["field"] == "timeout_s"


def test_invalid_field_writes_nothing(proj):
    before = core._project_text(proj)
    r = projedit.update_settings(proj, {"name": "Fine", "timeout_s": 1})
    assert not r["ok"] and core._project_text(proj) == before


# ----------------------------------------------------------------------------- save_toml

def test_save_toml_ok_keeps_comments(proj):
    t = "# hi\n" + core._project_text(proj).replace('name = "PeEdit"', 'name = "Renamed"')
    r = projedit.save_toml(proj, t)
    assert r["ok"] and r["settings"]["toml"] == t and reload(proj).name == "Renamed"


def test_save_toml_rejections(proj):
    t = core._project_text(proj)
    before = t
    path = proj.cfg["repo"]["path"]
    cases = [
        ("name = [", "toml"),
        ("x" * (64 * 1024 + 1), "toml"),
        (t.replace('name = "PeEdit"', "name = 5"), "name"),
        (t.replace(path, path + "-other"), "repo"),
        (t.replace(f'worktrees = "{proj.cfg["repo"]["worktrees"]}"', 'worktrees = "/x"'), "repo"),
        (t.replace('base = "main"', "base = 1"), "repo"),
        (t.replace(f'account = "{proj.cfg["account"]}"', 'account = "nope"'), "account"),
        (t.replace("quick = []", "quick = [1]"), "quick"),
        (t.replace("quick = []", 'quick = "x"'), "quick"),
    ]
    for text, field in cases:
        r = projedit.save_toml(proj, text)
        assert not r["ok"] and r["field"] == field, (field, r)
    assert core._project_text(proj) == before and reload(proj).slug == proj.slug


def test_save_toml_repo_change_allowed_on_request(proj):
    t = core._project_text(proj)
    path = proj.cfg["repo"]["path"]
    assert projedit.save_toml(proj, t.replace(path, path + "2"), allow_repo_change=True)["ok"]
    assert reload(proj).cfg["repo"]["path"] == path + "2"
