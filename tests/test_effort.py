"""Effort is chosen per task (router, architect, task file) and raised before the model is: the escalation ladder."""

import asyncio

from agentdesk import core


def labels(tier, effort=None):
    return [core.rung_label(*r) for r in core.effort_ladder(tier, effort)]


def test_ladder_raises_effort_before_the_model():
    assert labels("standard") == ["standard/medium", "standard/high", "hard/high", "hard/xhigh"]
    assert labels("mechanical") == ["mechanical", "standard/medium", "standard/high", "hard/high", "hard/xhigh"]
    assert labels("standard", "low") == ["standard/low", "standard/high", "hard/high", "hard/xhigh"]
    assert labels("standard", "high") == ["standard/high", "hard/high", "hard/xhigh"]
    assert labels("hard", "max")[0] == "hard/xhigh"  # never above the tier's ceiling
    assert labels("weird", "low") == ["weird/low"]


def test_task_file_effort_and_validation(project):
    core.add_task(project, "1", "t", "b", "standard", None, None, "high")
    t = core.load_tasks(project)[0]
    assert t.effort == "high" and 'effort = "high"' in (project.tasks_dir / "1.md").read_text()
    plan = asyncio.run(core.route(project, core.Store(project), t, None))
    assert plan["effort"] == "high"
    import pytest
    with pytest.raises(ValueError, match="effort"):
        core.add_task(project, "2", "t", "b", None, None, None, "turbo")


def test_plans_carry_effort(project):
    core.propose_plan(project, "s", [{"title": "A", "goal": "a", "tier": "standard", "effort": "high"}])
    ids = core.approve_plan(project)
    assert next(t for t in core.load_tasks(project) if t.id == ids["d1"]).effort == "high"
    import pytest
    core.close_plan(project)
    with pytest.raises(ValueError, match="effort"):
        core.propose_plan(project, "s", [{"title": "A", "goal": "a", "effort": "turbo"}])


def test_do_task_climbs_tier_and_effort(project, monkeypatch, tmp_path):
    seen = []

    async def route(p, store, task, meter):
        return {"tier": "standard", "why": "", "brief": "b", "effort": "medium"}

    async def work(p, store, task, wt, tier, brief, meter, effort=None):
        seen.append((tier, effort))
        return (tier, effort) == ("hard", "high"), "summary\nNO_CHANGES_NEEDED: test"

    monkeypatch.setattr(core, "route", route)
    monkeypatch.setattr(core, "work", work)
    monkeypatch.setattr(core, "ensure_worktree", lambda *a: tmp_path)
    monkeypatch.setattr(core, "worktree_conflicts", lambda *a: "")
    monkeypatch.setattr(core, "git", lambda *a, **k: (0, "abc"))
    monkeypatch.setattr(core, "commit", lambda *a: "abc123")
    monkeypatch.setattr(core, "keep_notes", lambda *a: None)
    monkeypatch.setattr(core, "large_files_note", lambda *a: "")
    core.add_task(project, "1", "t", "b")
    store = core.Store(project)
    st = store.load()
    meter = core.Meter(project, store, st)
    asyncio.run(core.do_task(project, store, core.load_tasks(project)[0], meter))
    assert seen == [("standard", "medium"), ("standard", "high"), ("hard", "high")]
    e = st["tasks"]["1"]
    assert e["status"] == "done" and (e["tier"], e["effort"]) == ("hard", "high")
    assert "standard/medium → standard/high → hard/high" in (project.path("reports") / "1.md").read_text()
