"""The task graph, the parallel orchestrator, dependency merges, integration, the service layer, REST and MCP."""

import asyncio
import json
import subprocess

import pytest

from agentdesk import core, dag, mcp_server, service

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def N(i, deps=(), files=(), tier=None):
    return dag.Node(i, list(deps), list(files), tier, f"task {i}")


def S(**tasks):
    return {"tasks": {k: dict(v) for k, v in tasks.items()}}


# ----------------------------------------------------------------------------- pure graph logic

def test_cycles_and_unknown_dependencies():
    assert dag.find_cycle([N("a"), N("b", ["a"])]) is None
    cyc = dag.find_cycle([N("a", ["c"]), N("b", ["a"]), N("c", ["b"])])
    assert cyc[0] == cyc[-1] and set(cyc) == {"a", "b", "c"}
    errs = dag.validate([N("a", ["zz"]), N("b", ["b"]), N("a")])
    assert any("used twice" in e for e in errs) and any("zz" in e for e in errs) and any("itself" in e for e in errs)
    assert dag.validate([N("b", ["old"])], known_done=["old"]) == []
    with pytest.raises(dag.GraphError):
        dag.topo_order([N("a", ["b"]), N("b", ["a"])])


def test_ready_prefers_the_critical_path():
    # a → b → c (hard chain) and d (alone, mechanical): a goes first even though d is listed first
    nodes = [N("d", tier="mechanical"), N("a", tier="standard"), N("b", ["a"], tier="hard"), N("c", ["b"])]
    assert dag.ready(nodes, S()) == ["a", "d"]
    assert dag.ready(nodes, S(a={"status": "done"})) == ["b", "d"]
    assert dag.ready(nodes, S(a={"status": "working"})) == ["d"]
    assert dag.ready(nodes, S(d={"status": "paused"}), skip=["a"]) == ["d"]  # paused goes on in the next run
    assert dag.levels(nodes) == {"d": 0, "a": 0, "b": 1, "c": 2}
    assert dag.width(nodes) == 2


def test_stuck_and_view():
    nodes = [N("a"), N("b", ["a"]), N("c", ["b"]), N("d")]
    st = S(a={"status": "blocked"}, d={"status": "done"})
    assert dag.stuck(nodes, st) == {"b": "a", "c": "a"}
    v = dag.view(nodes, st)
    states = {n["id"]: n["state"] for n in v["nodes"]}
    assert states == {"a": "blocked", "b": "stuck", "c": "stuck", "d": "done"}
    assert ["a", "b"] in v["edges"] and v["errors"] == []


def test_conflicts_only_between_parallel_tasks():
    nodes = [N("a", files=["src/api/"]), N("b", files=["src/api/users.py"]), N("c", ["a"], files=["src/api/users.py"]),
             N("d", files=["README.md"])]
    pairs = {(a, b) for a, b, _ in dag.concurrent_conflicts(nodes)}
    assert pairs == {("a", "b"), ("b", "c")}  # a/c are ordered; d touches nothing shared
    assert dag.overlaps(["SRC\\Api\\users.py"], ["src/api"]) == ["SRC\\Api\\users.py"]


def test_plan_warnings_flag_parallel_overlaps(project):
    plan = core.propose_plan(project, "s", [
        {"title": "A", "goal": "x", "files": ["app/main.py"]},
        {"title": "B", "goal": "y", "files": ["app/main.py"]},
        {"title": "C", "goal": "z", "files": ["app/main.py"], "depends": ["d1", "d2"]}])
    assert len(plan["warnings"]) == 1 and "d1 and d2" in plan["warnings"][0]
    v = core.plan_view(project)
    assert v["width"] == 2 and [t["level"] for t in v["tasks"]] == [0, 0, 1]
    ids = core.approve_plan(project)
    t = next(t for t in core.load_tasks(project) if t.id == ids["d3"])
    assert t.files == ["app/main.py"] and t.depends == [ids["d1"], ids["d2"]]


# ----------------------------------------------------------------------------- the orchestrator

