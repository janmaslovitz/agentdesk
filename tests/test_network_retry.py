"""A network hiccup ends the connection, not the task; settings fall back to the defaults for missing keys."""

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from agentdesk import core


def result(sid="s1", cost=0.1):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id=sid,
                         total_cost_usd=cost, usage={})


@pytest.mark.parametrize("text,expected", [
    ("API Error: Connection dropped (ECONNRESET)", True),
    ("Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)", True),
    ("API Error: 529 overloaded_error", True),
    ("fetch failed", True),
    ("request timed out", True),
    ("Command failed with exit code 1", False),        # no detail: not assumed to be the network
    ("KeyError: 'tiers'", False),
    ("checks failed: cargo test", False),
])
def test_which_errors_are_transient(text, expected):
    assert core.is_transient(RuntimeError(text)) is expected


class Flaky:
    """Fails the first `fail` connections with a network error (after one message with a session id), then works."""
    made: list = []
    fail = 1
    error = "API Error: Connection dropped (ECONNRESET)"

    def __init__(self, options):
        self.options = options
        Flaky.made.append(self)
        self.n = len(Flaky.made)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):
        self.text = text

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="working")], model="sonnet", session_id="sess-1")
        if self.n <= Flaky.fail:
            raise RuntimeError(Flaky.error)
        yield result("sess-1")


def setup_work(project, monkeypatch, delays=(0.01, 0.01)):
    Flaky.made, Flaky.fail, Flaky.error = [], 1, "API Error: Connection dropped (ECONNRESET)"
    monkeypatch.setattr(core, "ClaudeSDKClient", Flaky)
    monkeypatch.setattr(core, "run_gates", lambda *a, **k: (True, ""))
    monkeypatch.setattr(core, "changed_crates", lambda *a: [])
    monkeypatch.setattr(core, "network_delays", lambda: list(delays))
    store = core.Store(project)
    state = store.load()
    state["tasks"] = {"001": {"title": "t"}}
    return store, core.Task(id="001", title="t", body="b"), core.Meter(project, store, state)


def test_a_dropped_connection_waits_and_resumes_the_same_session(project, monkeypatch):
    store, task, meter = setup_work(project, monkeypatch)
    ok, summary = asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    assert ok is True and summary == "working"
    assert len(Flaky.made) == 2
    assert Flaky.made[0].options.resume is None and Flaky.made[1].options.resume == "sess-1"
    assert "Continue the task exactly where you left off" in Flaky.made[1].text
    log = project.path("agentdesk.log").read_text(encoding="utf-8")
    assert "network error" in log and "retrying" in log
    assert meter.state["tasks"]["001"].get("crashes", 0) == 0  # not counted against the task


def test_a_real_error_is_not_retried(project, monkeypatch):
    store, task, meter = setup_work(project, monkeypatch)
    Flaky.error = "KeyError: 'tiers'"
    with pytest.raises(RuntimeError):
        asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    assert len(Flaky.made) == 1


def test_it_gives_up_after_the_configured_retries(project, monkeypatch):
    store, task, meter = setup_work(project, monkeypatch, delays=(0.01,))
    Flaky.fail = 99
    with pytest.raises(RuntimeError, match="ECONNRESET"):
        asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    assert len(Flaky.made) == 2  # the first try and the one retry


def test_settings_fall_back_to_defaults_for_missing_keys():
    p = core.DATA / "settings.toml"
    p.write_text('[limits]\nretries_per_tier = 5\n\n[[accounts]]\nname = "mine"\nkind = "subscription"\n', encoding="utf-8")
    try:
        s = core.settings()
        assert s["limits"]["retries_per_tier"] == 5                    # the file wins
        assert s["limits"]["run_budget_usd"] == 40.0                   # a missing key falls back
        assert "deny" in s["guard"] and "tiers" in s and "router" in s  # whole missing sections too
        assert [a["name"] for a in s["accounts"]] == ["mine"]          # lists are replaced, not mixed
    finally:
        p.unlink(missing_ok=True)
