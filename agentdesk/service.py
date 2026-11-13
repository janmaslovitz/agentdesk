"""One set of operations on AgentDesk, for every way in: the REST API (`/api/v1/...`, server.py), the MCP server
(`python -m agentdesk mcp`, mcp_server.py) and scripts. Everything works on the data folder directly, so it needs
no running dashboard: runs, integrations and the architect start as detached processes (`python -m agentdesk ...`)
that hold the project's lock, and keep going when the caller ends.

Each operation takes JSON-able keyword arguments and returns a JSON-able dict; a bad request raises ApiError (with an
HTTP status). OPERATIONS describes them (name, description, JSON schema), and `call(name, args)` runs one.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from . import core, dag, reliability


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _project(project: str) -> core.Project:
    try:
        return core.project(str(project))
    except KeyError:
        raise ApiError(f"no project {project!r}; list_projects shows them", 404)


def _task(p: core.Project, task_id: str) -> core.Task:
    t = next((t for t in core.load_tasks(p) if t.id == str(task_id)), None)
    if t is None:
        raise ApiError(f"no task {task_id!r} in {p.slug}", 404)
    return t


# ----------------------------------------------------------------------------- background processes

PROCS = {"run": ("runner.pid", "runner.log", "run"), "architect": ("architect.pid", "architect.log", "architect")}


def _pid(p: core.Project, kind: str) -> int:
    try:
        return int(p.path(PROCS[kind][0]).read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def busy(p: core.Project, kind: str = "run") -> bool:
    """A run/integration (kind "run") or the architect (kind "architect") of the project is going: its lock is held."""
    return core.RunLock.held_elsewhere(p, PROCS[kind][2])


def spawn(p: core.Project, args: list[str], kind: str = "run") -> subprocess.Popen:
    """Start `python -m agentdesk <args>` detached (own session / process group), output to the project's log file
    of that kind, pid in its pid file. Refuses while one of that kind is going, or the disk is (nearly) full."""
    if busy(p, kind):
        raise ApiError(f"a {kind} of {p.slug} is already going", 409)
    gate = core.disk_gate()
    if gate:
        raise ApiError(f"not starting: {gate}", 507)
    ok, why = reliability.disk_ok(str(core.DATA), core.autorunner_cfg(p)["min_free_gb"])
    if not ok:
        raise ApiError(f"not enough free disk space ({why})", 507)
    pidf, logf, _ = PROCS[kind]
    reliability.rotate(p.path(logf))
    out = p.path(logf).open("a", encoding="utf-8")
    env = {**os.environ, **core.policy_env(p), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "AGENTDESK_HOME": str(core.DATA)}
    if sys.platform == "win32":
        kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW}
    else:
        kw = {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, *([] if core.FROZEN else ["-m", "agentdesk"]), *args], cwd=core.APP,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, **kw)
    out.close()
    # The pid file is written by the process itself once it holds the lock (see __main__), so a second start that
    # lost the race can't overwrite the pid of the one that runs.
    core.Store(p).log(f"api: started {' '.join(args)} (pid {proc.pid})")
    return proc


def _wait_free(p: core.Project, kind: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not busy(p, kind):
            return True
        time.sleep(0.2)
    return not busy(p, kind)


def stop_process(p: core.Project, kind: str = "run", grace_s: float = 15.0) -> dict:
    """Stop the project's run (or architect): SIGTERM to its process group (the worker CLIs included), SIGKILL after
    `grace_s`. Tasks it left working go back to pending."""
    if not busy(p, kind):
        return {"ok": True, "stopped": False, "note": f"no {kind} is going"}
    pid = _pid(p, kind)
    if pid <= 0:
        raise ApiError(f"the {kind} is going but its pid is unknown; stop it from the dashboard", 409)
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
        if not _wait_free(p, kind, grace_s):
            reliability.kill_group(pid)
    freed = _wait_free(p, kind, 10)
    fixed = []
    if kind == "run" and freed:
        store = core.Store(p)
        state = store.load()
        fixed = core.recover_stale(state, alive=False)
        for tid in fixed:  # a stop is not a crash: don't count it against the task
            e = state["tasks"][tid]
            e.update(crashes=max(0, int(e.get("crashes", 1)) - 1), last_failure="stopped by the user")
        if fixed:
            store.save(state)
        store.log(f"api: {kind} stopped" + (f"; {', '.join(fixed)} back to pending" if fixed else ""))
    return {"ok": freed, "stopped": True, "reset": fixed}


# ----------------------------------------------------------------------------- views

def _run_summary(p: core.Project, state: dict, tasks: list[core.Task]) -> dict:
    counts: dict[str, int] = {}
    v = dag.view(dag.nodes_from_tasks(tasks), state)
    for n in v["nodes"]:
        counts[n["state"]] = counts.get(n["state"], 0) + 1
    cfg = core.run_cfg(p)
    return {"running": busy(p), "architect_running": busy(p, "architect"), "max_parallel": cfg["max_parallel"],
            "counts": counts, "spent_usd": round(float(state.get("spent_usd", 0.0)), 4), "tokens": state.get("tokens", {}),
            "graph_errors": v["errors"], "graph_width": v["width"]}


def list_projects() -> dict:
    out = []
    for p in core.projects():
        state = core.Store(p).load()
        tasks = core.load_tasks(p)
        out.append({"project": p.slug, "name": p.name, "repo": str(p.repo), "base": p.cfg["repo"]["base"],
                    "account": p.account.get("name"), **_run_summary(p, state, tasks)})
    return {"projects": out}


def status(project: str) -> dict:
    """The project's tasks (with derived graph state: ready / waiting / stuck), the run and spending."""
    p = _project(project)
    state = core.Store(p).load()
    tasks = core.load_tasks(p)
    v = dag.view(dag.nodes_from_tasks(tasks), state)
    rows = []
    for n in v["nodes"]:
        e = state["tasks"].get(n["id"], {})
        cost = sum((r.get("cost_usd") or 0) for r in e.get("runs", []))
        rows.append({"id": n["id"], "title": n["title"], "status": n["status"], "state": n["state"], "tier": n["tier"],
                     "effort": e.get("effort"),
                     "depends": n["depends"], "level": n["level"], "cost_usd": round(cost, 4), "branch": e.get("branch"),
                     "commit": e.get("commit"), "last_failure": (e.get("last_failure") or "")[-300:] or None,
                     "question": e.get("question") or None,
                     "started": e.get("started"), "finished": e.get("finished"), "stuck_on": n["stuck_on"]})
    return {"project": p.slug, "tasks": rows, "run": _run_summary(p, state, tasks), "integration": state.get("integration")}