def add(p, tid, deps=(), tier="mechanical"):
    core.add_task(p, tid, f"task {tid}", "do it", tier, list(deps))


def fake_tasks(monkeypatch, outcome=None, delay=0.05):
    """do_task that takes `delay` seconds and ends with outcome.get(id, "done"); records the overlap."""
    seen = {"order": [], "max": 0, "now": 0}

    async def fake(p, store, task, meter):
        seen["now"] += 1
        seen["max"] = max(seen["max"], seen["now"])
        seen["order"].append(task.id)
        meter.state["tasks"].setdefault(task.id, {})["status"] = "working"
        await asyncio.sleep(delay)
        status = (outcome or {}).get(task.id, "done")
        if status == "crash":
            seen["now"] -= 1
            raise RuntimeError("boom")
        meter.state["tasks"][task.id].update(status=status, branch=f"auto/{task.id}")
        store.save(meter.state)
        seen["now"] -= 1

    monkeypatch.setattr(core, "do_task", fake)
    return seen


def test_runs_independent_tasks_in_parallel_up_to_the_limit(project, monkeypatch):
    for i in ("1", "2", "3", "4", "5"):
        add(project, i)
    seen = fake_tasks(monkeypatch)
    res = asyncio.run(core.run_dag(project, max_parallel=3))
    assert seen["max"] == 3 and sorted(res["done"]) == ["1", "2", "3", "4", "5"]


def test_dependencies_order_and_blocked_branch(project, monkeypatch):
    # 1 → 2 → 4 ; 1 → 3 (blocked) → 5 ; 6 alone
    add(project, "1"); add(project, "2", ["1"]); add(project, "3", ["1"]); add(project, "4", ["2"])
    add(project, "5", ["3"]); add(project, "6")
    seen = fake_tasks(monkeypatch, {"3": "blocked"})
    res = asyncio.run(core.run_dag(project, max_parallel=4))
    o = seen["order"]
    assert o.index("1") < o.index("2") < o.index("4") and o.index("1") < o.index("3")
    assert "5" not in o and res["stuck"] == ["5"] and res["blocked"] == ["3"]
    assert sorted(res["done"]) == ["1", "2", "4", "6"]


def test_a_crash_ends_one_task_not_the_run(project, monkeypatch):
    add(project, "1"); add(project, "2"); add(project, "3", ["2"])
    fake_tasks(monkeypatch, {"1": "crash"})
    res = asyncio.run(core.run_dag(project, max_parallel=2))
    e = core.Store(project).load()["tasks"]["1"]
    assert e["status"] == "pending" and e["crashes"] == 1 and "boom" in e["last_failure"]
    assert sorted(res["done"]) == ["2", "3"] and res["started"].count("1") == 1


def test_auto_skips_flagged_and_max_tasks(project, monkeypatch):
    add(project, "1"); add(project, "2"); add(project, "3")
    st = core.Store(project).load()
    st["tasks"]["1"] = {"status": "pending", "last_failure": "$ pytest\nFAILED"}
    core.Store(project).save(st)
    seen = fake_tasks(monkeypatch)
    asyncio.run(core.run_dag(project, max_parallel=3, auto=True, max_tasks=1))
    assert seen["order"] == ["2"]


def test_budget_stops_new_starts(project, monkeypatch):
    add(project, "1"); add(project, "2")
    monkeypatch.setattr(core.Meter, "stop_reason", lambda self: "run budget 1 USD spent")
    seen = fake_tasks(monkeypatch)
    res = asyncio.run(core.run_dag(project, max_parallel=2))
    assert seen["order"] == [] and res["stopped"]


def test_run_lock_is_exclusive(project):
    a = core.RunLock(project)
    assert a.acquire()
    try:
        assert core.RunLock.held_elsewhere(project)
        assert not core.RunLock(project).acquire()
        assert not service.busy(project, "architect")
    finally:
        a.release()
    assert not core.RunLock.held_elsewhere(project)


