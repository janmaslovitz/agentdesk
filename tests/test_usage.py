"""Account usage reading: null/stale/manual readings, the pause gate, per-run usage deltas."""

import asyncio
import datetime as dt

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultMessage

from agentdesk import core, server

UNKNOWN = "usage unknown — enter it under Accounts"


@pytest.fixture(autouse=True)
def clean(project):
    core.save_account_usage(project.account["name"], {})
    return project


def ago(minutes):
    return (dt.datetime.now() - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def reading(p, util, minutes=0, source="sdk"):
    core.save_account_usage(p.account["name"], {"reading": {"utilization": util, "at": ago(minutes), "source": source}})


def test_null_reading_is_unknown(project):
    name = project.account["name"]
    assert core.usage_gate(name) == UNKNOWN
    core.save_account_usage(name, {"rate_limits": {"five_hour": {"utilization": None, "seen": ago(0)}}})  # old file shape
    assert core.current_reading(name) is None and core.usage_gate(name) == UNKNOWN


def test_stale_reading_is_unknown(project):
    reading(project, 0.2, minutes=31)
    assert core.usage_gate(project.account["name"]) == UNKNOWN
    reading(project, 0.2, minutes=5)
    assert core.usage_gate(project.account["name"]) is None


def test_manual_value(project):
    name = project.account["name"]
    core.set_manual_usage(name, 37)
    r = core.current_reading(name)
    assert r["source"] == "manual" and r["utilization"] == 0.37 and core.usage_gate(name) is None
    assert server.reading_view(name)["stale"] is False
    with pytest.raises(ValueError):
        core.set_manual_usage(name, 120)


def test_threshold_stop(project, monkeypatch):
    reading(project, 0.90)
    meter = core.Meter(project, core.Store(project), {})
    assert meter.stop_reason() == "usage limit 90% reached the pause threshold"
    reading(project, 0.50)
    assert meter.stop_reason() is None


def test_sdk_event_saves_reading_and_old_files_load(project):
    name = project.account["name"]
    meter = core.Meter(project, core.Store(project), {})
    meter.see(RateLimitEvent(rate_limit_info=RateLimitInfo(status="allowed", utilization=None, rate_limit_type="five_hour"), uuid="u", session_id="s"))
    assert core.current_reading(name) is None
    meter.see(RateLimitEvent(rate_limit_info=RateLimitInfo(status="allowed", utilization=0.4, rate_limit_type="five_hour"), uuid="u", session_id="s"))
    assert core.current_reading(name)["source"] == "sdk" and core.usage_gate(name) is None


def test_used_delta(project):
    meter = core.Meter(project, core.Store(project), {})
    reading(project, 0.30, minutes=10)
    before = meter.mark()
    assert meter.used(before)["usage_used_pct"] is None  # no newer reading: unknown, not 0
    reading(project, 0.34)
    assert meter.used(before) == {"usage_before": 30.0, "usage_after": 34.0, "usage_used_pct": 4.0}
    assert meter.used(None)["usage_used_pct"] is None


def test_work_records_usage_and_refuses_when_unknown(project, monkeypatch):
    store = core.Store(project)
    state = store.load()
    task = core.Task(id="001", title="t", body="b")
    state["tasks"] = {"001": {"title": "t"}}
    meter = core.Meter(project, store, state)
    name = project.account["name"]

    class FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def query(self, text): pass
        async def receive_response(self):
            core.save_account_usage(name, {"reading": {"utilization": 0.35, "at": ago(0), "source": "sdk"}})
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                                total_cost_usd=0.1, usage={})

    monkeypatch.setattr(core, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(core, "run_gates", lambda *a, **k: (True, ""))
    monkeypatch.setattr(core, "changed_crates", lambda *a: [])
    # a reading from 1 minute ago, then the attempt writes a newer one
    reading(project, 0.30, minutes=1)
    asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    run = state["tasks"]["001"]["runs"][0]
    assert run["usage_before"] == 30.0 and run["usage_after"] == 35.0 and run["usage_used_pct"] == 5.0
    # no reading at all: the attempt doesn't start
    core.save_account_usage(name, {})
    ok, _ = asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    assert ok is False and len(state["tasks"]["001"]["runs"]) == 1


def test_tier_averages():
    tasks = [{"tier": "standard", "used_pct": 4.0}, {"tier": "standard", "used_pct": 2.0}, {"tier": "hard", "used_pct": None}]
    assert server.tier_averages(tasks) == {"standard": {"avg_pct": 3.0, "tasks": 2}}
    assert server.task_used([{"usage_used_pct": None}]) is None
    assert server.task_used([{"usage_used_pct": 1.5}, {}, {"usage_used_pct": 2.0}]) == 3.5