def graph(project: str) -> dict:
    """The task graph: nodes (status, derived state, level, critical-path weight), edges [from, to], errors (cycles,
    unknown dependencies), conflicts (parallel tasks that list the same files) and width (max useful parallelism)."""
    p = _project(project)
    return {"project": p.slug, **dag.view(dag.nodes_from_tasks(core.load_tasks(p)), core.Store(p).load())}


def task(project: str, id: str, activity: int = 30) -> dict:
    """One task: its file, state entry (runs, costs, failures), report and the last `activity` timeline events."""
    p = _project(project)
    t = _task(p, id)
    e = core.Store(p).load()["tasks"].get(t.id, {})
    rep = p.path("reports") / f"{t.id}.md"
    return {"id": t.id, "title": t.title, "body": t.body, "tier": t.tier, "depends": t.depends, "files": t.files,
            "state": e, "report": rep.read_text(encoding="utf-8") if rep.is_file() else None,
            "activity": core.read_activity(p, t.id, max(0, min(int(activity), 500)))}


def log(project: str, lines: int = 80) -> dict:
    p = _project(project)
    f = p.path("agentdesk.log")
    text = f.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(int(lines), 2000)):] if f.is_file() else []
    return {"lines": text}


# ----------------------------------------------------------------------------- tasks and plans

def add_task(project: str, title: str, body: str = "", tier: str | None = None, depends: list | None = None,
             files: list | None = None, id: str | None = None, effort: str | None = None) -> dict:
    """Queue one task. Its dependencies must exist and must not form a cycle."""
    p = _project(project)
    title = str(title or "").strip()
    if not title:
        raise ApiError("a task needs a title")
    tid = str(id or core.next_task_id(p))
    tasks = core.load_tasks(p)
    if any(t.id == tid for t in tasks):
        raise ApiError(f"task {tid} exists already", 409)
    deps = [str(d) for d in depends or []]
    nodes = dag.nodes_from_tasks(tasks) + [dag.Node(tid, deps, [str(f) for f in files or []], tier, title)]
    errors = dag.validate(nodes)
    if errors:
        raise ApiError("; ".join(errors))
    try:
        core.add_task(p, tid, title, str(body or ""), tier or None, deps, [str(f) for f in files or []], effort or None)
    except ValueError as e:
        raise ApiError(str(e))
    conflicts = [c for c in dag.concurrent_conflicts(nodes) if tid in c[:2]]
    return {"ok": True, "id": tid,
            "warnings": [f"{a} and {b} may run in parallel but both touch {', '.join(f[:5])}" for a, b, f in conflicts]}