def test_shared_usage_is_split_by_cost():
    st = S(a={"runs": [{"cost_usd": 3.0, "at": "2026-01-01T10:05:00"}]}, b={"runs": [{"cost_usd": 1.0, "at": "2026-01-01T10:06:00"}]})
    core.share_usage(st, ["a", "b"], {"utilization": 0.10, "at": "2026-01-01T10:00:00"}, {"utilization": 0.18, "at": "2026-01-01T11:00:00"})
    assert st["tasks"]["a"]["usage_share_pct"] == 6.0 and st["tasks"]["b"]["usage_share_pct"] == 2.0
    assert st["last_run_usage"]["used_pct"] == 8.0


def test_overlapping_calls_mark_usage_shared(project, monkeypatch):
    m = core.Meter(project, core.Store(project), core.Store(project).load())
    monkeypatch.setattr(m, "mark", lambda: None)
    one = m.begin()
    assert "usage_shared" not in m.end(one)
    a = m.begin()
    b = m.begin()
    assert m.end(a)["usage_shared"] and m.end(b)["usage_shared"] and m.active == 0
    c = m.begin()
    m.release(c)
    m.release(c)
    assert m.active == 0


# ----------------------------------------------------------------------------- git: dependency merges, integration

def commit_on(p, branch, base, files: dict, msg):
    """Make `branch` from `base` with `files` written and committed (in a throwaway worktree)."""
    wt = p.repo.parent / f"wt-{branch.replace('/', '-')}"
    subprocess.run(["git", "worktree", "add", "-q", "-b", branch, str(wt), base], cwd=p.repo, check=True)
    for name, text in files.items():
        (wt / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", msg], cwd=wt, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=p.repo, check=True)


def mark_done(p, *ids):
    st = core.Store(p).load()
    for i in ids:
        st["tasks"][i] = {"status": "done", "branch": f"auto/{i}"}
    core.Store(p).save(st)


def ident(p):
    subprocess.run(["git", "config", "user.name", "t"], cwd=p.repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=p.repo, check=True)


def test_worktree_gets_every_dependency(project):
    ident(project)
    commit_on(project, "auto/a", "main", {"a.txt": "A"}, "a")
    commit_on(project, "auto/b", "main", {"b.txt": "B"}, "b")
    add(project, "a"); add(project, "b"); add(project, "c", ["a", "b"])
    mark_done(project, "a", "b")
    c = next(t for t in core.load_tasks(project) if t.id == "c")
    st = core.Store(project).load()
    wt = core.ensure_worktree(project, core.Store(project), c, st)
    assert (wt / "a.txt").read_text() == "A" and (wt / "b.txt").read_text() == "B"
    assert core.worktree_conflicts(project, c, wt, st) == ""


def test_conflicting_dependencies_are_left_for_the_worker(project):
    ident(project)
    commit_on(project, "auto/a", "main", {"x.txt": "from a\n"}, "a")
    commit_on(project, "auto/b", "main", {"x.txt": "from b\n"}, "b")
    add(project, "a"); add(project, "b"); add(project, "c", ["a", "b"])
    mark_done(project, "a", "b")
    c = next(t for t in core.load_tasks(project) if t.id == "c")
    st = core.Store(project).load()
    wt = core.ensure_worktree(project, core.Store(project), c, st)
    assert core.unmerged_files(wt) == ["x.txt"]
    pre = core.worktree_conflicts(project, c, wt, st)
    assert "x.txt" in pre and "Resolve" in pre
    m = core.Meter(project, core.Store(project), st)
    ok, why = asyncio.run(core.check_worktree(project, core.Store(project), c, wt, m))
    assert not ok and why.startswith("The checks failed") and core.classify_failure(why) == "gate"
    (wt / "x.txt").write_text("from a and b\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.txt"], cwd=wt, check=True)
    assert asyncio.run(core.check_worktree(project, core.Store(project), c, wt, m))[0]
    core.commit(project, c, wt, "standard")
    log = subprocess.run(["git", "log", "--format=%P", "-1"], cwd=wt, capture_output=True, text=True).stdout.split()
    assert len(log) == 2  # the merge is completed by the task's commit


