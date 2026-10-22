"""The task detail panel: prompt → orchestrator handoff → agent work (runs/<id>.jsonl) → agent report."""

import asyncio
import json
import threading
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from agentdesk import core, server


def result(cost=0.1, structured=None, error=False, text=None):
    return ResultMessage(subtype="error_max_turns" if error else "success", duration_ms=1, duration_api_ms=1, is_error=error, num_turns=3,
                         session_id="s1", total_cost_usd=cost, usage={"input_tokens": 10, "output_tokens": 5}, result=text,
                         structured_output=structured)


def worker_messages():
    return [
        AssistantMessage(content=[TextBlock(text="Looking at the code"), ToolUseBlock(id="t1", name="Edit", input={"file_path": "src/a.py", "old_string": "x"})],
                         model="sonnet", session_id="s1"),
        AssistantMessage(content=[ToolUseBlock(id="t2", name="Bash", input={"command": "pytest tests"})], model="sonnet", session_id="s1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=[{"type": "text", "text": "1 failed"}], is_error=True)]),
        result(),
    ]


def add_task(p, tid="001", title="Do it", body="Make the thing work.", depends=()):
    dep = f"depends = {json.dumps(list(depends))}\n" if depends else ""
    (p.tasks_dir / f"{tid}.md").write_text(f'+++\nid = "{tid}"\ntitle = "{title}"\n{dep}+++\n\n{body}\n', encoding="utf-8")


# ----------------------------------------------------------------------------- the jsonl writer

def test_activity_records_compact_events(project):
    act = core.Activity(project, "001", "worker")
    act.add("start", tier="standard", model="sonnet", effort="medium", attempt=1)
    for m in worker_messages():
        act.see(m)
    act.add("text", text="y" * 5000)
    evs = core.read_activity(project, "001")
    kinds = [e["kind"] for e in evs]
    assert kinds == ["start", "text", "tool", "tool", "tool_error", "result", "text"]
    assert evs[0]["effort"] == "medium" and all(e["role"] == "worker" for e in evs)
    edit, bash = evs[2], evs[3]
    assert edit["name"] == "Edit" and edit["file"] == "src/a.py"
    assert bash["input"] == "pytest tests" and "file" not in bash
    assert evs[4]["name"] == "Bash" and evs[4]["text"] == "1 failed"
    assert evs[5]["turns"] == 3 and evs[5]["tokens"]["input"] == 10 and evs[5]["cost_usd"] == 0.1
    assert len(evs[6]["text"]) <= core.ACTIVITY_TEXT + 1  # capped
    assert core.activity_path(project, "001") == project.path("runs") / "001.jsonl"


def test_activity_reader_skips_torn_lines_and_writer_caps_the_file(project, monkeypatch):
    f = core.activity_path(project, "002")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"kind": "tool", "name": "Read"}\n{"kind": "res', encoding="utf-8")
    assert [e["kind"] for e in core.read_activity(project, "002")] == ["tool"]
    assert core.read_activity(project, "nope") == []
    monkeypatch.setattr(core, "ACTIVITY_MAX_BYTES", 10)
    act = core.Activity(project, "002", "worker")
    act.add("text", text="dropped once the file is over the cap")
    act.add("check", ok=True)  # key events are still kept
    assert "dropped" not in f.read_text(encoding="utf-8") and '"check"' in f.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- written during (mocked) runs

class FakeClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):
        self.text = text

    async def receive_response(self):
        for m in worker_messages():
            yield m


def test_work_writes_the_timeline_and_records_effort(project, monkeypatch):
    gates = iter([(False, "$ pytest\n1 failed"), (True, "")])
    monkeypatch.setattr(core, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(core, "run_gates", lambda *a, **k: next(gates))
    monkeypatch.setattr(core, "changed_crates", lambda *a: [])
    store = core.Store(project)
    state = store.load()
    state["tasks"] = {"001": {"title": "t"}}
    meter = core.Meter(project, store, state)
    ok, _ = asyncio.run(core.work(project, store, core.Task(id="001", title="t", body="b"), project.repo, "standard", "brief", meter))
    assert ok
    runs = meter.state["tasks"]["001"]["runs"]
    tier = core.settings()["tiers"]["standard"]
    assert [r["effort"] for r in runs] == [tier.get("effort")] * 2 and [r["attempt"] for r in runs] == [1, 2]
    evs = core.read_activity(project, "001")
    kinds = [e["kind"] for e in evs]
    assert kinds.count("start") == 2 and kinds.count("result") == 2 and kinds.count("tool") == 4
    checks = [e for e in evs if e["kind"] == "check"]
    assert [c["ok"] for c in checks] == [False, True] and "1 failed" in checks[0]["text"]
    assert any(e["kind"] == "retry" and e["attempt"] == 2 for e in evs)
    assert kinds.index("retry") < len(kinds) - 1 - kinds[::-1].index("start")  # the retry comes before the second start
    assert evs[0]["model"] == tier["model"] and evs[0].get("effort") == tier.get("effort")


def test_route_writes_the_timeline(project, monkeypatch):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[ToolUseBlock(id="r1", name="Grep", input={"pattern": "def main", "path": "src"})], model="haiku")
        yield result(cost=0.01, structured={"tier": "standard", "why": "two files", "brief": "edit a and b"})

    monkeypatch.setattr(core, "query", fake_query)
    store = core.Store(project)
    meter = core.Meter(project, store, store.load())
    plan = asyncio.run(core.route(project, store, core.Task(id="003", title="t", body="b"), meter))
    assert plan["tier"] == "standard"
    evs = core.read_activity(project, "003")
    assert [e["kind"] for e in evs] == ["start", "tool", "result", "routed"] and all(e["role"] == "router" for e in evs)
    assert evs[1]["input"] == "def main  in src" and evs[3]["text"] == "two files"
    assert "effort" in meter.state["tasks"]["003"]["runs"][0]


