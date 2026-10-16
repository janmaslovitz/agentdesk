"""Coworker with full access (the default): curator of the whole project, in the project checkout, no guard."""

import asyncio
import subprocess

import pytest
from claude_agent_sdk import PermissionResultAllow

from agentdesk import core, server


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, server.LOOP).result(5)


@pytest.fixture
def chat(project):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
                   cwd=project.repo, check=True)
    core.set_chat_mode(project, "coworker")
    c = server.Chat(server.LOOP, project)
    yield c
    c.approvals.deny_all()


def allowed(chat, name, args):
    return isinstance(run(chat.coworker_permission(name, args)), PermissionResultAllow)


def test_full_access_is_the_default():
    assert core.chat_settings("coworker")["full_access"] is True


@pytest.mark.parametrize("cmd", ["git push origin main", "git remote add x https://e.x/r.git", "git reset --hard HEAD~1",
                                 "git checkout main", "gh pr create", "cargo publish", "type project.toml",
                                 "Set-Content settings.toml x"])
def test_every_command_is_allowed(chat, cmd):
    for tool in ("Bash", "PowerShell"):
        assert allowed(chat, tool, {"command": cmd})


def test_edits_anywhere_are_allowed_including_main_checkout(chat, tmp_path):
    for target in (chat.p.repo / "app.py", chat.p.dir / "project.toml", tmp_path / "elsewhere.txt"):
        for tool in ("Edit", "Write"):
            assert allowed(chat, tool, {"file_path": str(target)})


def test_hook_lets_everything_through(chat):
    r = run(chat.coworker_hook({"tool_name": "Bash", "tool_input": {"command": "git push --force"}}))
    assert r == {}


def test_options_use_the_project_checkout_not_a_worktree(chat):
    o = chat._coworker_options()
    assert o.cwd == str(chat.p.repo)
    assert not core.cowork_path(chat.p).exists()          # no auto/cowork worktree is made
    assert o.can_use_tool == chat.coworker_permission
    assert not o.hooks and not o.disallowed_tools
    assert str(chat.p.dir) in [str(d) for d in o.add_dirs]
    assert "curator" in o.system_prompt and chat.p.repo.as_posix() in o.system_prompt.replace("\\", "/")


def test_info_reports_the_checkout_and_base_branch(chat):
    info = chat.info()
    assert info["full_access"] is True and info["worktree"] == str(chat.p.repo) and info["branch"] == "main"


def test_switching_it_off_restores_the_guard(chat, monkeypatch):
    orig = core.chat_settings
    monkeypatch.setattr(core, "chat_settings", lambda mode: {**orig(mode), **({"full_access": False} if mode == "coworker" else {})})
    assert not allowed(chat, "Bash", {"command": "git push origin main"})
    o = chat._coworker_options()
    assert o.cwd == str(core.cowork_path(chat.p)) and o.hooks