def test_integration_merges_in_order_and_skips_conflicts(project):
    ident(project)
    commit_on(project, "auto/a", "main", {"a.txt": "A"}, "a")
    commit_on(project, "auto/b", "auto/a", {"b.txt": "B"}, "b")
    commit_on(project, "auto/c", "main", {"a.txt": "other"}, "c")   # conflicts with a
    commit_on(project, "auto/d", "auto/c", {"d.txt": "D"}, "d")     # builds on c
    add(project, "a"); add(project, "b", ["a"]); add(project, "c"); add(project, "d", ["c"])
    mark_done(project, "a", "b", "c", "d")
    res = core.integrate(project)
    assert res["merged"] == ["a", "b"]
    assert {s["task"] for s in res["skipped"]} == {"c", "d"} and res["skipped"][0]["files"] == ["a.txt"]
    wt = core.Path(res["worktree"])
    assert (wt / "b.txt").exists() and not (wt / "d.txt").exists()
    assert "Integration" in (project.path("reports") / "integration.md").read_text()
    again = core.integrate(project)  # made fresh from the base each time
    assert again["merged"] == ["a", "b"]
    assert core.worktree_cap_reason(project, "zz") is None


# ----------------------------------------------------------------------------- service, REST, MCP

def test_service_add_task_checks_the_graph(project):
    s = project.slug
    assert service.call("add_task", {"project": s, "title": "one", "id": "1", "files": ["m.py"]})["id"] == "1"
    with pytest.raises(service.ApiError, match="not a task"):
        service.call("add_task", {"project": s, "title": "two", "depends": ["9"]})
    r = service.call("add_task", {"project": s, "title": "two", "files": ["m.py"]})
    assert r["warnings"] and "m.py" in r["warnings"][0]
    with pytest.raises(service.ApiError, match="unknown argument"):
        service.call("status", {"project": s, "bogus": 1})
    with pytest.raises(service.ApiError) as e:
        service.call("status", {"project": "nope"})
    assert e.value.status == 404
    st = service.call("status", {"project": s})
    assert [t["state"] for t in st["tasks"]] == ["ready", "ready"] and st["run"]["running"] is False
    g = service.call("graph", {"project": s})
    assert g["conflicts"] and g["width"] == 2


def test_service_plan_flow_without_starting(project):
    s = project.slug
    service.call("propose_plan", {"project": s, "summary": "x", "tasks": [{"title": "A", "goal": "a"},
                                                                       {"title": "B", "goal": "b", "depends": ["d1"]}]})
    service.call("propose_plan", {"project": s, "tasks": [{"title": "A2", "goal": "a"}]})  # replaces the draft
    assert [t["title"] for t in service.call("plan", {"project": s})["plan"]["tasks"]] == ["A2"]
    r = service.call("approve_plan", {"project": s, "start": False})
    assert list(r["ids"]) == ["d1"] and "run" not in r
    service.call("close_plan", {"project": s})


def test_service_retry(project):
    add(project, "1")
    st = core.Store(project).load()
    st["tasks"]["1"] = {"status": "blocked", "last_failure": "x", "crashes": 2}
    core.Store(project).save(st)
    service.call("retry", {"project": project.slug, "id": "1"})
    e = core.Store(project).load()["tasks"]["1"]
    assert e["status"] == "pending" and "last_failure" not in e and e["crashes"] == 0


def test_rest_routes(project):
    s = project.slug
    code, idx = service.rest("GET", [], {}, {})
    assert code == 200 and any(o["name"] == "run" for o in idx["operations"])
    assert service.rest("POST", ["projects", s, "tasks"], {}, {"title": "t", "id": "7"})[0] == 200
    code, st = service.rest("GET", ["projects", s], {}, {})
    assert code == 200 and st["tasks"][0]["id"] == "7"
    assert service.rest("GET", ["projects", s, "tasks", "7"], {"activity": "5"}, {})[1]["title"] == "t"
    assert service.rest("GET", ["projects", s, "graph"], {}, {})[1]["nodes"][0]["state"] == "ready"
    assert service.rest("POST", ["call", "status"], {}, {"project": s})[0] == 200
    assert service.rest("GET", ["projects", "nope"], {}, {})[0] == 404
    assert service.rest("GET", ["weird"], {}, {})[0] == 404
    assert service.rest("POST", ["projects", s, "tasks"], {}, {"title": "x", "depends": ["404"]})[0] == 400


