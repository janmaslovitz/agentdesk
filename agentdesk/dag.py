"""The task graph: which tasks may run now, in what order, and what is wrong with a graph.

Pure logic over plain data (task ids, their `depends`, optional `files`, and the per-task status from state.json), so
the scheduler (core.run_dag), the architect's plan check, the dashboard and the API all agree on one answer.

Statuses (state.json `tasks.<id>.status`): pending (or missing), routing, working, done, blocked, paused.
Derived here, never stored: ready (pending, every dependency done), waiting (a dependency is not done yet) and
stuck (a dependency, direct or not, is blocked: the task can't start until someone unblocks it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

ACTIVE = ("routing", "working")
RUNNABLE = ("pending", "paused")  # paused: the run budget stopped it; the next run goes on with it
TIER_WEIGHT = {"mechanical": 1.0, "standard": 3.0, "hard": 8.0}  # rough relative effort, for the critical path
DEFAULT_WEIGHT = TIER_WEIGHT["standard"]


@dataclass
class Node:
    id: str
    depends: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    tier: str | None = None
    title: str = ""


class GraphError(ValueError):
    """The graph can't be scheduled (a cycle, a dependency on an unknown task)."""


def nodes_from_tasks(tasks: Iterable) -> list[Node]:
    """Nodes from core.Task objects (or anything with id/depends and optional files/tier/title)."""
    return [Node(str(t.id), [str(d) for d in t.depends], [str(f) for f in getattr(t, "files", None) or []],
                 getattr(t, "tier", None), getattr(t, "title", "")) for t in tasks]


def status_of(state: dict, tid: str) -> str:
    return str(state.get("tasks", {}).get(tid, {}).get("status") or "pending")


def find_cycle(nodes: list[Node]) -> list[str] | None:
    """One dependency cycle as a list of ids (first id repeated at the end), or None. Unknown ids are ignored."""
    deps = {n.id: [d for d in n.depends] for n in nodes}
    color: dict[str, int] = {}  # 1 = on the stack, 2 = finished
    for start in deps:
        if color.get(start):
            continue
        stack = [(start, iter(deps[start]))]
        path = [start]
        color[start] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                path.pop()
                color[node] = 2
                continue
            if nxt not in deps:
                continue
            if color.get(nxt) == 1:
                return path[path.index(nxt):] + [nxt]
            if not color.get(nxt):
                color[nxt] = 1
                stack.append((nxt, iter(deps[nxt])))
                path.append(nxt)
    return None


def validate(nodes: list[Node], known_done: Iterable[str] = ()) -> list[str]:
    """Problems that make the graph unschedulable: duplicate ids, unknown dependencies, self-dependencies, cycles.
    `known_done`: ids outside `nodes` that may be depended on (e.g. tasks finished long ago)."""
    errors = []
    ids = [n.id for n in nodes]
    seen = set()
    for i in ids:
        if i in seen:
            errors.append(f"task id {i} is used twice")
        seen.add(i)
    extra = set(known_done)
    for n in nodes:
        for d in n.depends:
            if d == n.id:
                errors.append(f"{n.id} depends on itself")
            elif d not in seen and d not in extra:
                errors.append(f"{n.id} depends on {d}, which is not a task")
    cyc = find_cycle(nodes)
    if cyc:
        errors.append("dependency cycle: " + " → ".join(cyc))
    return errors


def check(nodes: list[Node], known_done: Iterable[str] = ()) -> None:
    errors = validate(nodes, known_done)
    if errors:
        raise GraphError("; ".join(errors))


def topo_order(nodes: list[Node]) -> list[str]:
    """Ids so that every task comes after its dependencies; ties keep the given order. Raises GraphError on a cycle."""
    cyc = find_cycle(nodes)
    if cyc:
        raise GraphError("dependency cycle: " + " → ".join(cyc))
    ids = {n.id for n in nodes}
    left = list(nodes)
    done: set[str] = set()
    out = []
    while left:
        for n in left:
            if all(d in done or d not in ids for d in n.depends):
                out.append(n.id)
                done.add(n.id)
                left.remove(n)
                break
    return out


def levels(nodes: list[Node]) -> dict[str, int]:
    """Depth of each task: 0 for tasks with no dependency in the graph, else 1 + the deepest dependency."""
    by_id = {n.id: n for n in nodes}
    out: dict[str, int] = {}
    for tid in topo_order(nodes):
        out[tid] = 1 + max((out[d] for d in by_id[tid].depends if d in out), default=-1)
    return out


def weight(n: Node) -> float:
    return TIER_WEIGHT.get(n.tier or "", DEFAULT_WEIGHT)


def critical_path(nodes: list[Node], done: Iterable[str] = ()) -> dict[str, float]:
    """For each task, the weight of the heaviest chain from it to the end of the graph (itself included). Done tasks
    weigh 0. The scheduler starts the ready task with the largest value first: it is what the whole run waits on."""
    finished = set(done)
    by_id = {n.id: n for n in nodes}
    dependents: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for d in n.depends:
            if d in dependents:
                dependents[d].append(n.id)
    out: dict[str, float] = {}
    for tid in reversed(topo_order(nodes)):
        own = 0.0 if tid in finished else weight(by_id[tid])
        out[tid] = own + max((out[c] for c in dependents[tid]), default=0.0)
    return out