def plan(project: str) -> dict:
    """The project's plan (draft or approved) with its tasks in dependency order, levels, estimate and warnings."""
    p = _project(project)
    arch = core.read_json(p.path("architect.json"), {})
    return {"plan": core.plan_view(p), "architect": {**arch, "running": busy(p, "architect")} if arch else None}


def propose_plan(project: str, tasks: list, summary: str = "") -> dict:
    """Save a draft plan (a task graph you designed): tasks in order, each {title, goal, body?, files?, tier?, depends?}
    where depends names earlier plan tasks as d1, d2, ... (by position) or ids of queued tasks. Replaces a draft."""
    p = _project(project)
    try:
        old = core.load_plan(p)
        if old and old.get("status") == "draft":
            v = core.update_plan(p, summary or None, tasks)
        else:
            v = core.propose_plan(p, summary, tasks)
    except ValueError as e:
        raise ApiError(str(e))
    return {"ok": True, "plan": core.plan_view(p, v)}


def approve_plan(project: str, start: bool = True, max_parallel: int | None = None) -> dict:
    """Turn the draft plan into queued tasks (new ids, dependencies mapped) and, with start, run the graph."""
    p = _project(project)
    try:
        ids = core.approve_plan(p)
    except ValueError as e:
        raise ApiError(str(e))
    out = {"ok": True, "ids": ids}
    if start:
        out["run"] = run(project, max_parallel=max_parallel, budget_usd=core.plan_budget(p))
    return out


def cancel_plan(project: str) -> dict:
    p = _project(project)
    try:
        return {"ok": True, "removed": core.cancel_plan(p)}
    except ValueError as e:
        raise ApiError(str(e))


def close_plan(project: str) -> dict:
    p = _project(project)
    if busy(p):
        raise ApiError("a run is going: stop it first", 409)
    try:
        core.close_plan(p)
    except ValueError as e:
        raise ApiError(str(e))
    return {"ok": True}


def architect(project: str, goal: str) -> dict:
    """Start the architect in the background: it reads the repository and saves a draft task graph for `goal`.
    Follow it with `plan` (architect.running, then the draft); approve it with approve_plan."""
    p = _project(project)
    goal = str(goal or "").strip()
    if len(goal) < 10:
        raise ApiError("describe the goal (at least a sentence)")
    if core.plan_active(core.load_plan(p)):
        raise ApiError("an approved plan is still going; close_plan first", 409)
    f = p.path("architect.goal.md")
    f.write_text(goal, encoding="utf-8")
    proc = spawn(p, ["architect", p.slug, "--goal-file", str(f)], "architect")
    return {"ok": True, "pid": proc.pid, "note": "the architect is working; poll plan for the draft"}


# ----------------------------------------------------------------------------- runs

def run(project: str, task: str | None = None, max_parallel: int | None = None, max_tasks: int | None = None,
        budget_usd: float | None = None) -> dict:
    """Start a run in the background: the whole task graph (up to max_parallel tasks at once, default [run]
    max_parallel), at most max_tasks of them, or just `task`."""
    p = _project(project)
    args = ["run", p.slug]
    if task:
        _task(p, task)
        args.append(str(task))
    if max_parallel:
        args += ["--parallel", str(max(1, int(max_parallel)))]
    if max_tasks:
        args += ["--max", str(max(1, int(max_tasks)))]
    if budget_usd:
        args += ["--budget", f"{max(0.5, float(budget_usd)):g}"]
    proc = spawn(p, args)
    return {"ok": True, "pid": proc.pid, "args": args}


def stop(project: str) -> dict:
    """Stop the project's run; tasks it was working on go back to pending (their worktrees stay)."""
    return stop_process(_project(project), "run")


def retry(project: str, id: str) -> dict:
    """Put a blocked, paused or crashed task back to pending (its worktree and branch stay). Allowed while a run is
    going: the run takes changes to tasks it isn't working on from disk and starts the task when it's ready."""
    p = _project(project)
    t = _task(p, id)
    store = core.Store(p)
    state = store.load()
    e = state["tasks"].setdefault(t.id, {"title": t.title})
    if e.get("status") == "done":
        raise ApiError(f"{t.id} is done")
    if e.get("status") in ("routing", "working"):
        raise ApiError(f"{t.id} is being worked on right now")
    e.update(status="pending", crashes=0, requeued=core.now_iso())
    e.pop("last_failure", None)
    store.save(state)
    store.log(f"api: {t.id} set back to pending" + (" (the run going now picks it up)" if busy(p) else ""))
    return {"ok": True}