def test_rest_over_http(project):
    from agentdesk import server
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
        req = urllib.request.Request(f"{base}/projects/{project.slug}/tasks", data=json.dumps({"title": "via http"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        got = json.loads(urllib.request.urlopen(f"{base}/projects/{project.slug}").read())
        assert got["tasks"][0]["title"] == "via http"
    finally:
        srv.shutdown()


def test_mcp_protocol(project, capsys):
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"architect", "approve_plan", "run", "graph", "integrate"} <= names
    r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "add_task", "arguments": {"project": project.slug, "title": "from mcp"}}})["result"]
    assert not r["isError"] and r["structuredContent"]["ok"]
    bad = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "status", "arguments": {"project": "nope"}}})["result"]
    assert bad["isError"]
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601
    assert "from mcp" not in capsys.readouterr().out  # tool output never leaks onto the protocol channel


def test_mcp_stdio_end_to_end(project):
    import os
    import sys
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}}]
    out = subprocess.run([sys.executable, "-m", "agentdesk", "mcp"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                         capture_output=True, text=True, timeout=60, cwd=core.APP, env={**os.environ, "AGENTDESK_HOME": str(core.DATA)})
    lines = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
    assert [l["id"] for l in lines] == [1, 2]
    assert any(p["project"] == project.slug for p in lines[1]["result"]["structuredContent"]["projects"])


def test_architect_review(project):
    raw = {"summary": "s", "tasks": [
        {"title": "core", "goal": "g", "done_when": ["tests pass"], "files": ["app/core.py"], "depends": []},
        {"title": "x", "goal": "g", "done_when": [], "files": ["app/x.py"], "depends": ["d1"]}]}
    norm, problems, warnings = core.architect_review(project, raw)
    assert norm and problems == ["task d2 (x) has no acceptance criteria (done_when)"] and warnings == []
    raw["tasks"][1]["done_when"] = ["ok"]
    raw["tasks"][1]["files"] = ["app/core.py"]
    raw["tasks"][1]["depends"] = []
    norm, problems, warnings = core.architect_review(project, raw)
    assert not problems and warnings and "d1 and d2" in warnings[0]
    assert "## Done when\n- tests pass" in norm[0]["body"]
    assert core.architect_review(project, {"tasks": [{"title": "a", "goal": "b", "depends": ["d2"]}]})[1]


def test_architect_loop_fixes_its_plan(project, monkeypatch):
    """A fake SDK client: the first answer has two parallel tasks on one file, the second fixes it."""
    answers = [
        {"summary": "s", "tasks": [{"title": "a", "goal": "g", "done_when": ["x"], "files": ["m.py"], "depends": []},
                                   {"title": "b", "goal": "g", "done_when": ["y"], "files": ["m.py"], "depends": []}]},
        {"summary": "s", "tasks": [{"title": "a", "goal": "g", "done_when": ["x"], "files": ["m.py"], "depends": []},
                                   {"title": "b", "goal": "g", "done_when": ["y"], "files": ["m.py"], "depends": ["d1"]}]}]
    prompts = []
    core._load_sdk()

    class Client:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, text):
            prompts.append(text)

        async def receive_response(self):
            yield core.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                     session_id="s", total_cost_usd=0.5, usage={}, result="",
                                     structured_output=answers[len(prompts) - 1])

    monkeypatch.setattr(core, "ClaudeSDKClient", Client)
    v = asyncio.run(core.architect(project, "Build the thing"))
    assert len(prompts) == 2 and "m.py" in prompts[1]
    assert v["by"] == "architect" and v["warnings"] == [] and v["tasks"][1]["depends"] == ["d1"]
    assert core.load_plan(project)["cost_usd"] == 1.0