def descendants(nodes: list[Node], roots: Iterable[str]) -> set[str]:
    """Every task that depends, directly or not, on one of `roots` (roots themselves excluded)."""
    dependents: dict[str, list[str]] = {}
    for n in nodes:
        for d in n.depends:
            dependents.setdefault(d, []).append(n.id)
    out: set[str] = set()
    todo = list(roots)
    while todo:
        for c in dependents.get(todo.pop(), []):
            if c not in out:
                out.add(c)
                todo.append(c)
    return out


def ancestors(nodes: list[Node], tid: str) -> set[str]:
    by_id = {n.id: n for n in nodes}
    out: set[str] = set()
    todo = list(by_id[tid].depends) if tid in by_id else []
    while todo:
        d = todo.pop()
        if d in out:
            continue
        out.add(d)
        if d in by_id:
            todo.extend(by_id[d].depends)
    return out


def stuck(nodes: list[Node], state: dict) -> dict[str, str]:
    """Not-done tasks that can't start because a dependency (direct or not) is blocked: {task id: blocked id}."""
    blocked = [n.id for n in nodes if status_of(state, n.id) == "blocked"]
    out: dict[str, str] = {}
    for b in blocked:
        for d in descendants(nodes, [b]):
            if status_of(state, d) != "done":
                out.setdefault(d, b)
    return out


def ready(nodes: list[Node], state: dict, skip: Iterable[str] = ()) -> list[str]:
    """Pending tasks whose dependencies are all done, most urgent first (critical path, then graph order).
    A dependency outside the graph counts as done only if state.json says so. `skip`: ids not to offer."""
    skip = set(skip)
    done = {tid for tid, e in state.get("tasks", {}).items() if e.get("status") == "done"}
    cand = [n for n in nodes if n.id not in skip and status_of(state, n.id) in RUNNABLE
            and all(d in done for d in n.depends)]
    if not cand:
        return []
    try:
        cp = critical_path(nodes, done)
        order = {tid: i for i, tid in enumerate(topo_order(nodes))}
    except GraphError:
        cp, order = {}, {n.id: i for i, n in enumerate(nodes)}
    cand.sort(key=lambda n: (-cp.get(n.id, 0.0), order.get(n.id, 0)))
    return [n.id for n in cand]


def _norm(f: str) -> str:
    return f.replace("\\", "/").strip().strip("/").lower()


def overlaps(a: Iterable[str], b: Iterable[str]) -> list[str]:
    """Paths in both lists; a folder ("src/api/" or "src/api") overlaps every path below it."""
    out = []
    bs = [_norm(x) for x in b if _norm(x)]
    for x in a:
        nx = _norm(x)
        if not nx:
            continue
        for y in bs:
            if nx == y or nx.startswith(y + "/") or y.startswith(nx + "/"):
                out.append(x)
                break
    return out


def concurrent_conflicts(nodes: list[Node]) -> list[tuple[str, str, list[str]]]:
    """Pairs of tasks that may run at the same time (neither depends on the other, directly or not) and declare
    files in common: their branches will likely conflict when merged. [(a, b, shared files)]."""
    anc = {n.id: ancestors(nodes, n.id) for n in nodes}
    out = []
    for i, a in enumerate(nodes):
        if not a.files:
            continue
        for b in nodes[i + 1:]:
            if not b.files or a.id in anc[b.id] or b.id in anc[a.id]:
                continue
            shared = overlaps(a.files, b.files)
            if shared:
                out.append((a.id, b.id, shared))
    return out


def width(nodes: list[Node]) -> int:
    """The largest number of tasks on one level: an upper bound on useful parallelism for this graph."""
    try:
        lv = levels(nodes)
    except GraphError:
        return 1
    counts: dict[int, int] = {}
    for v in lv.values():
        counts[v] = counts.get(v, 0) + 1
    return max(counts.values(), default=0)


def view(nodes: list[Node], state: dict) -> dict:
    """The graph for the dashboard / API: nodes with status (derived states included), level and critical path,
    the edges, and what is wrong with it."""
    errors = validate(nodes, [tid for tid, e in state.get("tasks", {}).items() if e.get("status") == "done"])
    lv: dict[str, int] = {}
    cp: dict[str, float] = {}
    if not any(e.startswith("dependency cycle") for e in errors):
        lv = levels(nodes)
        done = {n.id for n in nodes if status_of(state, n.id) == "done"}
        cp = critical_path(nodes, done)
    stuck_on = stuck(nodes, state)
    ready_ids = set(ready(nodes, state))
    rows = []
    for n in nodes:
        s = status_of(state, n.id)
        derived = s
        if s in RUNNABLE:
            derived = "stuck" if n.id in stuck_on else ("ready" if n.id in ready_ids else "waiting")
        rows.append({"id": n.id, "title": n.title, "status": s, "state": derived, "depends": n.depends, "files": n.files,
                     "tier": state.get("tasks", {}).get(n.id, {}).get("tier") or n.tier, "level": lv.get(n.id),
                     "critical": round(cp.get(n.id, 0.0), 2), "stuck_on": stuck_on.get(n.id)})
    return {"nodes": rows, "edges": [[d, n.id] for n in nodes for d in n.depends], "errors": errors,
            "conflicts": [{"a": a, "b": b, "files": f} for a, b, f in concurrent_conflicts(nodes)],
            "width": width(nodes) if not errors else None}
