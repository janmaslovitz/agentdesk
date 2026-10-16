import subprocess

import pytest

from agentdesk import core


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA", tmp_path / "home")
    # isolate from the user's git identity/config
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nogitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "nogitconfig"))
    return tmp_path


def test_plain_folder_needs_opt_in(home):
    f = home / "plain"; f.mkdir(); (f / "a.txt").write_text("hi")
    with pytest.raises(core.NeedsLocalGit) as e:
        core.add_project("p", str(f), "main")
    assert e.value.reason == "not_a_repo"
    assert not (f / ".git").exists() and not (core.DATA / "projects" / "p").exists()


def test_plain_folder_setup_and_worktree(home):
    f = home / "plain"; f.mkdir(); (f / "a.txt").write_text("hi")
    p = core.add_project("p", str(f), "ignored", local_git=True)
    assert g(f, "log", "--format=%s") == "AgentDesk: starting point"
    assert (f / ".gitignore").read_text().split() == ["node_modules", "target", "dist", "build", ".venv", "__pycache__", "*.log", ".DS_Store"]
    assert g(f, "config", "--local", "user.email") == "agentdesk@localhost"
    assert g(f, "remote") == ""
    assert "a.txt" in g(f, "ls-files")
    assert p.cfg["repo"]["base"] == g(f, "symbolic-ref", "--short", "HEAD")
    wt = core.ensure_worktree(p, core.Store(p), core.Task("001", "t", "b"))
    assert (wt / "a.txt").exists() and g(wt, "branch", "--show-current") == "auto/001"


def test_existing_gitignore_kept(home):
    f = home / "plain"; f.mkdir(); (f / ".gitignore").write_text("mine\n")
    core.add_project("p", str(f), "main", local_git=True)
    assert (f / ".gitignore").read_text() == "mine\n"


def test_empty_repo_gets_first_commit_only(home):
    f = home / "empty"; f.mkdir(); g(f, "init"); (f / ".gitignore").write_text("x\n")
    with pytest.raises(core.NeedsLocalGit) as e:
        core.add_project("p", str(f), "main")
    assert e.value.reason == "no_commits"
    core.add_project("p", str(f), "main", local_git=True)
    assert g(f, "rev-list", "--count", "HEAD") == "1"
    assert (f / ".gitignore").read_text() == "x\n"


def test_existing_repo_unchanged(home):
    f = home / "repo"; f.mkdir(); g(f, "init"); (f / "a.txt").write_text("x")
    g(f, "-c", "user.name=u", "-c", "user.email=u@x", "add", "-A")
    g(f, "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-m", "mine")
    (f / "dirty.txt").write_text("y")
    head, status = g(f, "rev-parse", "HEAD"), g(f, "status", "--porcelain")
    core.add_project("p", str(f), "master", local_git=True)
    assert g(f, "rev-parse", "HEAD") == head and g(f, "status", "--porcelain") == status
    assert not (f / ".gitignore").exists()