def answer(project: str, id: str, answer: str) -> dict:
    """Answer a task that waits for a decision (status needs_decision): the answer is added to the task as a
    "## Decision" section and the task goes back to the queue, keeping its worktree. Start a run to go on."""
    p = _project(project)
    _task(p, id)
    try:
        core.answer_task(p, str(id), str(answer or ""), "user")
    except KeyError:
        raise ApiError(f"no task {id!r} in {p.slug}", 404)
    except ValueError as e:
        raise ApiError(str(e))
    return {"ok": True, "note": "recorded; the task is pending again (run starts it)"}


def integrate(project: str, wait: bool = False) -> dict:
    """Merge every finished task's branch into auto/integration (dependency order) and run the final checks there.
    In the background by default; poll status (integration). wait=True does it now and returns the result."""
    p = _project(project)
    if not wait:
        proc = spawn(p, ["integrate", p.slug])
        return {"ok": True, "pid": proc.pid, "note": "integrating in the background; status shows the result"}
    lock = core.RunLock(p)
    if not lock.acquire():
        raise ApiError("a run of this project is going; integrate after it", 409)
    try:
        return {"ok": True, **core.integrate(p)}
    finally:
        lock.release()


# ----------------------------------------------------------------------------- registry

@dataclass
class Op:
    name: str
    fn: Callable
    description: str
    schema: dict
    write: bool = False


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_P = {"project": {"type": "string", "description": "project slug (list_projects)"}}
_PLAN_TASK = {"type": "object", "required": ["title", "goal"], "properties": {
    "title": {"type": "string"}, "goal": {"type": "string", "description": "what to change, where, constraints"},
    "body": {"type": "string", "description": "more detail, e.g. acceptance criteria"},
    "files": {"type": "array", "items": {"type": "string"}, "description": "files/folders it touches"},
    "tier": {"type": "string", "enum": core.TIER_ORDER},
    "effort": {"type": "string", "enum": ["low", "medium", "high"], "description": "optional; else the router / the tier decides"},
    "depends": {"type": "array", "items": {"type": "string"}, "description": "d1, d2 ... (earlier plan tasks) or queued task ids"}}}

