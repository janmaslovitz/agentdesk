import asyncio
import os

from agentdesk import core


def test_route_skips_the_model_when_tier_is_set(project, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the router model must not be called")

    monkeypatch.setattr(core, "query", boom)
    store = core.Store(project)
    meter = core.Meter(project, store, store.load())
    task = core.Task("1", "t", "do the thing", "standard")
    plan = asyncio.run(core.route(project, store, task, meter))
    assert plan == {"tier": "standard", "why": "set in the task file", "brief": "do the thing"}
    assert any(e.get("kind") == "routed" for e in core.read_activity(project, "1"))


def test_load_tasks_cache_sees_edit_add_delete_and_returns_copies(project):
    core.add_task(project, "a", "A", "body a")
    assert [t.id for t in core.load_tasks(project)] == ["a"]
    f = core.add_task(project, "b", "B", "body b")
    assert [t.id for t in core.load_tasks(project)] == ["a", "b"]
    f.write_text("+++\nid = \"b\"\ntitle = \"B2\"\ncrates = [\"x\"]\n+++\n\nnew body, longer\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    b = [t for t in core.load_tasks(project) if t.id == "b"][0]
    assert (b.title, b.body, b.crates) == ("B2", "new body, longer", ["x"])
    b.crates.append("polluted")
    b.depends.append("zzz")
    again = [t for t in core.load_tasks(project) if t.id == "b"][0]
    assert again.crates == ["x"] and again.depends == []
    f.unlink()
    assert [t.id for t in core.load_tasks(project)] == ["a"]