def test_integration_uses_a_branch_that_resolved_its_dependencies(project):
    """a and b conflict; c depends on both and resolved it: integrating c brings a and b in, no conflict."""
    ident(project)
    commit_on(project, "auto/a", "main", {"x.txt": "from a\n"}, "a")
    commit_on(project, "auto/b", "main", {"x.txt": "from b\n"}, "b")
    add(project, "a"); add(project, "b"); add(project, "c", ["a", "b"])
    mark_done(project, "a", "b")
    c = next(t for t in core.load_tasks(project) if t.id == "c")
    wt = core.ensure_worktree(project, core.Store(project), c, core.Store(project).load())
    (wt / "x.txt").write_text("a and b\n", encoding="utf-8")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\0junk")
    core.commit(project, c, wt, "standard")
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "auto/c"], cwd=project.repo, capture_output=True, text=True).stdout
    assert "__pycache__" not in files  # build junk is never committed
    mark_done(project, "c")
    res = core.integrate(project, final=False)
    assert res["merged"] == ["a", "b", "c"] and res["skipped"] == [] and res["via"] == {"a": "c", "b": "c"}
    assert (core.Path(res["worktree"]) / "x.txt").read_text() == "a and b\n"


# ----------------------------------------------------------------------------- fixes after the FleetPulse benchmark

def test_worktree_cap_counts_only_unfinished_tasks(project):
    p = project
    w = core.Path(p.cfg["repo"]["worktrees"])
    for tid in ("a", "b"):
        core.add_worktree(p, w / tid, f"auto/{tid}", "main")
    text = core._set_toml_key(core._project_text(p), "hygiene", "max_worktrees", "2")
    core._write_project(p, text)
    p = core.project(p.slug)
    assert "max_worktrees = 2" in core.worktree_cap_reason(p, "c", {"tasks": {}})
    # a done task's worktree waits for hygiene / the merge: it must not hold a slot
    assert core.worktree_cap_reason(p, "c", {"tasks": {"a": {"status": "done"}}}) is None


def test_run_reports_ready_tasks_it_could_not_start(project, monkeypatch):
    add(project, "1"); add(project, "2")
    monkeypatch.setattr(core, "worktree_cap_reason", lambda p, tid, state=None: "limit reached" if tid == "2" else None)
    fake_tasks(monkeypatch)
    res = asyncio.run(core.run_dag(project, max_parallel=4))
    assert res["done"] == ["1"] and res["not_started"] == ["2"]
    assert "were not started: 2 (limit reached)" in project.path("agentdesk.log").read_text()


def test_default_parallelism():
    assert core.tomllib.loads(core.DEFAULT_SETTINGS)["run"]["max_parallel"] == 8


def test_foreign_failures_names_tests_the_task_did_not_touch(project):
    ident(project)
    (project.repo / "tests").mkdir()
    (project.repo / "tests" / "test_core.py").write_text("def test_x(): pass\n")
    subprocess.run(["git", "add", "-A"], cwd=project.repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "core"], cwd=project.repo, check=True)
    add(project, "t")
    t = next(x for x in core.load_tasks(project) if x.id == "t")
    wt = core.ensure_worktree(project, core.Store(project), t, core.Store(project).load())
    (wt / "tests" / "test_mine.py").write_text("def test_y(): assert 0\n")
    failure = ("FAILED tests/test_core.py::test_load_collectors_empty - assert [x] == []\n"
               "FAILED tests/test_mine.py::test_y - assert 0\n")
    assert core.foreign_failures(wt, "main", failure) == ["tests/test_core.py"]
    assert core.foreign_failures(wt, "main", "no file names here") == []


def test_prompts_carry_the_test_hygiene_rules():
    assert "never that a registry" in core.WORKER_PROMPT and "outgrown" in core.WORKER_PROMPT
    assert "false" in core.ARCHITECT_PROMPT and "positives" in core.ARCHITECT_PROMPT