OPERATIONS: list[Op] = [
    Op("list_projects", list_projects, "List projects with their run state, task counts and spending.", _obj({})),
    Op("status", status, "A project's tasks with status and graph state (ready/waiting/stuck), the run and spending.",
       _obj(_P, ["project"])),
    Op("graph", graph, "A project's task graph: nodes, edges, levels, critical path, cycles, file conflicts, width.",
       _obj(_P, ["project"])),
    Op("task", task, "One task: its text, runs, costs, failures, report and recent agent activity.",
       _obj({**_P, "id": {"type": "string"}, "activity": {"type": "integer", "minimum": 0, "maximum": 500}}, ["project", "id"])),
    Op("log", log, "The last lines of the project's log.",
       _obj({**_P, "lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["project"])),
    Op("add_task", add_task, "Queue one task (checked against the graph: dependencies exist, no cycle).",
       _obj({**_P, "title": {"type": "string"}, "body": {"type": "string", "description": "goal, where, constraints, done when"},
             "tier": {"type": "string", "enum": core.TIER_ORDER}, "depends": {"type": "array", "items": {"type": "string"}},
             "files": {"type": "array", "items": {"type": "string"}}, "id": {"type": "string"},
             "effort": {"type": "string", "enum": ["low", "medium", "high"]}}, ["project", "title"]), True),
    Op("plan", plan, "The project's draft or approved plan, and the architect's state.", _obj(_P, ["project"])),
    Op("propose_plan", propose_plan, "Save a draft plan (a task graph) for approval; replaces an existing draft.",
       _obj({**_P, "summary": {"type": "string"}, "tasks": {"type": "array", "items": _PLAN_TASK}}, ["project", "tasks"]), True),
    Op("approve_plan", approve_plan, "Queue the draft plan's tasks and (start=true) run the graph in parallel.",
       _obj({**_P, "start": {"type": "boolean"}, "max_parallel": {"type": "integer", "minimum": 1, "maximum": 32}}, ["project"]), True),
    Op("cancel_plan", cancel_plan, "Drop the draft plan.", _obj(_P, ["project"]), True),
    Op("close_plan", close_plan, "Forget an approved/finished plan (its tasks stay queued).", _obj(_P, ["project"]), True),
    Op("architect", architect, "Start the architect: it reads the repo and drafts a parallel task graph for the goal.",
       _obj({**_P, "goal": {"type": "string"}}, ["project", "goal"]), True),
    Op("run", run, "Start a background run of the task graph (parallel), or of one task.",
       _obj({**_P, "task": {"type": "string"}, "max_parallel": {"type": "integer", "minimum": 1, "maximum": 32},
             "max_tasks": {"type": "integer", "minimum": 1}, "budget_usd": {"type": "number", "minimum": 0.5,
             "description": "this run's budget (default [limits] run_budget_usd)"}}, ["project"]), True),
    Op("stop", stop, "Stop the project's run; its working tasks go back to pending.", _obj(_P, ["project"]), True),
    Op("retry", retry, "Put a blocked/paused/crashed task back to pending.", _obj({**_P, "id": {"type": "string"}}, ["project", "id"]), True),
    Op("answer", answer, "Answer a task waiting for a decision (needs_decision); it goes back to the queue with the answer.",
       _obj({**_P, "id": {"type": "string"}, "answer": {"type": "string"}}, ["project", "id", "answer"]), True),
    Op("integrate", integrate, "Merge all finished branches into auto/integration and run the final checks.",
       _obj({**_P, "wait": {"type": "boolean"}}, ["project"]), True),
]
BY_NAME = {o.name: o for o in OPERATIONS}


def call(name: str, args: dict | None = None) -> dict:
    """Run operation `name` with `args` (unknown keys refused). Raises ApiError."""
    op = BY_NAME.get(name)
    if op is None:
        raise ApiError(f"unknown operation {name!r}", 404)
    args = dict(args or {})
    allowed = set(op.schema.get("properties", {}))
    extra = set(args) - allowed
    if extra:
        raise ApiError(f"{name}: unknown argument(s) {', '.join(sorted(extra))}")
    missing = [k for k in op.schema.get("required", []) if args.get(k) in (None, "")]
    if missing:
        raise ApiError(f"{name}: missing {', '.join(missing)}")
    try:
        return op.fn(**args)
    except ApiError:
        raise
    except (ValueError, TypeError) as e:
        raise ApiError(f"{name}: {e}")


# ----------------------------------------------------------------------------- REST mapping (server.py serves it)

def _q_int(q: dict, k: str) -> dict:
    v = q.get(k)
    return {k: int(v)} if v not in (None, "") and str(v).isdigit() else {}


def rest(method: str, parts: list[str], query: dict, body: dict) -> tuple[int, dict]:
    """/api/v1/... → (HTTP status, JSON). `parts` is the path after /api/v1, split on '/'."""
    try:
        if method == "GET" and not parts:
            return 200, {"api": "agentdesk", "version": 1, "operations": [
                {"name": o.name, "description": o.description, "schema": o.schema, "write": o.write} for o in OPERATIONS],
                "rpc": "POST /api/v1/call/<operation> with the arguments as a JSON object"}
        if method == "POST" and len(parts) == 2 and parts[0] == "call":
            return 200, call(parts[1], body)
        if parts[:1] != ["projects"]:
            raise ApiError("not found", 404)
        if method == "GET" and len(parts) == 1:
            return 200, list_projects()
        slug = parts[1] if len(parts) > 1 else ""
        rest_ = parts[2:]
        if method == "GET":
            if not rest_:
                return 200, status(slug)
            if rest_ == ["graph"]:
                return 200, graph(slug)
            if rest_ == ["plan"]:
                return 200, plan(slug)
            if rest_ == ["log"]:
                return 200, log(slug, **_q_int(query, "lines"))
            if len(rest_) == 2 and rest_[0] == "tasks":
                return 200, task(slug, rest_[1], **_q_int(query, "activity"))
        if method == "POST":
            if rest_ == ["tasks"]:
                return 200, call("add_task", {**body, "project": slug})
            if len(rest_) == 3 and rest_[0] == "tasks" and rest_[2] == "retry":
                return 200, retry(slug, rest_[1])
            if len(rest_) == 3 and rest_[0] == "tasks" and rest_[2] == "answer":
                return 200, call("answer", {**body, "project": slug, "id": rest_[1]})
            if rest_ == ["plan"]:
                return 200, call("propose_plan", {**body, "project": slug})
            if len(rest_) == 2 and rest_[0] == "plan" and rest_[1] in ("approve", "cancel", "close"):
                return 200, call(f"{rest_[1]}_plan", {**body, "project": slug})
            if rest_ in (["architect"], ["run"], ["stop"], ["integrate"]):
                return 200, call(rest_[0], {**body, "project": slug})
        raise ApiError("not found", 404)
    except ApiError as e:
        return e.status, {"error": str(e)}