# ----------------------------------------------------------------------------- the endpoint

@pytest.fixture
def http():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def get(url):
    return json.load(urllib.request.urlopen(url))


def test_endpoint_empty_states_for_a_pending_task(project, http):
    add_task(project, "001", depends=["000"])
    d = get(f"{http}/api/p/{project.slug}/task/001")
    assert d["status"] == "pending" and d["body"] == "Make the thing work."
    assert d["activity"] == [] and d["log"] == [] and d["report"] == "" and d["brief"] == "" and d["failure"] == ""
    h = d["handoff"]
    assert h["runs"] == [] and h["why"] == "" and h["brief"] == "" and h["depends"] == ["000"]
    assert h["branch"] is None and h["worktree"] is None and h["commit"] is None
    assert d["totals"] == {"runs": 0, "turns": 0, "tokens": {}, "cost": 0}


def test_endpoint_for_a_finished_task(project, http):
    add_task(project, "001")
    store = core.Store(project)
    state = store.load()
    runs = [{"tier": "router", "model": "haiku", "effort": None, "cost_usd": 0.01, "turns": 2, "tokens": {"input": 5}, "at": "2026-10-20T10:00:00"},
            {"tier": "standard", "model": "sonnet", "effort": "medium", "attempt": 1, "cost_usd": 0.5, "turns": 9, "tokens": {"input": 100, "output": 7},
             "at": "2026-10-20T10:05:00"}]
    state["tasks"]["001"] = {"title": "Do it", "status": "done", "tier": "standard", "why": "a small change", "brief": "edit a.py",
                             "runs": runs, "commit": "abc123", "branch": "auto/001", "started": "2026-10-20T10:00:00", "finished": "2026-10-20T10:09:00"}
    store.save(state)
    project.path("reports").mkdir(exist_ok=True)
    project.path("reports").joinpath("001.md").write_text("# 001: Do it\n\nDone.\n", encoding="utf-8")
    act = core.Activity(project, "001", "worker")
    for m in worker_messages():
        act.see(m)
    d = get(f"{http}/api/p/{project.slug}/task/001")
    assert d["status"] == "done" and d["report"].startswith("# 001: Do it")
    h = d["handoff"]
    assert (h["tier"], h["model"], h["effort"], h["why"], h["brief"]) == ("standard", "sonnet", "medium", "a small change", "edit a.py")
    assert h["branch"] == "auto/001" and h["commit"] == "abc123" and h["finished"] == "2026-10-20T10:09:00" and len(h["runs"]) == 2
    assert [e["kind"] for e in d["activity"]] == ["text", "tool", "tool", "tool_error", "result"] and d["log"] == []
    assert d["totals"]["runs"] == 2 and d["totals"]["turns"] == 11 and d["totals"]["tokens"] == {"input": 105, "output": 7}
    # the runs in /state are the same rows (one helper builds both)
    st = get(f"{http}/api/p/{project.slug}/state")
    assert next(t for t in st["tasks"] if t["id"] == "001")["runs"] == h["runs"]


def test_endpoint_falls_back_to_the_log_for_older_runs(project, http):
    add_task(project, "001")
    project.path("agentdesk.log").write_text("[10-20 10:00:00] 001: routed to standard (x)\n[10-20 10:00:01] 0011: other\n"
                                             "[10-20 10:00:02] 001: done at abc\n", encoding="utf-8")
    state = core.Store(project).load()
    state["tasks"]["001"] = {"title": "Do it", "status": "done", "runs": []}
    core.Store(project).save(state)
    d = get(f"{http}/api/p/{project.slug}/task/001")
    assert d["activity"] == [] and len(d["log"]) == 2 and "done at abc" in d["log"][1]


# ----------------------------------------------------------------------------- export decision

def test_run_timelines_are_exported_only_with_the_logs(project, tmp_path):
    core.Activity(project, "001", "worker").add("tool", name="Bash", input="ls")
    assert "runs/001.jsonl" not in core.export_files(project)
    assert "runs/001.jsonl" in core.export_files(project, include_logs=True)
    assert core._wanted("runs/001.jsonl")  # and accepted back on import
    z = zipfile.ZipFile(core.export_project(project, tmp_path / "x.zip", include_logs=True))
    assert "runs/001.jsonl" in z.namelist()


# ----------------------------------------------------------------------------- the page (source check only)

def test_panel_source_sections_and_no_reset_on_polling():
    html = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")
    i = html.index("async function showDetail")
    js = html[i:html.index("\nfunction toggleGroup", i)]
    for title in ('"Prompt"', '"Orchestrator handoff"', '"Agent work"', '"Agent report"'):
        assert title in js
    assert js.index('"Prompt"') < js.index('"Orchestrator handoff"') < js.index('"Agent work"') < js.index('"Agent report"')
    assert "Not started yet" in js and "detailKey !== key" in js and "t.runs.length" in js
    assert "if (box.dataset.h === html) return;" in js  # unchanged HTML: the DOM (scroll, open sections) is left alone
    assert "detailOpen[t.id" in js and 't.status === "done" ? "report"' in js
    assert "${esc(d.report)}" not in js and "mdBlock(d.report)" in html  # rendered through md(), which escapes
    assert "const mdBlock = s => md(s)" in html
