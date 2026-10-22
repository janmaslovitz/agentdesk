import asyncio
import subprocess

from agentdesk import core
from agentdesk.core import Project


def proj(mcp, tmp_path):
    return Project("x", tmp_path, {"name": "x", "mcp": mcp})


GOOD = {"command": "npx", "args": ["-y", "pkg"], "env": {"A": "1"}, "enabled": True}


def test_disabled_by_default_and_absent(tmp_path):
    assert proj({"a": {"command": "npx"}}, tmp_path).mcp() == {}
    assert Project("x", tmp_path, {}).mcp() == {}
    assert core.options(proj({"a": {"command": "npx"}}, tmp_path), role="worker").mcp_servers == {}


def test_worker_gets_server_router_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda c: "C:/x/npx.cmd" if c == "npx" else None)
    p = proj({"cg": GOOD}, tmp_path)
    w = core.options(p, role="worker", allowed_tools=["Read"])
    assert w.mcp_servers == {"cg": {"type": "stdio", "command": "C:/x/npx.cmd", "args": ["-y", "pkg"], "env": {"A": "1"}}}
    assert w.allowed_tools == ["Read"]  # mcp__cg__* is not added
    assert core.options(p, role="router").mcp_servers == {}
    assert core.options(p).mcp_servers == {}
    p2 = proj({"cg": {**GOOD, "roles": ["worker", "router"]}}, tmp_path)
    assert "cg" in core.options(p2, role="router").mcp_servers


def test_merges_existing_servers_and_unresolved_command(tmp_path, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda c: None)
    o = core.options(proj({"cg": GOOD}, tmp_path), role="worker", mcp_servers={"other": {"command": "z"}})
    assert set(o.mcp_servers) == {"cg", "other"} and o.mcp_servers["cg"]["command"] == "npx"


def test_invalid_entries_skipped_with_log(tmp_path):
    bad = {"n1": "str", "n2": {"enabled": True}, "n3": {**GOOD, "args": "x"}, "n4": {**GOOD, "env": {"A": 1}},
           "n5": {**GOOD, "roles": ["boss"]}, "n6": {**GOOD, "enabled": "yes"}, "ok": GOOD}
    lines = []
    assert list(proj(bad, tmp_path).mcp(lines.append)) == ["ok"]
    assert len(lines) == 6
    assert proj("junk", tmp_path).mcp() == {}


def test_guard_allows_mcp_tools(tmp_path):
    g = core.guard(tmp_path)
    r = asyncio.run(g("mcp__code-graph__semantic_code_search", {"query": "x"}, None))
    assert type(r).__name__ == "PermissionResultAllow"


def test_commit_never_stages_code_graph(tmp_path):
    def git(*a):
        return subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    git("config", "user.email", "a@b.c")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / ".code-graph").mkdir()
    (tmp_path / ".code-graph" / "index.db").write_text("x")
    task = core.Task.__new__(core.Task)
    task.id, task.title = "001", "t"
    p = Project("x", tmp_path, {"repo": {"co_author": "Co-Authored-By: x <x@y>"}})
    core.commit(p, task, tmp_path, "standard")
    assert git("ls-files").split() == ["a.txt"]
