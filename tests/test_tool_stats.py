"""Per-task tool-call stats: counting, MCP grouping per server, accumulation in state.json."""

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from agentdesk import core
from test_network_retry import setup_work


def test_counting_and_mcp_grouping():
    s = core.new_tool_stats()
    for n in ["Read", "Read", "Edit", "mcp__code-graph__find", "mcp__code-graph__callers", "mcp__other__x"]:
        core.count_tool(s, n)
    assert s["total"] == 6 and s["by_tool"]["Read"] == 2
    assert s["mcp"] == {"code-graph": 2, "other": 1}
    assert "MCP: code-graph 2, other 1" in core.tools_line(s)


def test_a_mocked_run_saves_tool_stats(project, monkeypatch):
    class Client:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def query(self, text): pass
        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text="hi"), ToolUseBlock(id="1", name="Read", input={}),
                                            ToolUseBlock(id="2", name="mcp__code-graph__find", input={})], model="m")
            yield ResultMessage(subtype="success", duration_ms=5, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="s", total_cost_usd=0.1, usage={})
    store, task, meter = setup_work(project, monkeypatch)
    monkeypatch.setattr(core, "ClaudeSDKClient", Client)
    asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    e = meter.state["tasks"]["001"]
    assert e["tool_stats"] == {"total": 2, "by_tool": {"Read": 1, "mcp__code-graph__find": 1}, "mcp": {"code-graph": 1}}
    assert e["runs_stats"][0]["turns"] == 2 and e["runs_stats"][0]["duration_ms"] == 5
    asyncio.run(core.work(project, store, task, project.repo, "standard", "brief", meter))
    assert e["tool_stats"]["total"] == 4 and len(e["runs_stats"]) == 2
    assert "code-graph" in store.load()["tasks"]["001"]["tool_stats"]["mcp"]
