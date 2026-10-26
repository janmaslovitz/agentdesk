"""AgentDesk dashboard: projects, accounts, queues, runs, tokens, limits, logs and a chat per project.

Listens on 127.0.0.1 only (`python -m agentdesk serve` can bind elsewhere, see agentdesk/serve.py). Runs are separate processes (`python -m agentdesk run …`), one per
project at a time, so closing the dashboard doesn't stop them (Stop does).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import tomllib
import time
import urllib.parse
import webbrowser
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core, hygiene, reliability

_SDK_NAMES = ('AgentDefinition', 'AssistantMessage', 'ClaudeSDKClient', 'HookMatcher', 'PermissionResultAllow', 'PermissionResultDeny', 'RateLimitEvent', 'ResultMessage', 'SystemMessage', 'TaskNotificationMessage', 'TaskStartedMessage', 'TextBlock', 'ToolResultBlock', 'ToolUseBlock', 'UserMessage', 'create_sdk_mcp_server')


def _load_sdk() -> None:
    """Import claude_agent_sdk (slow: ~1s) on first use instead of at program start; its names become
    module globals, so tests can still patch them. Call before using any SDK name."""
    if "tool" in globals():
        return
    core._load_sdk()
    import claude_agent_sdk
    for n in _SDK_NAMES:
        globals().setdefault(n, getattr(claude_agent_sdk, n))
    globals().setdefault("tool", claude_agent_sdk.tool)


def __getattr__(name: str):
    if name in _SDK_NAMES or name == "tool":
        _load_sdk()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


WEB = (Path(sys._MEIPASS) / "agentdesk" / "web") if core.FROZEN else Path(__file__).resolve().parent / "web"
ID = re.compile(r"[A-Za-z0-9_-]+")


# ----------------------------------------------------------------------------- runs

class Runner:
    def __init__(self, p: core.Project):
        self.p = p
        self.proc: subprocess.Popen | None = None
        self.args: list[str] = []

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str]) -> str | None:
        if self.running():
            return "a run is already going for this project"
        gate = core.disk_gate()
        if gate:  # the server's disk budget / this user's quota (running work is never stopped by it)
            core.Store(self.p).log(f"DISK BUDGET: not starting a run for {self.p.slug}: {gate}")
            return f"not starting: {gate}"
        cfg = core.autorunner_cfg(self.p)
        for where in {str(core.DATA), str(self.p.cfg.get("repo", {}).get("worktrees") or core.DATA)}:
            ok, why = reliability.disk_ok(where, cfg["min_free_gb"])
            if not ok:  # a full disk wrecks half-written state: refuse loudly instead
                msg = f"DISK FULL: not starting a run for {self.p.slug}: {why} on {where}"
                core.Store(self.p).log(msg)
                print(msg, file=sys.stderr, flush=True)
                return f"not enough free disk space ({why}); free some space and try again"
        reliability.rotate(self.p.path("runner.log"))
        out = self.p.path("runner.log").open("a", encoding="utf-8")
        env = {**os.environ, **core.policy_env(self.p), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "AGENTDESK_HOME": str(core.DATA)}
        # Detach from the dashboard's console/session so the run outlives the dashboard and its window.
        if sys.platform == "win32":
            kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW}
        else:
            kw = {"start_new_session": True}
        self.proc = subprocess.Popen([sys.executable, *([] if core.FROZEN else ["-m", "agentdesk"]), *args], cwd=core.APP, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, **kw)
        out.close()
        try:
            self.p.path("runner.pid").write_text(str(self.proc.pid), encoding="utf-8")
        except OSError:
            pass
        self.args = args
        core.Store(self.p).log(f"dashboard: started {' '.join(args)} (pid {self.proc.pid})")
        return None

    def stop(self) -> str | None:
        if not self.running() or self.proc is None:
            return "nothing is running"
        self.proc.terminate()
        core.Store(self.p).log("dashboard: run stopped")
        return None

    def info(self) -> dict:
        return {"running": self.running(), "args": self.args, "exit": None if self.running() or not self.proc else self.proc.returncode}


RUNNERS: dict[str, Runner] = {}
LOCK = threading.Lock()


def runner(p: core.Project) -> Runner:
    with LOCK:
        if p.slug not in RUNNERS:
            RUNNERS[p.slug] = Runner(p)
        RUNNERS[p.slug].p = p
        return RUNNERS[p.slug]


def run_args(p: core.Project, mode: str, tid: str = "") -> list[str] | None:
    """Command line for a dashboard run (`python -m agentdesk …`), or None for an unknown mode."""
    if tid and not ID.fullmatch(tid):
        raise ValueError("bad task id")
    return {"next": ["run", p.slug, "--max", "1"], "all": ["run", p.slug], "task": ["run", p.slug, tid],
            "plan": ["plan", p.slug] + ([tid] if tid else []), "final": ["final", p.slug, tid]}.get(mode)


# ----------------------------------------------------------------------------- chat

CHAT_PROMPT = """You are the assistant on the user's AgentDesk dashboard for the project "{name}"
(repository {repo}). Agents work on it task by task. Be brief and plain; answer in the user's
language.

Your working directory is the project's AgentDesk folder:
- state.json: per task status (pending/routing/working/done/blocked/paused), tier, runs (model,
  cost_usd, tokens, turns), last_failure, commit, branch; spent_usd; tokens.
- tasks/*.md: the queue. Each file starts with a TOML header between +++ lines:
    +++
    id = "007"
    title = "Short title"
    tier = "standard"        # optional: mechanical | standard | hard (else the router decides)
    depends = ["001"]        # optional
    +++
    Then: the goal, what's wrong today, where (files), constraints, how to tell it's done.
  Use the next free number as id ({next_id} is free now).
- reports/<id>.md: brief and worker summary of finished tasks. notes.md: durable facts.
- agentdesk.log: what happened, newest at the end. project.toml: repo, base branch, checks.

You may read these, and you may read the repository ({repo}) to write better tasks (Read, Grep,
Glob). You never edit the repository: you may only create or edit task files in tasks/ and append to
notes.md. The workers do the coding. Your tools for them (the agentdesk server):
- add_task: queue a task (title, body, optional tier and depends). queue_status: tasks and the run.
- task_activity(id?): the agent timeline of a task; when asked what an agent is doing, read it and say in a sentence or two.
- run_next, run_task(id), run_all, plan_task(id) (preview the router's plan), stop_run. The user
  approves each of these on the dashboard; only one run per project goes at a time.
Shell commands (Bash/PowerShell) also wait for the user's approval, so prefer Read/Grep/Glob and use
the shell only when it's really needed (e.g. `git log`), one plain command at a time. If the user
denies a call, don't retry it: say what you wanted to do.
When the user asks for work, write a clear task (ask one short question first only if the goal is
unclear), then say it's queued, and offer to start it. Never invent results: read the files.
"""

MEMORY_SECTIONS = ("Decisions", "Facts about the project", "Open questions", "User preferences")
MEMORY_MAX_WORDS = 1500
MEMORY_HEADING = "What you remember from earlier conversations"

ACK_RULE = """

Start every reply to the user's message with one short sentence, in the user's language, that confirms you understood
the request and says what you will do next (for example: "Понял фронт работ: формирую список задач и отправляю в работу."
or "Got it: I'll check the branch state, then merge what passes."). Only then do the work or answer. For a plain
question, one short acknowledging clause is enough. Don't repeat it later in the same reply."""

MEMORY_PROMPT = """You maintain the running memory of a project chat assistant. Below are the current memory and the latest
exchange. Return the full updated memory and nothing else, as Markdown with exactly these sections, in this order:
{sections}
Rules: key facts, decisions, open threads and user preferences only, as short bullet points. Drop what is outdated or
trivial. Never store secrets (keys, tokens, passwords) or file contents. Keep it under {words} words in total.
{extra}
Current memory:
{memory}

Latest exchange:
User: {user}
Assistant: {reply}
"""


def valid_memory(text: str) -> bool:
    """A usable memory: every section heading present, within the word cap."""
    return all(f"## {s}" in text for s in MEMORY_SECTIONS) and len(text.split()) <= MEMORY_MAX_WORDS


async def memory_call(p: core.Project, model: str, prompt: str) -> str:
    """One short call without tools or permission callbacks; its cost is not counted in chat_usage.json."""
    _load_sdk()
    opts = core.options(p, model=model, max_turns=1, allowed_tools=[], disallowed_tools=list(MEMORY_NO_TOOLS), system_prompt="You write concise Markdown memory notes.",
                        cwd=str(p.dir), permission_mode="default", setting_sources=[])
    out = ""
    async for msg in core.query(prompt=prompt, options=opts):
        if isinstance(msg, ResultMessage) and not msg.is_error:
            out = msg.result or ""
    return out.strip()


MEMORY_NO_TOOLS = ("Bash", "PowerShell", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent")

COWORKER_PROMPT = """You are the Coworker on the user's AgentDesk dashboard for the project "{name}". You work on
your own: nothing you do waits for the user's approval, so act carefully. Be brief and plain; answer
in the user's language.

Your working directory {wt} is your own git worktree of the repository on branch auto/cowork. Make
code changes only there, and commit them there when a piece of work is done (git add / git commit);
the user reviews auto/cowork and merges what they want. Never touch the user's checkout ({repo}).
Before changing code, read the project's instructions (CLAUDE.md / AGENTS.md / README) in your
worktree and follow them; run the relevant checks after a change.

The project's AgentDesk folder is {folder}:
- state.json: per task status, tier, runs, cost, last_failure, commit, branch. reports/<id>.md:
  finished tasks. agentdesk.log: what happened. You may read all of it.
- tasks/*.md: the queue; you may write task files there and append to notes.md (durable facts).
Your tools for the workers (the agentdesk server), no approval needed: add_task, queue_status,
task_activity(id) (the agent timeline: when asked what an agent is doing, read it and describe it in a sentence or two), run_next, run_task(id), run_all, plan_task(id), stop_run. Only one run per project goes at a time.
Delegate bigger pieces of work to the workers as tasks; do small things yourself.

Shell commands (Bash/PowerShell) run without asking, but a guard refuses pushing, changing remotes,
publishing, pull requests, resetting or switching to main, and editing AgentDesk's config; edits
outside your worktree, tasks/ and notes.md are refused too. Don't try to work around a refusal:
say what you wanted to do and let the user do it. You have a budget per message
(${budget:.2f}); finish with a short summary of what you did and what's left. Never invent results.
"""

COWORKER_FULL_PROMPT = """You are the Coworker on the user's AgentDesk dashboard for the project "{name}": its curator. You
work above the whole project, on your own, with full access: every tool and every shell command runs
without asking, in the project's own checkout {repo} (base branch "{base}", currently checked out there
unless you switch) and anywhere else on this computer. Be brief and plain; answer in the user's language.

Your job is to look after the project and keep it moving:
- Keep the picture true: read the code, the project's instructions (CLAUDE.md / AGENTS.md / README),
  the git state (branches, worktrees, uncommitted changes, what the workers' auto/* branches hold), the
  queue and the reports. Say what is actually going on; never invent results, read the files.
- Curate the work: decide what is next, write and reorder tasks, hand bigger pieces to the workers and
  review what they bring back (read the diff, run the checks), then merge the good branches into
  "{base}", fix what needs fixing, and clean up what is finished or stale (merged branches, worktrees).
- Do small things yourself, directly in the checkout, on "{base}" when that is right: edit, run the
  checks, commit. Use a branch when a change is risky or the user asked to review first.
- Keep the user's work safe even though nothing stops you: look at the target before deleting or
  overwriting, don't discard uncommitted changes you didn't make, prefer commands you can undo, and run
  the project's checks before committing to "{base}". Pushing, publishing, opening pull requests and
  changing remotes are allowed, but do them when the user asks for it or the task clearly includes it,
  and say plainly that you did.

The project's AgentDesk folder is {folder}:
- state.json: per task status, tier, runs, cost, last_failure, commit, branch. reports/<id>.md:
  finished tasks. agentdesk.log: what happened. notes.md: durable facts (append what future you needs).
- tasks/*.md: the queue. project.toml: repo, base branch, checks (you may edit it; keep it valid TOML).
Your tools for the workers (the agentdesk server): add_task, queue_status, task_activity(id) (the agent timeline: when asked what an agent is doing, read it and describe it in a sentence or two), run_next, run_task(id),
run_all, plan_task(id), stop_run. Only one run per project goes at a time.

You have a budget per message (${budget:.2f}); finish with a short summary of what you did and what's
left. Workers run in their own worktrees, so your edits in the checkout don't collide with theirs.
"""

PLANNER_PROMPT = """You are the Planner on the user's AgentDesk dashboard for the project "{name}" (repository
{repo}). You turn the user's request into a plan of tasks for the workers; you don't do the work
yourself. Be brief and plain; answer in the user's language.

Read the request, then as much of the repository ({repo}) and the project's AgentDesk folder (your
working directory: tasks/*.md is the queue, state.json the status of each task, reports/, notes.md,
agentdesk.log) as you need to plan well, with Read, Glob and Grep. Keep the exploring short. Then
call propose_plan with:
- summary: one line on what the plan achieves;
- tasks, in order, each with a short title; goal (what to change, constraints, and how to tell it's
  done: the worker reads only its task); files (the files it touches); tier (mechanical | standard |
  hard, or empty to let the router decide); depends (what must be done first: d1, d2, ... for this
  plan's tasks by position, or ids of tasks already queued).
Prefer few, self-contained tasks; split only where one worker would otherwise do too much at once.

The plan is a draft: nothing runs until the user presses "Approve and start" on the plan card. Then the
drafts become queued tasks and the workers run them in dependency order. You don't approve, start or
stop anything yourself. When the user asks for changes, call update_plan with the whole new task list;
call cancel_plan only when the user asks to drop the draft. An approved plan is not rewritten: if one
of its tasks is blocked, explain what failed (state.json last_failure, agentdesk.log) and what you'd
suggest, and wait for the user's decision.

You can't edit files or run commands, and never push or publish anything. Budget per message:
${budget:.2f}. Never invent results: read the files. Each user message starts with the plan's current
state in brackets.
"""

COWORKER_LIVE_ADDENDUM = """
This is a live session: it stays open between messages, so you can be interactive like a teammate.
- The user can write to you while you work: read new messages as they come and adapt. Keep replies short.
- Background work: run long commands in the background (Bash/PowerShell with run_in_background) and keep going;
  you are woken when they finish. Use watch_run right after starting a worker run: it wakes you when the run
  ends or fails and costs nothing while waiting. Use schedule_checkup to come back later.
- Subagents (the Agent tool): "explorer" (cheap, read-only investigation), "reviewer" (reads a branch or diff and
  reports problems), "implementer" (makes one focused change and runs the checks). Run several in parallel for
  independent questions; give each a self-contained brief and verify what they report before relying on it.
- checkup gives you the project's health in one report (queue, run, failures, unmerged branches, usage, spending).
  When woken by a scheduled checkup, act on what needs attention (queue tasks, merge what passes, tell the user
  what needs them); if everything is fine, answer with one line.
Spending: about ${burst:.2f} per burst of work (a message or a wake-up) and ${daily:.2f} per day for everything
you and your subagents do; keep bursts proportionate and prefer the cheaper subagents for reading and searching.
"""

CHECKUP_PROMPT = ("[Scheduled checkup] Run the checkup tool and look at the project: failed or blocked tasks, finished work "
                  "that is not merged yet, the run state, usage and today's spending. Act on what you safely can (queue "
                  "tasks, merge what passes the checks, restart what crashed) and say what needs the user. If nothing "
                  "needs attention reply with the single line: All quiet.")


def live_agents() -> dict:
    """Subagents the live Coworker can launch with the Agent tool."""
    _load_sdk()
    return {
        "explorer": AgentDefinition(
            description="Fast read-only investigator: finds files, reads code and logs, answers a specific question about the repository.",
            prompt="You investigate and report. Read and search only; never change anything. Answer the question in a short, factual "
                   "report with file paths and line numbers.",
            tools=["Read", "Grep", "Glob"], model="haiku"),
        "reviewer": AgentDefinition(
            description="Reviews a branch, commit range or diff for correctness bugs and missed requirements, and runs the project's checks.",
            prompt="You review code. Read the diff (git diff / git log), read the surrounding code, run the project's checks when "
                   "that helps, and report concrete problems with file and line, most serious first. Don't edit files. If it is fine, say so.",
            tools=["Read", "Grep", "Glob", "Bash", "PowerShell"], model="sonnet"),
        "implementer": AgentDefinition(
            description="Makes one focused code change in the checkout and runs the relevant checks, then reports what changed.",
            prompt="You make exactly the change you are asked for, in the files you are told about, following the project's instructions "
                   "(CLAUDE.md / AGENTS.md / README). Run the relevant checks. Don't commit unless asked. Report the files changed and the "
                   "check results.",
            model="sonnet"),
    }


MCP = "agentdesk"
MCP_PREFIX = f"mcp__{MCP}__"


def _event_line(ev: dict) -> str:
    """One timeline event as a short line for the chat: '09:31 tool Edit core.py'."""
    at = str(ev.get("at", ""))[11:16]
    kind = ev.get("kind", "")
    who = "router " if ev.get("role") == "router" else ""
    if kind == "tool":
        what = f"{ev.get('name', '')} {ev.get('input') or ev.get('file') or ''}"
    elif kind == "result":
        what = f"finished: {ev.get('turns', '?')} turns, ${ev.get('cost_usd') or 0:.2f}" + (" (error)" if ev.get("error") else "")
    else:
        what = ev.get("text") or ev.get("name") or ev.get("tier") or ""
    return f"{at} {who}{kind} {str(what).strip()}"[:220]


def activity_digest(p: "core.Project", tid: str, last: int = 12) -> str:
    """What an agent is doing now, from the task's timeline (runs/<id>.jsonl): status, counts and the last events."""
    e = core.Store(p).load()["tasks"].get(tid, {})
    ev = core.read_activity(p, tid, limit=300)
    head = f"{tid} {e.get('status', 'pending')}" + (f", started {e['started']}" if e.get("started") else "")
    if not ev:
        return head + ": no timeline yet"
    tools = [x for x in ev if x.get("kind") == "tool"]
    files = sorted({Path(str(x["file"])).name for x in tools if x.get("file")})[-8:]
    errs = sum(1 for x in ev if x.get("kind") == "tool_error")
    out = [head, f"tool calls: {len(tools)}, tool errors: {errs}" + (f", files touched: {', '.join(files)}" if files else "")]
    out += ["last events:"] + ["  " + _event_line(x) for x in ev[-last:]]
    return "\n".join(out)
AUTONOMOUS = ("coworker", "planner")  # modes without approval cards: a budget per message and the usage pause
PLANNER_TOOLS = ("propose_plan", "update_plan", "cancel_plan")
# What the Planner may use. ToolSearch only loads tool schemas (the CLI may defer MCP tools behind it).
PLANNER_ALLOWED = ("Read", "Glob", "Grep", "ToolSearch")
PLAN_POLL_S = 2.0
NEEDS_APPROVAL = {"run_next": "Start a run of the next ready task", "run_task": "Start a run of task {id}",
                  "run_all": "Start a run of all ready tasks", "plan_task": "Preview the router's plan for task {id} (a cheap model call)",
                  "stop_run": "Stop the current run"}
APPROVAL_TIMEOUT_S = 600
ACTIONS = ("allow_once", "always_allow", "deny")


class Approvals:
    """Tool calls waiting for the user's answer on the dashboard.

    `ask` runs on the SDK's event loop and awaits a Future; `resolve` is called from HTTP threads and
    sets it through `loop.call_soon_threadsafe`. No answer within the timeout counts as deny.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, timeout: float = APPROVAL_TIMEOUT_S):
        self.loop, self.timeout = loop, timeout
        self.lock = threading.Lock()
        self.items: dict[str, tuple[dict, asyncio.Future]] = {}

    async def ask(self, tool: str, detail: str, reason: str = "", prefix: str | None = None) -> dict:
        fut = self.loop.create_future()
        rid = uuid.uuid4().hex[:10]
        req = {"id": rid, "tool": tool, "detail": detail, "reason": reason, "prefix": prefix, "at": time.time()}
        with self.lock:
            self.items[rid] = (req, fut)
        try:
            return await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            return {"action": "deny", "timeout": True}
        finally:
            with self.lock:
                self.items.pop(rid, None)

    def pending(self) -> list[dict]:
        with self.lock:
            return [dict(req) for req, _ in self.items.values()]

    def resolve(self, rid: str, action: str, prefix: str | None = None) -> bool:
        if action not in ACTIONS:
            raise ValueError(f"action is one of {', '.join(ACTIONS)}")
        with self.lock:  # answered cards leave the list now, not when the SDK loop gets round to ask()
            entry = self.items.pop(rid, None)
        if entry is None:
            return False
        fut = entry[1]
        answer = {"action": action, "prefix": prefix}
        self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(answer))
        return True

    def deny_all(self) -> None:
        for req in self.pending():
            self.resolve(req["id"], "deny")


tool_summary = core.tool_summary  # shared with the task run timeline (core.Activity)
result_text = core.result_text


class Chat:
    def __init__(self, loop: asyncio.AbstractEventLoop, p: core.Project):
        _load_sdk()
        self.loop, self.p = loop, p
        self.client: ClaudeSDKClient | None = None
        self.history: list[dict] = self.load_history()
        self.lock = threading.Lock()
        self.approvals = Approvals(loop)
        self.busy = False
        self.auto_turn = False  # busy only because the CLI spoke unprompted (may be a stray late message)
        self.model = ""
        self.mode = ""  # the mode the current session was started in
        self.session_id: str | None = None  # the CLI session; coworker messages resume it in a new process
        self.session_cost = 0.0  # the session's running total_cost_usd (the CLI reports totals, not per message)
        self.generation = 0  # bumped by reset, so an answer still coming in doesn't revive the old session
        # numbers the progress notes, so the page can fetch the new ones; continues after the saved transcript, or a
        # restart reuses old numbers and the page re-adds old entries
        self.seq = max([int(m.get('seq') or 0) for m in self.history] or [0])
        self.note_lock = threading.Lock()
        self.watcher: threading.Thread | None = None
        self.pump: asyncio.Future | None = None  # reads the live session's messages (coworker live mode)
        self.drop = False  # the live process hit its spend limit: reconnect (resume) on the next message
        self.last_checkup = 0.0
        self._saved_at = 0.0
        self.last_event = time.time()  # the last message from the live session (or the start of the burst)
        self.idle_warned = False
        self.live_calls: dict = {}  # tool calls started and not answered yet
        self.cli_err: list[str] = []  # the last lines the CLI wrote to stderr (its own retry messages, errors)
        self.watching_run = False

    # -- the visible transcript, kept in chat.json so it survives a restart (the SDK session does not)

    def load_history(self) -> list[dict]:
        """The saved transcript, or [] when the file is missing, unreadable or not a list."""
        try:
            data = json.loads(self.p.path("chat.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # ValueError covers bad JSON and bad UTF-8
            return []
        return data if isinstance(data, list) else []

    def save_history(self) -> None:
        f = self.p.path("chat.json")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.history, indent=2, ensure_ascii=False), encoding="utf-8")
        core.replace_file(tmp, f)

    # -- the running memory, kept in chat_memory.md: this (not the transcript) goes back to the model

    def load_memory(self) -> str:
        try:
            return self.p.path("chat_memory.md").read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return ""

    def save_memory(self, text: str) -> None:
        f = self.p.path("chat_memory.md")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(text.strip() + "\n", encoding="utf-8")
        core.replace_file(tmp, f)

    def with_memory(self, prompt: str) -> str:
        """The system prompt plus the memory as it is now (read when a session is created)."""
        memory = self.load_memory()
        return f"{prompt}\n# {MEMORY_HEADING}\n{memory}\n" if memory else prompt

    def update_memory(self, user: str, reply: str) -> None:
        """Ask a tool-free model call for the updated memory; on any failure keep the old one."""
        try:
            old = self.load_memory()
            model = core.chat_settings(core.chat_mode(self.p))["model"] or "haiku"
            extra = ""
            for _ in range(2):  # one retry
                prompt = MEMORY_PROMPT.format(sections="\n".join(f"## {s}" for s in MEMORY_SECTIONS), words=MEMORY_MAX_WORDS - 200,
                                              extra=extra, memory=old or "(empty)", user=user, reply=reply)
                new = asyncio.run_coroutine_threadsafe(memory_call(self.p, model, prompt), self.loop).result(timeout=120).strip()
                if valid_memory(new):
                    self.save_memory(new)
                    return
                extra = "Your previous reply was empty, lacked a section heading or was too long. Follow the format and the word limit exactly.\n"
            core.Store(self.p).log("chat memory update rejected; kept the old memory")
        except Exception as e:  # the chat keeps working without it
            core.Store(self.p).log(f"chat memory update failed: {e or type(e).__name__}")

    # -- permissions: everything the chat may do without the user, and the approval cards for the rest

    async def permission(self, name: str, args: dict, _ctx=None):
        if name in ("Write", "Edit"):
            root = str(self.p.dir.resolve()).lower().replace("\\", "/")
            path = str(args.get("file_path", "")).lower().replace("\\", "/")
            if not ((path.startswith(root + "/tasks/") and path.endswith(".md")) or path == root + "/notes.md"):
                return PermissionResultDeny(message="you may only write task files in tasks/ or notes.md")
            return PermissionResultAllow()
        if name in ("Bash", "PowerShell"):
            cmd = str(args.get("command", ""))
            if core.command_allowed(cmd, core.allowed_commands(self.p)) and not core.guard_denied(cmd):
                return PermissionResultAllow()
            prefix = core.suggest_prefix(cmd)
            answer = await self.approvals.ask(name, cmd, str(args.get("description") or ""), prefix=prefix)
            if answer["action"] == "always_allow":
                pre = str(answer.get("prefix") or prefix or "").strip()
                if pre and core.command_allowed(cmd, [pre]):
                    core.allow_command(self.p, pre)
            return self._verdict(answer)
        if name.startswith(MCP_PREFIX) and name[len(MCP_PREFIX):] in NEEDS_APPROVAL:
            short = name[len(MCP_PREFIX):]
            answer = await self.approvals.ask(short, NEEDS_APPROVAL[short].format(id=args.get("id", "")))
            return self._verdict(answer)
        return PermissionResultAllow()

    async def coworker_permission(self, name: str, args: dict, ctx=None):
        """Coworker: no approval cards. Its own run/queue tools are allowed; everything else goes through
        the workers' guard, with edits limited to its worktree, tasks/ and notes.md."""
        if name.startswith(MCP_PREFIX) or core.chat_settings("coworker").get("full_access"):
            return PermissionResultAllow()
        wt = core.cowork_path(self.p)
        check = core.guard(wt, also=(self.p.dir / "tasks", self.p.dir / "notes.md"), never=(Path(self.p.cfg["repo"]["worktrees"]),),
                           extra_deny=core.COWORK_DENY, who="the Coworker")
        return await check(name, args, ctx)

    async def coworker_hook(self, data: dict, _tool_use_id=None, _ctx=None) -> dict:
        """PreToolUse hook running the same check for every tool call, including those the CLI allows on its
        own without asking can_use_tool (e.g. commands it thinks are read-only, like `echo`)."""
        if core.chat_settings("coworker").get("full_access"):
            return {}
        r = await self.coworker_permission(str(data.get("tool_name", "")), dict(data.get("tool_input") or {}))
        if isinstance(r, PermissionResultDeny):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": r.message}}
        return {}

    async def planner_permission(self, name: str, args: dict, _ctx=None):
        """Planner: reading and its three planning tools, nothing else (no edits, shell or run tools)."""
        if name in PLANNER_ALLOWED or (name.startswith(MCP_PREFIX) and name[len(MCP_PREFIX):] in PLANNER_TOOLS):
            return PermissionResultAllow()
        return PermissionResultDeny(message="the Planner only reads and plans: no edits, commands or runs. "
                                            "Put the work in the plan (propose_plan / update_plan)")

    async def planner_hook(self, data: dict, _tool_use_id=None, _ctx=None) -> dict:
        r = await self.planner_permission(str(data.get("tool_name", "")), dict(data.get("tool_input") or {}))
        if isinstance(r, PermissionResultDeny):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": r.message}}
        return {}

    @staticmethod
    def _verdict(answer: dict):
        if answer["action"] in ("allow_once", "always_allow"):
            return PermissionResultAllow()
        if answer.get("timeout"):
            return PermissionResultDeny(message="no answer from the user within 10 minutes; not done")
        return PermissionResultDeny(message="the user denied this; don't retry it")

    # -- custom tools: the chat delegates to the workers through the same runner as the dashboard buttons

    def tools(self, approval: bool = True) -> list:
        p = self.p
        needs = " (needs the user's approval)" if approval else ""

        def ok(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}]}

        def err(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        def start(mode: str, tid: str = "") -> dict:
            if tid and not any(t.id == tid for t in core.load_tasks(p)):
                return err(f"no task {tid}")
            try:
                args = run_args(p, mode, tid)
            except ValueError as e:
                return err(str(e))
            e = start_run(p, args or [])
            return err(e) if e else ok(f"started: {' '.join(args or [])}. Progress shows on the dashboard and in agentdesk.log.")

        @tool("add_task", "Queue a task for the workers. body: the goal, what's wrong today, where (files), constraints, how to tell it's done.",
              {"type": "object", "required": ["title", "body"], "properties": {
                  "title": {"type": "string"}, "body": {"type": "string"},
                  "tier": {"type": "string", "enum": core.TIER_ORDER, "description": "optional; else the router decides"},
                  "depends": {"type": "array", "items": {"type": "string"}, "description": "ids of tasks that must be done first"}}})
        async def add_task(a):
            tid = core.next_task_id(p)
            core.add_task(p, tid, str(a.get("title") or "Untitled"), str(a.get("body") or ""), a.get("tier") or None,
                          [str(x) for x in a.get("depends") or []])
            return ok(f"queued task {tid}")

        @tool("queue_status", "The tasks with their status and tier, and whether a run is going.", {})
        async def queue_status(_a):
            state = core.Store(p).load()
            r = runner(p).info()
            lines = [f"run: {'running ' + ' '.join(r['args']) if r['running'] else 'idle'}"]
            for t in core.load_tasks(p):
                e = state["tasks"].get(t.id, {})
                lines.append(f"{t.id} {e.get('status', 'pending')} {e.get('tier') or t.tier or 'auto'} — {t.title}")
                if e.get("status") in ("working", "routing"):
                    last = core.read_activity(p, t.id, limit=5)
                    if last:
                        lines.append("    now: " + _event_line(last[-1]))
            return ok("\n".join(lines))

        @tool("task_activity", "What a worker agent is doing: the task's timeline (last tool calls, files touched, errors, results). "
                               "id optional: defaults to the task that is working now. Read it, then describe in a sentence or two what the agent is up to.",
              {"type": "object", "properties": {"id": {"type": "string"}}})
        async def task_activity(a):
            tid = str(a.get("id") or "").strip()
            tasks = core.Store(p).load()["tasks"]
            if not tid:
                busy = [k for k, v in tasks.items() if v.get("status") in ("working", "routing")]
                if not busy:
                    return ok("no agent is working right now")
                tid = busy[-1]
            if not any(t.id == tid for t in core.load_tasks(p)):
                return err(f"no task {tid}")
            return ok(activity_digest(p, tid))

        @tool("run_next", f"Start a run of the next ready task{needs}.", {})
        async def run_next(_a):
            return start("next")

        @tool("run_task", f"Start a run of one task{needs}.", {"id": str})
        async def run_task(a):
            return start("task", str(a.get("id", "")))

        @tool("run_all", f"Start a run of all ready tasks, one after another{needs}.", {})
        async def run_all(_a):
            return start("all")

        @tool("plan_task", f"Preview the router's tier and brief for a task, without doing it{needs}.", {"id": str})
        async def plan_task(a):
            return start("plan", str(a.get("id", "")))

        @tool("stop_run", f"Stop the current run{needs}.", {})
        async def stop_run(_a):
            e = runner(p).stop()
            return err(e) if e else ok("run stopped")

        base = [add_task, queue_status, task_activity, run_next, run_task, run_all, plan_task, stop_run]
        if approval:
            return base

        @tool("checkup", "A health report of the project: run state, task counts, what needs attention (failed or unmerged work), "
                         "account usage and the Coworker's spending today.", {})
        async def checkup(_a):
            return ok(self.checkup_report())

        @tool("watch_run", "Wake me when the current worker run ends or fails. Use right after starting a run; costs nothing while waiting.", {})
        async def watch_run(_a):
            if not runner(p).running():
                return ok("no run is going")
            self.watch_run_bg()
            return ok("watching: I will be woken with a report when the run ends")

        @tool("schedule_checkup", "Wake me for a project checkup in N minutes (5 to 1440).", {"type": "object", "required": ["minutes"],
              "properties": {"minutes": {"type": "number"}}})
        async def schedule_checkup(a):
            try:
                m = float(a.get("minutes"))
            except (TypeError, ValueError):
                return err("minutes must be a number")
            if not 5 <= m <= 1440:
                return err("minutes must be between 5 and 1440")
            self.schedule_checkup(m)
            return ok(f"checkup scheduled in {m:g} minutes")

        return base + [checkup, watch_run, schedule_checkup]

    def planner_tools(self) -> list:
        """The Planner's tools: they only write the draft plan (plan.json and tasks/drafts/)."""
        p = self.p

        def done(plan_or_text) -> dict:
            if isinstance(plan_or_text, str):
                return {"content": [{"type": "text", "text": plan_or_text}]}
            v = core.plan_view(p, plan_or_text) or {}
            lines = [f"draft plan v{v.get('version', 1)}: {v.get('summary', '')}"]
            for t in v.get("tasks", []):
                lines.append(f"{t['id']} {t['title']} ({t['tier'] or 'tier: router'}"
                             + (f"; after {', '.join(t['depends'])}" if t["depends"] else "") + ")")
            lo, hi = v.get("estimate", [0, 0])
            lines.append(f"Estimated ${lo:.2f}-${hi:.2f}. The user sees it as a card with Approve and start / Edit / Cancel; "
                         "nothing runs before they approve.")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        def err(e: Exception) -> dict:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}

        task_schema = {"type": "object", "required": ["title", "goal"], "properties": {
            "title": {"type": "string", "description": "short"},
            "goal": {"type": "string", "description": "what to change, constraints, and how to tell it's done"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "files it touches"},
            "tier": {"type": "string", "enum": core.TIER_ORDER, "description": "optional; else the router decides"},
            "depends": {"type": "array", "items": {"type": "string"},
                        "description": "d1, d2, ... (this plan's tasks by position) or ids of queued tasks"}}}
        plan_schema = {"type": "object", "required": ["tasks"], "properties": {
            "summary": {"type": "string", "description": "one line on what the plan achieves"},
            "tasks": {"type": "array", "items": task_schema, "description": "in order"}}}

        @tool("propose_plan", "Write the plan as draft tasks (not runnable until the user approves it on the dashboard).", plan_schema)
        async def propose_plan(a):
            try:
                return done(core.propose_plan(p, str(a.get("summary") or ""), a.get("tasks")))
            except ValueError as e:
                return err(e)

        @tool("update_plan", "Replace the draft plan's tasks (the whole list) after the user asked for changes.", plan_schema)
        async def update_plan(a):
            try:
                return done(core.update_plan(p, a.get("summary") or None, a.get("tasks")))
            except ValueError as e:
                return err(e)

        @tool("cancel_plan", "Drop the draft plan and its draft task files (only when the user asks).", {})
        async def cancel_plan(_a):
            try:
                return done(f"draft plan cancelled ({core.cancel_plan(p)} draft task file(s) removed)")
            except ValueError as e:
                return err(e)

        return [propose_plan, update_plan, cancel_plan]

    # -- sessions: the Assistant keeps one CLI process; the Coworker starts one per message (resuming the
    # session), because --max-budget-usd counts the spend of the process, which makes it a per-message budget.

    def _assistant_options(self):
        self.model = core.chat_settings("assistant")["model"]
        prompt = self.with_memory(CHAT_PROMPT.format(name=self.p.name, repo=self.p.repo, next_id=core.next_task_id(self.p)))
        # Only read-only tools go in allowed_tools: anything listed there skips can_use_tool.
        return core.options(self.p, model=self.model, cwd=str(self.p.dir), system_prompt=prompt + ACK_RULE, max_turns=15,
                            allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.repo)],
                            mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.tools())},
                            disallowed_tools=["WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"], permission_mode="default",
                            can_use_tool=self.permission, setting_sources=[])

    def _coworker_options(self):
        cs = core.chat_settings("coworker")
        self.model = cs["model"]
        if cs.get("full_access"):
            prompt = COWORKER_FULL_PROMPT.format(name=self.p.name, repo=self.p.repo, base=self.p.cfg["repo"]["base"],
                                                 folder=self.p.dir, budget=float(cs["budget_usd"]))
            gate_env = {k: str(v) for k, v in self.p.cfg.get("gates", {}).get("env", {}).items()}
            # Everything is allowed through can_use_tool (no guard, no hook); the project checkout is the working directory.
            return core.options(self.p, scratch=False, model=self.model, effort=cs.get("effort") or None, cwd=str(self.p.repo), system_prompt=prompt + ACK_RULE,
                                max_turns=int(cs["max_turns"]), max_budget_usd=float(cs["budget_usd"]), resume=self.session_id,
                                allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.dir), str(Path(self.p.cfg["repo"]["worktrees"]))],
                                mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.tools(approval=False))},
                                disallowed_tools=[], permission_mode="default",
                                can_use_tool=self.coworker_permission, setting_sources=[], env=gate_env)
        wt = core.cowork_worktree(self.p)
        prompt = self.with_memory(COWORKER_PROMPT.format(name=self.p.name, wt=wt, repo=self.p.repo, folder=self.p.dir, budget=float(cs["budget_usd"])))
        gate_env = {k: str(v) for k, v in self.p.cfg.get("gates", {}).get("env", {}).items()}
        # Shell, edit and run tools must not go in allowed_tools (that skips can_use_tool, i.e. the guard).
        # No setting_sources: a project's .claude/settings.json could allow commands past the guard.
        return core.options(self.p, model=self.model, effort=cs.get("effort") or None, cwd=str(wt), system_prompt=prompt + ACK_RULE,
                            max_turns=int(cs["max_turns"]), max_budget_usd=float(cs["budget_usd"]), resume=self.session_id,
                            allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.dir)],
                            mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.tools(approval=False))},
                            disallowed_tools=["WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"], permission_mode="default",
                            can_use_tool=self.coworker_permission, setting_sources=[], env=gate_env,
                            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self.coworker_hook])]})

    def _planner_options(self):
        cs = core.chat_settings("planner")
        self.model = cs["model"]
        prompt = PLANNER_PROMPT.format(name=self.p.name, repo=self.p.repo, budget=float(cs["budget_usd"]))
        # Read-only plus the planning tools; the permission check and the hook refuse everything else.
        return core.options(self.p, model=self.model, effort=cs.get("effort") or None, cwd=str(self.p.dir), system_prompt=prompt + ACK_RULE,
                            max_turns=int(cs["max_turns"]), max_budget_usd=float(cs["budget_usd"]), resume=self.session_id,
                            allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.repo)],
                            mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.planner_tools())},
                            disallowed_tools=["Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
                                              "Task", "Agent"],
                            permission_mode="default", can_use_tool=self.planner_permission, setting_sources=[],
                            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self.planner_hook])]})

    async def _client(self, mode: str) -> ClaudeSDKClient:
        if self.client is not None and self.mode != mode:
            await self._close()
        if self.client is None:
            self.mode = mode
            # The coworker's worktree may need a `git worktree add` first: off the event loop.
            opts = (self._live_options() if mode == "coworker" and self.is_live()
                    else await asyncio.to_thread(self._coworker_options) if mode == "coworker"
                    else self._planner_options() if mode == "planner" else self._assistant_options())
            self.client = ClaudeSDKClient(options=opts)
            await self.client.connect()
        return self.client

    async def _close(self) -> None:
        client, self.client = self.client, None
        if self.pump is not None and not self.pump.done():
            self.pump.cancel()
        self.pump = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # the process may be gone already
                pass

    def _answer(self, text: str, tools=(), cost: float = 0.0, tokens: dict | None = None) -> dict:
        return {"text": text, "tools": list(tools), "cost": cost, "tokens": tokens or {}, "model": self.model}

    async def _ask(self, text: str) -> dict:
        mode = core.chat_mode(self.p)
        coworker = mode in AUTONOMOUS  # coworker or planner: per-message budget and the usage pause
        acct = self.p.account.get("name", "default")
        gen = self.generation
        if coworker:
            await self._close()  # a fresh process per message, so the budget counts this message only
        client = await self._client(mode)
        reply, calls, result = [], {}, None
        if mode == "planner":  # the plan may have changed on the dashboard (approved, cancelled, progress)
            text = f"[Plan: {core.plan_brief(self.p)}]\n\n{text}"
        try:
            await client.query(text)
            async for msg in client.receive_response():
                if isinstance(msg, RateLimitEvent):
                    core.record_rate_limit(acct, msg.rate_limit_info)
                elif isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            reply.append(b.text)
                        elif isinstance(b, ToolUseBlock):
                            name = b.name[len(MCP_PREFIX):] if b.name.startswith(MCP_PREFIX) else b.name
                            calls[b.id] = {"name": name, "input": tool_summary(b.name, b.input or {}), "output": "", "error": False}
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock) and b.tool_use_id in calls:
                            out = result_text(b.content)
                            calls[b.tool_use_id].update(output=out[:2000] + ("\n…" if len(out) > 2000 else ""), error=bool(b.is_error))
                elif isinstance(msg, ResultMessage):
                    result = msg
        finally:
            if coworker and self.client is client:
                await self._close()
        cost, tokens = 0.0, {}
        if result is not None:
            # total_cost_usd is the session's running total (also across a resume): this message is the difference.
            total = float(result.total_cost_usd or 0.0)
            cost = total - self.session_cost if total >= self.session_cost else total
            tokens = core.usage_tokens(result.usage)
            if gen == self.generation:  # not reset (e.g. a mode switch) meanwhile
                self.session_cost, self.session_id = total, result.session_id
        u = self.usage()
        u["spent_usd"] = round(u.get("spent_usd", 0.0) + cost, 4)
        for k, v in tokens.items():
            u.setdefault("tokens", {})[k] = u.get("tokens", {}).get(k, 0) + v
        core.write_json(self.p.path("chat_usage.json"), u)
        parts = ["\n\n".join(reply).strip()]
        if coworker and (note := budget_note(result, cost, mode)):
            parts.append(note)
        return self._answer("\n\n".join(x for x in parts if x) or "(no answer)", calls.values(), cost, tokens)

    # -- the live Coworker: one persistent session whose messages are read continuously and shown as they arrive

    def is_live(self) -> bool:
        cs = core.chat_settings("coworker")
        return core.chat_mode(self.p) == "coworker" and bool(cs.get("full_access")) and bool(cs.get("live", True))

    def _live_options(self):
        cs = core.chat_settings("coworker")
        self.model = cs["model"]
        prompt = COWORKER_FULL_PROMPT.format(name=self.p.name, repo=self.p.repo, base=self.p.cfg["repo"]["base"],
                                             folder=self.p.dir, budget=float(cs["budget_usd"]))
        prompt += COWORKER_LIVE_ADDENDUM.format(burst=float(cs["budget_usd"]), daily=float(cs.get("daily_budget_usd") or 0))
        gate_env = {k: str(v) for k, v in self.p.cfg.get("gates", {}).get("env", {}).items()}
        daily = float(cs.get("daily_budget_usd") or 0)
        # The process-wide limit is only a backstop (the CLI counts the spend of the whole process); the bursts are
        # checked when they end, the day's total before each new burst.
        return core.options(self.p, scratch=False, model=self.model, effort=cs.get("effort") or None, cwd=str(self.p.repo),
                            system_prompt=self.with_memory(prompt) + ACK_RULE, max_turns=int(cs["max_turns"]),
                            max_budget_usd=daily or None, resume=self.session_id,
                            allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.dir), str(Path(self.p.cfg["repo"]["worktrees"]))],
                            mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.tools(approval=False))},
                            disallowed_tools=[], permission_mode="default", can_use_tool=self.coworker_permission,
                            setting_sources=[], env={**gate_env, "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}, agents=live_agents(), stderr=self._on_stderr)

    def daily_spent(self) -> float:
        d = self.usage().get("daily") or {}
        return float(d.get("usd", 0.0)) if d.get("date") == dt.date.today().isoformat() else 0.0

    def _record_usage(self, cost: float, tokens: dict) -> None:
        u = self.usage()
        u["spent_usd"] = round(u.get("spent_usd", 0.0) + cost, 4)
        for k, v in tokens.items():
            u.setdefault("tokens", {})[k] = u.get("tokens", {}).get(k, 0) + v
        today = dt.date.today().isoformat()
        d = u.get("daily") if (u.get("daily") or {}).get("date") == today else {"date": today, "usd": 0.0}
        d["usd"] = round(float(d["usd"]) + cost, 4)
        u["daily"] = d
        core.write_json(self.p.path("chat_usage.json"), u)

    def _live_add(self, entry: dict) -> None:
        """Add an entry to the transcript; the page fetches the ones newer than its last seq."""
        with self.note_lock:
            self.seq += 1
            self.history.append({**entry, "seq": self.seq})
            # chat.json is the whole transcript: rewriting it for every streamed entry is O(n) each time. At most once
            # a second here; a finished burst (the result entry, with a cost) always saves.
            if entry.get("cost") is not None or time.time() - self._saved_at >= 1.0:
                try:
                    self.save_history()
                    self._saved_at = time.time()
                except OSError:
                    pass

    def _on_stderr(self, line: str) -> None:
        line = line.strip()
        if line:
            self.cli_err = (self.cli_err + [line[:240]])[-5:]

    def idle_s(self) -> int:
        """Seconds the live session has been busy without a single message from it (0 when it is not busy)."""
        # a tool call in flight (a long build, say) is silence too, but a healthy one: only count the wait for the model
        return int(time.time() - self.last_event) if self.busy and self.client is not None and not self.live_calls else 0

    def watch_idle(self, after_s: float = 120) -> None:
        """A turn that has been silent for `after_s` seconds is usually the CLI retrying a failing connection: say so
        in the chat once (with what the CLI last wrote), instead of leaving "working…" for the user to puzzle over."""
        if self.is_live() and self.busy and not self.idle_warned and self.idle_s() >= after_s:
            self.idle_warned = True
            hint = f" Last thing the CLI said: {self.cli_err[-1]}" if self.cli_err else ""
            self.note(f"⚠ No reply from Claude for {self.idle_s() // 60} min. The connection to Anthropic may be failing or slow "
                      f"(the CLI retries quietly). Stop cancels the turn; you can send the message again.{hint}")

    def _live_event(self, msg, calls: dict, acct: str) -> None:
        self.last_event, self.idle_warned = time.time(), False
        if isinstance(msg, RateLimitEvent):
            core.record_rate_limit(acct, msg.rate_limit_info)
        elif isinstance(msg, TaskStartedMessage):  # (these are SystemMessages too: check them first)
            self.note(f"▶ Background task started: {msg.description}")
        elif isinstance(msg, TaskNotificationMessage):
            mark = "✓" if str(msg.status) in ("completed", "done", "success") else "✗"
            self.note(f"{mark} Background task {msg.status}: {(msg.summary or msg.task_id or '')[:300]}")
        elif isinstance(msg, SystemMessage):
            if getattr(msg, "subtype", "") == "session_state_changed" and isinstance(msg.data, dict):
                # the CLI says it is idle: no turn is owed, so the "working" indicator must stop (a stray late message
                # after the result used to leave `busy` on for good)
                if msg.data.get("state") == "idle" and not calls:
                    self.busy = False
            sid = (msg.data or {}).get("session_id") if isinstance(msg.data, dict) else None
            if sid:
                self.session_id = str(sid)
        elif isinstance(msg, AssistantMessage):
            if not self.busy:
                self.auto_turn = True
            self.busy = True  # also when the CLI started a turn by itself (a finished background job)
            sub = bool(getattr(msg, "parent_tool_use_id", None))
            texts = []
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    texts.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    name = b.name[len(MCP_PREFIX):] if b.name.startswith(MCP_PREFIX) else b.name
                    calls[b.id] = {"name": ("↳ " if sub else "") + name, "input": tool_summary(b.name, b.input or {}), "output": "", "error": False}
            if texts:
                self._live_add({"role": "assistant", "kind": "live", "text": ("↳ " if sub else "") + "\n\n".join(texts)})
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            done = []
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.tool_use_id in calls:
                    call = calls.pop(b.tool_use_id)
                    out = result_text(b.content)
                    call.update(output=out[:2000] + ("\n…" if len(out) > 2000 else ""), error=bool(b.is_error))
                    done.append(call)
            if done:
                self._live_add({"role": "assistant", "kind": "live", "text": "", "tools": done})
        elif isinstance(msg, ResultMessage):
            self._live_result(msg)

    def _live_result(self, msg) -> None:
        total = float(msg.total_cost_usd or 0.0)
        cost = total - self.session_cost if total >= self.session_cost else total
        tokens = core.usage_tokens(msg.usage)
        self.session_cost, self.session_id = total, msg.session_id or self.session_id
        self._record_usage(cost, tokens)
        self.busy, self.auto_turn = False, False
        cs = core.chat_settings("coworker")
        burst, daily = float(cs["budget_usd"]), float(cs.get("daily_budget_usd") or 0)
        notes = []
        if msg.subtype == "error_max_budget_usd":
            self.drop = True
            notes.append("Stopped: the Coworker process reached its spend limit; the next message starts a fresh process "
                         "(the conversation resumes).")
        elif msg.subtype == "error_max_turns":
            notes.append("Stopped: reached the turn limit for one burst (`[chat.coworker] max_turns`). Send another message to continue.")
        elif cost > burst:
            notes.append(f"This burst cost ${cost:.2f}, over the ${burst:.2f} per-burst budget (`[chat.coworker] budget_usd`).")
        if daily and self.daily_spent() >= daily:
            notes.append(f"Today's Coworker spending reached the daily budget of ${daily:.2f} (`[chat.coworker] daily_budget_usd`); "
                         "it takes no new work until tomorrow or until you raise it.")
        self._live_add({"role": "assistant", "kind": "live", "text": "\n".join(notes), "cost": cost, "tokens": tokens, "model": self.model})

    async def _pump(self, client, gen: int) -> None:
        """Read every message of the live session until it ends: answers, tool calls, background-task events and
        the turns the CLI starts by itself when a background job finishes."""
        calls = self.live_calls = {}
        acct = self.p.account.get("name", "default")
        try:
            async for msg in client.receive_messages():
                if gen != self.generation or self.client is not client:
                    break
                try:
                    self._live_event(msg, calls, acct)
                except Exception as e:  # one odd message must not end the session
                    core.Store(self.p).log(f"live chat: could not show a message: {e}")
                if self.drop:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if gen == self.generation:
                self.note(f"⚠ The live session ended: {e or type(e).__name__}. The next message starts it again.")
        finally:
            if gen == self.generation:
                self.busy, self.auto_turn = False, False
                self.live_calls.clear()
                if self.client is client:
                    self.client = None
                    self.drop = False
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

    async def _live_send(self, text: str) -> dict:
        cs = core.chat_settings("coworker")
        cap = float(cs.get("daily_budget_usd") or 0)
        if cap and self.daily_spent() >= cap:
            why = (f"Today's Coworker spending reached the daily budget of ${cap:.2f}. Raise `[chat.coworker] daily_budget_usd` "
                   "in settings.toml (0 = no cap) to go on today.")
            self.note(why)
            return {"error": why}
        if self.drop:
            await self._close()
            self.drop = False
        client = await self._client("coworker")
        if self.pump is None or self.pump.done():
            self.pump = asyncio.ensure_future(self._pump(client, self.generation))
        self.busy, self.auto_turn = True, False
        self.last_event, self.idle_warned = time.time(), False
        await client.query(text)
        return {"started": True, "live": True}

    def inject(self, text: str, label: str) -> None:
        """Wake the live Coworker with a message that is not from the user (a finished run, a checkup)."""
        if not self.is_live():
            self.note(f"{label}: {text[:300]}")
            return
        self.note(label)
        try:
            out = asyncio.run_coroutine_threadsafe(self._live_send(text), self.loop).result(timeout=90)
            if out.get("error"):
                return
        except Exception as e:
            self.note(f"{label}: couldn't reach the Coworker ({e or type(e).__name__})")

    def settle(self, quiet_s: float = 45) -> None:
        """A "turn" only inferred from an unprompted message, with nothing running and nothing heard for a while, is over
        (a stray message after the result must not leave "working…" on for good)."""
        if self.busy and self.auto_turn and not self.live_calls and time.time() - self.last_event >= quiet_s:
            self.busy = self.auto_turn = False

    def is_busy(self) -> bool:
        self.settle()
        return self.busy

    def interrupt(self) -> bool:
        """Stop the running reply. Always answers in the chat; always leaves `busy` off."""
        client, was_busy = self.client, self.busy
        self.busy = self.auto_turn = False
        if not was_busy:
            self.note("Nothing to stop: no reply is running.")
            return False
        if client is not None:
            asyncio.run_coroutine_threadsafe(client.interrupt(), self.loop)
        self.note("■ Interrupted by you.")
        return True

    def checkup_report(self) -> str:
        p = self.p
        state = core.Store(p).load()
        tasks = core.load_tasks(p)
        by: dict[str, int] = {}
        for t in tasks:
            s = state["tasks"].get(t.id, {}).get("status", "pending")
            by[s] = by.get(s, 0) + 1
        r = runner(p).info()
        lines = [f"Run: {'running ' + ' '.join(r['args']) if r['running'] else 'idle'}",
                 "Tasks: " + (", ".join(f"{n} {s}" for s, n in sorted(by.items())) or "none")]
        items = project_attention(p, state, tasks)
        lines.append("Needs attention: " + ("nothing" if not items else ""))
        for i in items[:15]:
            lines.append(f"- {i.get('task')}: {i.get('kind')} — {str(i.get('reason') or i.get('title') or '')[:160]}")
        rd = reading_view(p.account.get("name", "default"))
        if rd:
            lines.append(f"Account usage: {round(rd['utilization'] * 100)}% ({rd['source']}, {rd['age_min']} min ago)")
        cs = core.chat_settings("coworker")
        cap = float(cs.get("daily_budget_usd") or 0)
        lines.append(f"Coworker spending today: ${self.daily_spent():.2f}" + (f" of ${cap:.2f}" if cap else ""))
        lines.append("Recent log:")
        lines += [f"  {ln[:160]}" for ln in tail(p.path("agentdesk.log"), 5)]
        return "\n".join(lines)

    def watch_run_bg(self) -> None:
        """Wake the Coworker when the current run ends (polls cheaply; no model calls while waiting)."""
        if self.watching_run:
            return
        self.watching_run = True

        def loop():
            try:
                deadline = time.time() + 6 * 3600
                while time.time() < deadline:
                    time.sleep(5)
                    if not runner(self.p).running():
                        self.inject("[Run finished] " + self.checkup_report(), "⏱ The run ended; waking the Coworker")
                        return
            finally:
                self.watching_run = False

        threading.Thread(target=loop, daemon=True, name=f"watch-run-{self.p.slug}").start()

    def schedule_checkup(self, minutes: float) -> None:
        def later():
            time.sleep(minutes * 60)
            self.inject(CHECKUP_PROMPT, f"⏱ Checkup after {minutes:g} min")

        threading.Thread(target=later, daemon=True, name=f"checkup-{self.p.slug}").start()

    def usage(self) -> dict:
        data = core.read_json(self.p.path("chat_usage.json"), {"spent_usd": 0.0, "tokens": {}})
        return data if isinstance(data, dict) else {"spent_usd": 0.0, "tokens": {}}

    def start(self, text: str) -> dict:
        """Answer in the background and return at once: a request held open for minutes gets dropped by the
        window/proxy ("chat falls off"). The page polls /chat until `busy` is false."""
        if self.is_live():  # no waiting: you can write while it works, and its answers stream in as entries
            with self.note_lock:
                self.history.append({"role": "user", "text": text})
                try:
                    self.save_history()
                except OSError:
                    pass
            try:
                return asyncio.run_coroutine_threadsafe(self._live_send(text), self.loop).result(timeout=90)
            except Exception as e:
                self.client = None
                self.busy = False
                msg = f"Error: {e or type(e).__name__}"
                self.note(msg)
                return {"error": msg}
        if self.busy:
            return {"error": "the assistant is still answering the previous message"}
        self.busy = True
        threading.Thread(target=self.ask, args=(text,), daemon=True, name=f"chat-{self.p.slug}").start()
        return {"started": True}

    def ask(self, text: str) -> dict:
        with self.lock:
            self.history.append({"role": "user", "text": text})
            self.save_history()
            self.busy = True
            fut = asyncio.run_coroutine_threadsafe(self._ask(text), self.loop)
            ok = True
            try:
                # Approvals can wait up to 10 minutes each, so the answer may take a while.
                out = fut.result(timeout=3 * APPROVAL_TIMEOUT_S)
            except Exception as e:  # shown in the chat; the dashboard keeps going
                fut.cancel()
                out = {"text": f"Error: {e or type(e).__name__}", "tools": [], "cost": 0.0}
                self.client = None
                ok = False
            finally:
                self.busy = False
            self.history.append({"role": "assistant", **out})
            self.save_history()
            if ok and out.get("text") and out["text"] != "(no answer)":
                self.update_memory(text, out["text"])  # reset() waits for this lock, so it can't be undone by a stale write
            return out

    def reset(self, keep_history: bool = False) -> None:
        """New session (new options); `keep_history` keeps the messages on screen (mode switch)."""
        self.approvals.deny_all()
        client = self.client
        if self.busy and client is not None:
            asyncio.run_coroutine_threadsafe(client.interrupt(), self.loop)
        with self.lock:
            if self.client is not None:
                asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
                self.client = None
            self.generation += 1
            self.mode, self.session_id, self.session_cost = "", None, 0.0
            if not keep_history:
                self.history = []
                self.p.path("chat.json").unlink(missing_ok=True)
                self.p.path("chat_memory.md").unlink(missing_ok=True)

    def info(self, since: int | None = None) -> dict:
        """The chat state. `since`: only the entries added after that seq (the page polls often; the whole
        transcript is 100 KB and more), else the whole transcript."""
        mode = core.chat_mode(self.p)
        cs = core.chat_settings(mode)
        history = self.history if since is None else [m for m in list(self.history) if m.get("seq", 0) > since]
        out = {"history": history, "mode": mode, "busy": self.is_busy(), "model": cs["model"], "seq": self.seq}
        if mode in AUTONOMOUS:
            out.update(budget_usd=float(cs["budget_usd"]))
        out["live"] = self.is_live()
        out["idle_s"] = self.idle_s()
        if out["live"]:
            out.update(daily_budget_usd=float(cs.get("daily_budget_usd") or 0), daily_spent=round(self.daily_spent(), 4))
        if mode == "coworker":
            full = bool(cs.get("full_access"))
            out.update(budget_usd=float(cs["budget_usd"]), full_access=full,
                       worktree=str(self.p.repo if full else core.cowork_path(self.p)),
                       branch=self.p.cfg["repo"]["base"] if full else f"auto/{core.COWORK}")
        return out

    # -- the approved plan: progress notes in the chat, and starting the queue when it's free

    def note(self, text: str) -> None:
        """A progress note in the chat (not from the model); the page fetches notes newer than its last seq."""
        with self.note_lock:
            self.seq += 1
            self.history.append({"role": "assistant", "text": text, "kind": "progress", "seq": self.seq})

    def watch_plan(self) -> None:
        """Poll the approved plan in the background until it's finished or closed."""
        if self.watcher is not None and self.watcher.is_alive():
            return

        def loop():
            while True:
                time.sleep(PLAN_POLL_S)
                try:
                    if not self.plan_tick():
                        return
                except Exception as e:  # the project went away, a file was half written, ...
                    try:
                        core.Store(self.p).log(f"planner: progress watch stopped: {e}")
                    except OSError:
                        pass
                    return

        self.watcher = threading.Thread(target=loop, daemon=True)
        self.watcher.start()

    def plan_tick(self) -> bool:
        """Report the approved plan's progress (task started, done, blocked, paused; the run ending; the
        plan finishing), and start the queue if the plan waits for a run that was going. A blocked task is
        only reported: the plan waits for the user. Returns whether the plan still needs watching."""
        p, notes = self.p, []
        with core.PLAN_LOCK:
            plan = core.load_plan(p)
            if not core.plan_active(plan):
                return False
            state = core.Store(p).load()["tasks"]
            r = runner(p)
            running = r.running()
            ids, seen = plan.get("ids", {}), plan.setdefault("seen", {})
            titles = {ids[t["key"]]: t["title"] for t in plan["tasks"] if t["key"] in ids}
            statuses = {}
            for tid in ids.values():
                e = state.get(tid, {})
                st, before = e.get("status", "pending"), seen.get(tid, "pending")
                statuses[tid] = st
                if st == before:
                    continue
                seen[tid] = st
                what = f"{tid}: {titles.get(tid, '')}"
                if st in ("routing", "working"):
                    if before not in ("routing", "working"):
                        notes.append(f"▶ Started {what}")
                elif st == "done":
                    notes.append(f"✓ Done {what}" + (f" (branch auto/{tid} at {e['commit']})" if e.get("commit") else ""))
                elif st == "blocked":
                    why = next((ln.strip() for ln in str(e.get("last_failure") or "").splitlines() if ln.strip()), "")
                    notes.append(f"✗ Blocked {what}" + (f" — {why[:200]}" if why else "") +
                                 ". The plan waits here: tell me how to go on (I don't change the plan without asking).")
                elif st == "paused":
                    notes.append(f"⏸ Paused {what} (the account usage limit or the run budget was reached).")
            done = sum(s == "done" for s in statuses.values())
            if statuses and done == len(statuses):
                plan["status"] = "finished"
                notes.append(f"Plan finished: all {done} task(s) done. Review the auto/* branches and merge what you want.")
            elif plan.get("start_pending") and not running:
                plan["start_pending"] = False
                msg, running = self._start_queue(plan)
                notes.append(msg)
            elif plan.get("was_running") and not running:
                rest = ", ".join(f"{t} ({s})" for t, s in statuses.items() if s != "done")
                notes.append(f"The run ended: {done} of {len(statuses)} plan tasks done; not done: {rest}. "
                             "Press Start on the plan card to go on.")
            plan["was_running"] = running
            core.save_plan(p, plan)
        for n in notes:
            self.note(n)
        return plan["status"] == "approved"

    def _start_queue(self, plan: dict) -> tuple[str, bool]:
        """Start "Run all" for the approved plan (PLAN_LOCK held; the caller saves `plan`). Returns the chat
        note and whether a run is going now."""
        p = self.p
        r = runner(p)
        if r.running():
            plan["start_pending"] = True
            return "A run is already going; the plan's tasks start when it ends.", True
        if e := r.start(run_args(p, "all") or []):
            return f"Couldn't start the queue: {e}", False
        return "Started the queue (Run all): the tasks run in dependency order; progress shows here.", True

    def start_plan(self) -> str:
        """Start (or go on with) the approved plan's run; the plan card's Start button."""
        with core.PLAN_LOCK:
            plan = core.load_plan(self.p)
            if not core.plan_active(plan):
                raise ValueError("there is no approved plan to start")
            msg, plan["was_running"] = self._start_queue(plan)
            core.save_plan(self.p, plan)
        self.note(msg)
        self.watch_plan()
        return msg

    def approve_plan(self) -> dict:
        """The user's "Approve and start": the drafts become tasks and the queue starts."""
        ids = core.approve_plan(self.p)
        self.note(f"Plan approved: tasks {', '.join(ids.values())} are queued.")
        msg = self.start_plan()
        return {"ok": True, "ids": ids, "note": msg}

    def cancel_plan(self) -> dict:
        n = core.cancel_plan(self.p)
        self.note(f"Plan cancelled: {n} draft task(s) removed.")
        return {"ok": True, "removed": n}

    def stop_plan(self) -> dict:
        """The plan card's Stop: the same as the stop_run tool, without an approval card."""
        if e := runner(self.p).stop():
            raise ValueError(e)
        self.note("Stopping the run (the plan's remaining tasks stay queued; Start goes on).")
        return {"ok": True}

    def close_plan(self) -> dict:
        if runner(self.p).running() and core.plan_active(core.load_plan(self.p)):
            raise ValueError("the plan's run is going: stop it first")
        core.close_plan(self.p)
        self.note("Plan closed; its tasks stay in the queue.")
        return {"ok": True}


def budget_note(result, cost: float, mode: str = "coworker") -> str | None:
    """The chat's message when a coworker/planner message hit `[chat.<mode>] budget_usd`."""
    if result is None or result.subtype != "error_max_budget_usd":
        return None
    budget = float(core.chat_settings(mode)["budget_usd"])
    return (f"Stopped: this message spent {usd(cost)} and reached the {mode.title()} budget of {usd(budget)} per message "
            f"(`[chat.{mode}] budget_usd` in settings.toml). Send another message to continue.")


def usd(x: float) -> str:
    return f"${x:.2f}" if x >= 0.1 or x == 0 else f"${x:.4f}"


LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
CHATS: dict[str, Chat] = {}


def chat(p: core.Project) -> Chat:
    with LOCK:
        if p.slug not in CHATS:
            CHATS[p.slug] = Chat(LOOP, p)
            if core.plan_active(core.load_plan(p)):  # e.g. after a dashboard restart
                CHATS[p.slug].watch_plan()
        return CHATS[p.slug]


# ----------------------------------------------------------------------------- accounts

_AUTH_CACHE: dict[str, tuple[float, dict]] = {}


def auth_status(a: dict, fresh: bool = False) -> dict:
    """Whether the Claude CLI is really signed in for this account: `claude auth status` run against the
    account's own login folder. {"connected": bool | None, "email": str, "method": str}; connected is None
    when the CLI couldn't be asked. Cached for a few seconds because the dashboard polls."""
    name = a.get("name", "default")
    hit = _AUTH_CACHE.get(name)
    if fresh:
        return _auth_query(a)
    if hit is None or time.time() - hit[0] >= 60:
        # Never make a dashboard request wait for the CLI: refresh in the background, answer with the last value.
        if name not in _AUTH_BUSY:
            _AUTH_BUSY.add(name)
            threading.Thread(target=_auth_refresh, args=(a,), daemon=True, name="auth-status").start()
    return hit[1] if hit else {"connected": None, "email": "", "method": ""}


_AUTH_BUSY: set[str] = set()


def _auth_refresh(a: dict) -> None:
    try:
        _auth_query(a)
    finally:
        _AUTH_BUSY.discard(a.get("name", "default"))


def _auth_query(a: dict) -> dict:
    name = a.get("name", "default")
    out: dict = {"connected": None, "email": "", "method": ""}
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        r = subprocess.run([core.bundled_cli(), "auth", "status"], capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL, env={**os.environ, **core.account_env(a)}, **kw)
        info = json.loads(r.stdout or "{}")
        out = {"connected": bool(info.get("loggedIn")), "email": str(info.get("email") or ""),
               "method": str(info.get("authMethod") or "")}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    _AUTH_CACHE[name] = (time.time(), out)
    return out


def auto_error(name: str) -> str | None:
    """Why the automatic usage reading last failed (None if it works or is off)."""
    return (core.account_usage(name).get("auto") or {}).get("error") if core.usage_auto_enabled() else None


def usage_loop() -> None:
    """Reads the real usage of every subscription account about once a minute while the dashboard runs."""
    while not QUIT.is_set():
        try:
            for a in core.settings().get("accounts", []):
                if a.get("kind") != "api_key":
                    core.refresh_usage(a.get("name", "default"))
        except Exception as e:
            log_error(f"usage reader: {type(e).__name__}: {e}")
        QUIT.wait(60)


def reading_view(name: str) -> dict | None:
    r = core.current_reading(name)
    if r is None:
        return None
    return {"utilization": r["utilization"], "source": r["source"], "at": r["at"], "age_min": round(r["age_min"], 1),
            "stale": r["age_min"] > core.usage_max_age_min(),
            **{k: r[k] for k in ("five_hour", "seven_day", "five_hour_resets", "seven_day_resets") if k in r}}


def account_rows() -> list[dict]:
    rows = []
    for a in core.settings().get("accounts", []):
        cfg_dir = Path(a["config_dir"]).expanduser() if a.get("config_dir") else Path.home() / ".claude"
        if a.get("kind") == "api_key":
            ready = bool(os.environ.get(a.get("key_env", "")))
            where = f"key from ${a.get('key_env', '')}" + ("" if ready else " (not set)")
        else:
            st = auth_status(a)
            ready = st["connected"] if st["connected"] is not None else ((cfg_dir / ".credentials.json").exists() or sys.platform == "darwin")
            where = str(cfg_dir)
        rows.append({"name": a.get("name"), "kind": a.get("kind", "subscription"), "where": where, "ready": ready,
                     "email": st["email"] if a.get("kind") != "api_key" else "",
                     "usage": core.account_usage(a.get("name", "default")).get("rate_limits", {}),
                     "reading": reading_view(a.get("name", "default")), "max_age_min": core.usage_max_age_min(),
                     "auto_error": auto_error(a.get("name", "default")) if a.get("kind") != "api_key" else None})
    return rows


def add_account(name: str, kind: str, value: str) -> str | None:
    if not ID.fullmatch(name):
        return "account names use letters, digits, - and _"
    if any(a.get("name") == name for a in core.settings().get("accounts", [])):
        return "an account with that name exists"
    if kind == "api_key":
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            return "give the name of an environment variable holding the key, e.g. ANTHROPIC_API_KEY_WORK"
        block = f'\n[[accounts]]\nname = "{name}"\nkind = "api_key"\nkey_env = "{value}"\n'
    else:
        folder = value or str(core.DATA / "logins" / name)
        Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
        block = f'\n[[accounts]]\nname = "{name}"\nkind = "subscription"\nconfig_dir = {core.toml_str(Path(folder).as_posix())}\n'
    path = core.DATA / "settings.toml"
    text = path.read_text(encoding="utf-8")
    # Accounts go before the first non-account table so they stay one list.
    at = text.find("\n[limits]")
    path.write_text(text[:at] + block + text[at:] if at >= 0 else text + block, encoding="utf-8")
    return None


def open_login(name: str) -> str | None:
    """Open a console running the Claude CLI for this account; the user types /login there."""
    a = core.account(name)
    if a.get("kind") == "api_key":
        return "API-key accounts don't log in; set the environment variable instead"
    env = {**os.environ, **core.account_env(a)}
    cli = core.bundled_cli()
    _AUTH_CACHE.pop(name, None)  # the next status check asks the CLI again
    try:
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "Claude login", "cmd", "/k", cli, "/login"], env=env)
        elif sys.platform == "darwin":
            cfg = env.get("CLAUDE_CONFIG_DIR", "")
            script = f'tell app "Terminal" to do script "CLAUDE_CONFIG_DIR={cfg} \\"{cli}\\" /login"'
            subprocess.Popen(["osascript", "-e", script])
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", cli, "/login"], env=env)
    except OSError as e:
        return f"couldn't open a terminal: {e}"
    return None


# ----------------------------------------------------------------------------- views

def tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 64_000))
        return f.read().decode("utf-8", "replace").splitlines()[-n:]


def task_used(runs: list[dict]) -> float | None:
    """Percent of the usage window the task's runs used; None when no run has a known value."""
    known = [r["usage_used_pct"] for r in runs if r.get("usage_used_pct") is not None]
    return round(sum(known), 1) if known else None


def tier_averages(tasks: list[dict]) -> dict:
    by: dict[str, list[float]] = {}
    for t in tasks:
        if t["used_pct"] is not None and t["tier"]:
            by.setdefault(t["tier"], []).append(t["used_pct"])
    return {k: {"avg_pct": round(sum(v) / len(v), 1), "tasks": len(v)} for k, v in by.items()}


def task_row(t: core.Task, e: dict) -> dict:
    """A task as the dashboard lists it: the task file plus its state.json entry (runs, totals, result)."""
    runs = e.get("runs", [])
    tok: dict[str, int] = {}
    for r in runs:
        for k, v in (r.get("tokens") or {}).items():
            tok[k] = tok.get(k, 0) + v
    return {"id": t.id, "title": t.title, "depends": t.depends, "tier": e.get("tier") or t.tier, "status": e.get("status", "pending"),
            "used_pct": task_used(runs), "why": e.get("why", ""), "runs": runs, "cost": round(sum((r.get("cost_usd") or 0) for r in runs), 4), "tokens": tok,
            "commit": e.get("commit"), "branch": e.get("branch"), "failure": e.get("last_failure", "")}


def task_log(p: core.Project, task_id: str, n: int = 80) -> list[str]:
    """The task's own lines of agentdesk.log (the timeline of runs from before runs/<id>.jsonl was recorded)."""
    f = p.path("agentdesk.log")
    if not f.exists():
        return []
    with f.open("rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 512_000))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    return [ln for ln in lines if f"] {task_id}: " in ln or f"before {task_id}:" in ln][-n:]


def task_detail(p: core.Project, t: core.Task) -> dict:
    """The task detail panel: prompt (task body), orchestrator handoff, the agent's activity and its report."""
    e = core.Store(p).load()["tasks"].get(t.id, {})
    row = task_row(t, e)
    worker = [r for r in row["runs"] if r.get("tier") != "router"]
    tier = row["tier"]
    planned = core.settings().get("tiers", {}).get(tier, {}) if tier else {}
    last = worker[-1] if worker else {}
    wt = Path(p.cfg["repo"]["worktrees"]) / t.id
    rep = p.path("reports") / f"{t.id}.md"
    activity = core.read_activity(p, t.id)
    handoff = {"tier": tier, "model": last.get("model") or planned.get("model"), "effort": last.get("effort", planned.get("effort")),
               "why": row["why"], "brief": e.get("brief", ""), "depends": t.depends, "branch": row["branch"] or (f"auto/{t.id}" if wt.exists() else None),
               "worktree": str(wt) if wt.exists() else None, "commit": row["commit"], "started": e.get("started"), "finished": e.get("finished"),
               "runs": row["runs"]}
    return {"id": t.id, "title": t.title, "status": row["status"], "body": t.body, "brief": handoff["brief"], "handoff": handoff,
            "activity": activity, "log": [] if activity else task_log(p, t.id), "failure": row["failure"],
            "totals": {"runs": len(row["runs"]), "turns": sum(r.get("turns") or 0 for r in row["runs"]), "tokens": row["tokens"], "cost": row["cost"]},
            "report": rep.read_text(encoding="utf-8") if rep.exists() else ""}


def project_state(p: core.Project) -> dict:
    state = core.Store(p).load()
    tasks = [task_row(t, state["tasks"].get(t.id, {})) for t in core.load_tasks(p)]
    s = core.settings()
    mode = core.chat_mode(p)
    cs = core.chat_settings(mode)
    try:
        plan = core.plan_view(p)
    except (ValueError, KeyError) as e:  # a hand-edited plan.json
        plan = {"status": "broken", "summary": f"plan.json can't be read: {e}", "tasks": [], "estimate": [0, 0]}
    return {"project": {"slug": p.slug, "name": p.name, "repo": str(p.repo), "base": p.cfg["repo"]["base"], "account": p.account.get("name"),
                        "gates": p.cfg.get("gates", {}).get("quick", []), "chat_mode": mode,
                        "chat_model": cs["model"] + (f" ({cs['effort']})" if cs.get("effort") else ""),
                        "chat_budget": cs.get("budget_usd"), "cowork": str(core.cowork_path(p))},
            "tasks": tasks, "spent_usd": state.get("spent_usd", 0.0), "tokens": state.get("tokens", {}), "chat": chat(p).usage(),
            "plan": plan, "chat_seq": chat(p).seq, "chat_busy": chat(p).is_busy(), "chat_live": chat(p).is_live(), "chat_idle_s": chat(p).idle_s(),
            "runner": runner(p).info(), "log": tail(p.path("agentdesk.log"), 150), "errors": tail(p.path("runner.log"), 25),
            "limits": {"run_budget": s["limits"]["run_budget_usd"]},
            "usage": core.account_usage(p.account.get("name", "default")).get("rate_limits", {}),
            "reading": reading_view(p.account.get("name", "default")), "max_age_min": core.usage_max_age_min(),
            "auto_error": auto_error(p.account.get("name", "default")) if p.account.get("kind") != "api_key" else None,
            "tier_avg": tier_averages(tasks),
            "tiers": {k: {"model": v["model"], "effort": v.get("effort")} for k, v in s["tiers"].items()}}


def serve_supervised(srv, stop: threading.Event | None = None) -> None:
    """serve_forever that restarts itself: if its loop ever dies with an error the window would stay open on a server
    that no longer answers (it did once), so log it and start the loop again."""
    while True:
        try:
            srv.serve_forever()
            return  # shutdown() was called: finished
        except Exception as e:
            log_error(f"web server loop failed ({type(e).__name__}: {e}); restarting it")
            if stop is not None and stop.wait(0.5):
                return
            time.sleep(0.5 if stop is None else 0)


def quiet_errors(srv):
    """A page that closed or gave up on a request is normal: don't log a traceback for it every time."""
    orig = getattr(srv, "handle_error", None)
    if orig is None:
        return srv

    def handle_error(request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        orig(request, client_address)

    srv.handle_error = handle_error
    return srv


_OV: tuple[float, dict] | None = None
_OV_BUSY = threading.Lock()


def _overview_refresh() -> None:
    global _OV
    try:
        _OV = (time.time(), _overview())
    finally:
        _OV_BUSY.release()


def overview() -> dict:
    """Fresh for 3 s. After that the last answer is returned at once while one background thread recomputes it
    (it walks every project and runs git): a dashboard request never waits for the walk, except the very first."""
    if _OV is None:
        with _OV_BUSY:  # first call (or several at once): compute once, the others wait for it
            if _OV is None:
                globals()["_OV"] = (time.time(), _overview())
        return _OV[1]
    if time.time() - _OV[0] >= 3 and _OV_BUSY.acquire(blocking=False):
        threading.Thread(target=_overview_refresh, daemon=True, name="overview").start()
    return _OV[1]


def _overview() -> dict:
    rows = []
    for p in core.projects():
        state = core.Store(p).load()
        counts: dict[str, int] = {}
        for t in core.load_tasks(p):
            st = state["tasks"].get(t.id, {}).get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        rows.append({"slug": p.slug, "name": p.name, "repo": str(p.repo), "account": p.account.get("name"), "counts": counts,
                     "spent_usd": state.get("spent_usd", 0.0), "running": runner(p).running(),
                     "auto": autorun_status(p), "auto_enabled": core.autorunner_cfg(p)["enabled"], "disk": hygiene.summary(p.slug)})
    return {"auto_global": core.autorunner_cfg()["enabled"], "attention": attention_all(), "projects": rows, "accounts": account_rows(), "presets": list(core.PRESETS), "data": str(core.DATA), "version": core.version_info()}


# ----------------------------------------------------------------------------- auto-runner

# ----------------------------------------------------------------------------- in-app updater

UPDATE_JOB: dict = {"state": "idle", "done": 0, "total": 0, "error": ""}  # idle / downloading / launching / error
_UPDATE_JOB_LOCK = threading.Lock()


def any_run_active() -> bool:
    try:
        return any(run_alive(p)[0] or runner(p).running() for p in core.projects())
    except Exception:
        return False


def busy_info() -> dict:
    """GET /api/busy: is work going on that must keep this instance running (a run, a chat turn, a build)?"""
    runs = any_run_active()
    chats = any(c.busy for c in list(CHATS.values()))
    try:
        builds = any(core.build_job(p).running() for p in core.projects())
    except Exception:
        builds = False
    return {"busy": runs or chats or builds, "runs": runs, "chats": chats, "builds": builds}


def update_status(force: bool = False) -> dict:
    """GET /api/update. Dev mode (not installed) only reports; a disabled check answers "unknown" without a request."""
    try:
        enabled = bool(core.settings().get("updates", {}).get("check", True))
    except Exception:
        enabled = True
    if not enabled and not force:
        info = {"status": "disabled", "current": core.version_info()["version"], "latest": None, "available": False, "notes": ""}
    else:
        info = core.check_update(force)
    out = {k: info.get(k) for k in ("status", "current", "latest", "available", "notes")}
    # The in-app updater is Windows-only; on macOS / Linux the badge links to the release page (download by hand).
    out.update(installable=core.FROZEN and os.name == "nt", link_only=os.name != "nt", page=info.get("page") or core.RELEASES_PAGE,
               enabled=enabled, job=dict(UPDATE_JOB), run_active=any_run_active())
    return out


def update_open() -> dict:
    """POST /api/update/open: show the latest release's page in the browser (macOS / Linux update)."""
    page = core.check_update().get("page") or core.RELEASES_PAGE
    if not core.update_url_ok(page):
        page = core.RELEASES_PAGE
    webbrowser.open(page)
    return {"ok": True, "page": page}


def launch_installer(path: Path) -> None:
    """Run the installer silently, detached, then start the new AgentDesk (the installer's [Run] entry skips when silent)."""
    exe = core.APP / "AgentDesk.exe"
    cmd = f'"{path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS & start "" "{exe}"'
    subprocess.Popen(["cmd.exe", "/c", cmd], close_fds=True, cwd=str(path.parent),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _update_worker(info: dict) -> None:
    def prog(n, total):
        UPDATE_JOB.update(done=n, total=total)
    try:
        path = core.download_update(info, prog)
        UPDATE_JOB["state"] = "launching"
        launch_installer(path)
        time.sleep(1.0)
        QUIT.set()  # main() stops the server and the process ends, so the installer can replace the files
    except Exception as e:
        UPDATE_JOB.update(state="error", error=f"{type(e).__name__}: {e}"[:300])


def update_install(confirm: bool = False) -> dict:
    """POST /api/update/install: download in the background, then launch the installer and exit."""
    if not core.FROZEN or os.name != "nt":
        return {"ok": False, "message": "updating in place only works in the installed program"}
    info = core.check_update()
    if not info.get("available"):
        return {"ok": False, "message": "no update available"}
    if any_run_active() and not confirm:
        return {"ok": False, "needs_confirm": True, "message": "a worker run is active; updating stops AgentDesk (runs keep going only if you confirm)"}
    with _UPDATE_JOB_LOCK:
        if UPDATE_JOB["state"] in ("downloading", "launching"):
            return {"ok": True, "message": "already updating"}
        UPDATE_JOB.update(state="downloading", done=0, total=0, error="")
    threading.Thread(target=_update_worker, args=(info,), daemon=True, name="updater").start()
    return {"ok": True}


QUIT = threading.Event()  # set when the dashboard shuts down; stops the auto-runner loop
FLAGGED: dict[str, set[str]] = {}  # project -> flags already written to its log


def run_alive(p: core.Project) -> tuple[bool, int | None]:
    """(is a run process alive, its exit code if this dashboard started it and it has ended)."""
    r = runner(p)
    if r.running():
        return True, None
    try:
        pid = int(p.path("runner.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if r.proc is not None and r.proc.pid == pid:
        return False, r.proc.returncode
    return core.pid_alive(pid), None


def project_attention(p: core.Project, state: dict | None = None, tasks: list | None = None) -> list[dict]:
    state = state if state is not None else core.Store(p).load()
    tasks = tasks if tasks is not None else core.load_tasks(p)
    merged = {t.id: core.branch_merged(p, e["branch"]) for t in tasks
              if (e := state["tasks"].get(t.id, {})).get("status") == "done" and e.get("branch")}
    return core.attention_items(p.slug, tasks, state, merged, core.autorunner_cfg(p)["retry_crashed"])


def autorun_status(p: core.Project, items: list[dict] | None = None) -> str:
    cfg = core.autorunner_cfg(p)
    if not cfg["enabled"]:
        return "Auto: off"
    if items if items is not None else project_attention(p):
        return "waiting: needs attention"
    return "Auto: on"


def autorun_project(p: core.Project, starter=None, alive=None) -> list[str]:
    """One pass for one project. Returns what it did (for tests/logs). It only reads state and pids; the one
    mutation besides starting a run is putting a dead run's tasks back to pending."""
    starter = starter or (lambda proj, args: runner(proj).start(args))
    alive = alive or run_alive
    did = []
    cfg = core.autorunner_cfg(p)
    store = core.Store(p)
    state = store.load()
    tasks = core.load_tasks(p)
    running, code = alive(p)
    if cfg["enabled"]:
        stale = core.recover_stale(state, running, code)
        if stale:
            store.save(state)
            for tid in stale:
                store.log(f"auto-runner: {tid} was working but its run is gone; set back to pending ({state['tasks'][tid]['last_failure']})")
                did.append(f"recovered {tid}")
    items = project_attention(p, state, tasks)
    seen = FLAGGED.setdefault(p.slug, set())
    for it in items:  # flags go to the project's log once; nothing else is done about them
        key = f"{it['task']}|{it['kind']}|{it['reason']}"
        if key not in seen:
            seen.add(key)
            store.log(f"needs attention: {it['task']} ({it['kind']}): {it['reason']}")
    if not cfg["enabled"]:
        return did
    if chat(p).approvals.pending():
        return did
    if hygiene.cleaning(p.slug):  # a disk cleanup is deleting build caches: start the next task after it
        return did
    t = core.next_startable(tasks, state, running, cfg["retry_crashed"])
    if t is None:
        return did
    cap = core.worktree_cap_reason(p, t.id)
    if cap:  # [hygiene] max_worktrees: wait until disk hygiene has removed a finished worktree
        if f"cap|{cap}" not in seen:
            seen.add(f"cap|{cap}")
            store.log(f"auto-runner: {t.id} waits: {cap}")
        return did
    gate = core.disk_gate()
    if gate:  # the server's disk budget / quota: logged once, retried every pass
        if f"gate|{gate}" not in seen:
            seen.add(f"gate|{gate}")
            store.log(f"auto-runner: {t.id} waits: {gate}")
        return did
    seen.difference_update({k for k in seen if k.startswith(("cap|", "gate|"))})
    again = int(state["tasks"].get(t.id, {}).get("crashes", 0)) > 0
    err = starter(p, run_args(p, "task", t.id) or [])
    if err:
        store.log(f"auto-runner: could not start {t.id}: {err}")
    else:
        store.log(f"auto-runner: started {t.id}" + (" (retry after crash)" if again else ""))
        did.append(f"started {t.id}")
    return did


def autorun_pass(starter=None, alive=None) -> dict[str, list[str]]:
    out = {}
    for p in core.projects():
        try:
            out[p.slug] = autorun_project(p, starter, alive)
        except Exception as e:  # one broken project must not stop the loop
            log_error(f"auto-runner: {p.slug}: {type(e).__name__}: {e}")
    return out


def checkup_loop() -> None:
    """Wakes the live Coworker of each project every `[chat.coworker] checkup_interval_min` minutes, but only while a
    session is open (the user chatted since the dashboard started), it is idle, and today's budget isn't spent."""
    while not QUIT.wait(30):
        try:
            for c in list(CHATS.values()):
                c.watch_idle()
            every = float(core.chat_settings("coworker").get("checkup_interval_min") or 0)
            if every <= 0:
                continue
            for p in core.projects():
                c = chat(p)
                if c.is_live() and c.client is not None and not c.busy and time.time() - c.last_checkup >= every * 60:
                    c.last_checkup = time.time()
                    c.inject(CHECKUP_PROMPT, "⏱ Scheduled checkup")
        except Exception as e:
            log_error(f"checkups: {type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- crash / boot recovery, guards

RUN_NAMES = ("agentdesk", "python", "run_agentdesk")  # what a live run's command line contains
INTERRUPTED = "interrupted by a restart"


def recover_project(p: core.Project, log=None) -> list[str]:
    """After a crash or reboot (instance start): drop a stale runner.pid (dead process, or a reused pid), kill
    children left behind by a dead run, put tasks that are `working` with no live run back to pending (counts as a
    crash, so retry_crashed applies), prune orphan and half-created worktrees, and mark a chat that was mid-turn."""
    log = log or log_error
    did: list[str] = []
    store = core.Store(p)
    pidf = p.path("runner.pid")
    try:
        old_pid = int(pidf.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        old_pid = 0
    if reliability.clean_pid_file(pidf, RUN_NAMES):
        did.append("removed stale runner.pid")
        if old_pid > 1 and not reliability.pid_alive(old_pid) and reliability.group_alive(old_pid):
            reliability.kill_group(old_pid)  # git / claude children of a run whose parent died
            did.append("killed orphan children of a dead run")
    alive, code = run_alive(p)
    state = store.load()
    stale = core.recover_stale(state, alive, code)
    if stale:
        store.save(state)
        for tid in stale:
            did.append(f"task {tid} was working with no live run: set back to pending ({state['tasks'][tid]['last_failure']})")
    if not alive:
        did += prune_worktrees(p)
    did += mark_interrupted_chat(p)
    for line in did:
        store.log(f"recovery: {line}")
        log(f"recovery [{p.slug}]: {line}")
    return did


def prune_worktrees(p: core.Project) -> list[str]:
    """`git worktree prune`, and remove worktree folders with no .git entry (an add that died half way)."""
    did = []
    repo = p.cfg.get("repo", {}).get("path")
    if repo and Path(repo).is_dir():
        core.git(["worktree", "prune"], Path(repo))
    root = Path(p.cfg.get("repo", {}).get("worktrees") or "")
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and not (d / ".git").exists():
                shutil.rmtree(d, ignore_errors=True)
                did.append(f"removed half-created worktree {d.name}")
    return did


def mark_interrupted_chat(p: core.Project) -> list[str]:
    """A transcript that ends with the user's message or an unfinished answer gets an 'interrupted by a restart' note."""
    f = p.path("chat.json")
    try:
        hist = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(hist, list) or not hist or not isinstance(hist[-1], dict):
        return []
    last = hist[-1]
    mid = last.get("role") == "user" or (last.get("kind") in ("live", "progress") and last.get("cost") is None)
    if not mid:
        return []
    seq = max([int(h.get("seq", 0)) for h in hist if isinstance(h, dict)] + [0]) + 1
    hist.append({"role": "assistant", "kind": "interrupted", "text": "⚠ " + INTERRUPTED, "seq": seq})
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(hist, indent=2, ensure_ascii=False), encoding="utf-8")
    core.replace_file(tmp, f)
    return ["chat was mid-turn: marked as interrupted"]


def recover_all(log=None) -> list[str]:
    """Everything above for every project, plus the dashboard's own stale pid file."""
    out = []
    if reliability.clean_pid_file(pid_file(), RUN_NAMES):
        out.append("removed stale dashboard.pid")
    for p in core.projects():
        try:
            out += recover_project(p, log)
        except Exception as e:  # one broken project must not keep the server from starting
            (log or log_error)(f"recovery [{p.slug}] failed: {type(e).__name__}: {e}")
    return out


def run_age_s(p: core.Project) -> float | None:
    try:
        return time.time() - p.path("runner.pid").stat().st_mtime
    except OSError:
        return None


def guard_pass() -> list[str]:
    """Stuck-child guard (one pass): a run older than [autorunner] run_timeout_min is killed with its children.
    Also rotates the runner logs. Returns what it did. (The tasks are then recovered as crashed.)"""
    did = []
    for p in core.projects():
        reliability.rotate(p.path("runner.log"), copytruncate=True)
        age = run_age_s(p)
        limit = core.autorunner_cfg(p)["run_timeout_min"] * 60
        if age is None or not limit or age <= limit:
            continue
        alive, _ = run_alive(p)
        if not alive:
            continue
        r = runner(p)
        if r.proc is not None and r.proc.poll() is None:
            core.kill_tree(r.proc)
        else:
            try:
                reliability.kill_group(int(p.path("runner.pid").read_text(encoding="utf-8").split()[0]))
            except (OSError, ValueError, IndexError):
                pass
        msg = f"run exceeded the hard timeout ({limit // 60} min): killed with its children"
        core.Store(p).log(f"guard: {msg}")
        log_error(f"guard [{p.slug}]: {msg}")
        did.append(f"{p.slug}: {msg}")
    return did


def guard_loop() -> None:
    while not QUIT.wait(30):
        try:
            guard_pass()
            reliability.rotate(core.DATA / "agentdesk.log", copytruncate=True)
        except Exception as e:
            log_error(f"guard: {type(e).__name__}: {e}")


def shutdown_runs(grace_s: float = 20.0) -> list[str]:
    """Graceful stop (SIGTERM of the service): ask every live run to end, wait up to grace_s, then kill the rest with
    their children. Tasks left `working` are put back to pending by recover_project at the next start."""
    pids = []
    for p in core.projects():
        alive, _ = run_alive(p)
        if not alive:
            continue
        try:
            pid = int(p.path("runner.pid").read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            continue
        if pid == os.getpid():
            continue
        pids.append(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace_s
    while pids and time.monotonic() < deadline and any(reliability.pid_alive(x) for x in pids):
        time.sleep(0.2)
    left = [x for x in pids if reliability.pid_alive(x)]
    for x in left:
        reliability.kill_group(x)
    return [f"run pid {x} killed after {grace_s:.0f}s" for x in left]


def autorun_loop() -> None:
    """Waits first (a short-lived dashboard never acts), then one pass per interval until QUIT is set."""
    interval = core.autorunner_cfg()["interval_s"]
    while not QUIT.wait(interval):
        try:
            cfg = core.autorunner_cfg()
            interval = cfg["interval_s"]
            if cfg["enabled"]:
                autorun_pass()
        except Exception as e:
            log_error(f"auto-runner: {type(e).__name__}: {e}")
            interval = 30


def start_run(p: core.Project, args: list) -> str | None:
    """Start a worker run, unless a disk cleanup is deleting build caches / worktrees of this project right now."""
    if hygiene.cleaning(p.slug):
        return "a disk cleanup is running; try again in a moment"
    return runner(p).start(args)


def project_busy(p: core.Project) -> bool:
    """A run or a dashboard build is going for the project: disk hygiene deletes nothing meanwhile."""
    return run_alive(p)[0] or core.build_job(p).running()


def hygiene_loop() -> None:
    """Disk hygiene in the background: every minute (the first after a minute) it measures each project whose last
    measurement is older than its [hygiene] interval_min and, only where auto = true, cleans up."""
    while not QUIT.wait(60):
        try:
            hygiene.loop_pass(run_active=project_busy, log=log_error)
        except Exception as e:
            log_error(f"disk hygiene: {type(e).__name__}: {e}")


def disk_view(p: core.Project, refresh: bool = False) -> dict:
    """GET /api/p/<slug>/disk: the cached disk report and cleanup plan (measured in the background, never here)."""
    return {**hygiene.report(p, refresh, log=log_error), "run_active": project_busy(p)}


def disk_clean(p: core.Project, d: dict) -> dict:
    """POST /api/p/<slug>/disk/clean: without `apply` a dry run (the plan, nothing deleted); with apply, confirm and
    the chosen action ids, the cleanup runs in the background (each action is checked again first)."""
    if d.get("apply") is not True:
        return {"ok": True, "dry_run": True, **disk_view(p)}
    if d.get("confirm") is not True:
        return {"ok": False, "message": "this needs confirmation"}
    ids = d.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
        return {"ok": False, "message": "choose what to clean up"}
    if project_busy(p):
        return {"ok": False, "message": "a run or build is in progress for this project; nothing was deleted"}
    return hygiene.start_clean(p, ids, run_active=project_busy, log=log_error)


def attention_all() -> dict:
    items = []
    for p in core.projects():
        for it in project_attention(p):
            items.append({**it, "name": p.name})
    return {"count": len(items), "items": items}


# ----------------------------------------------------------------------------- git panel: GitHub (gh) routes
# Each request runs in its own server thread (ThreadingHTTPServer), so slow git/gh calls never hold up the rest of
# the dashboard; every call has a timeout and gh answers are cached for a few seconds (core.GH_TTL).

GIT_GH_POSTS = ("remote", "account", "switch", "pr", "open")
_PR_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_.~%/+#?=&-]*")


def git_get(p: core.Project, what: str, q: dict) -> dict:
    """GET /api/p/<slug>/git/<what>: gh (accounts and remote), repos, prs, checks. `refresh=1` skips the cache."""
    refresh = q.get("refresh") in ("1", "true")
    if what == "gh":
        return {**core.gh_accounts(refresh), "account": core.git_account(p), "remote": core.git_remote_info(p)}
    if what == "repos":
        return core.gh_repo_list(p, str(q.get("owner") or ""), refresh)
    if what == "prs":
        return core.git_prs(p, refresh)
    if what == "checks":
        n = str(q.get("number") or "")
        return core.git_pr_checks(p, int(n), refresh) if n.isascii() and n.isdigit() else {"ok": False, "message": "which pull request?"}
    return {"ok": False, "error": "unknown git view"}


def git_post(p: core.Project, what: str, d: dict) -> dict:
    """POST /api/p/<slug>/git/<what> (confirmed by the user, except open): remote, account, switch, pr, open."""
    if what == "open":
        url = str(d.get("url") or "")
        if not _PR_URL.fullmatch(url):
            return {"ok": False, "message": "not a web address"}
        webbrowser.open(url)
        return {"ok": True}
    if what == "remote":
        return {**core.git_set_remote(p, str(d.get("url") or "")), "status": core.git_status(p)}
    if what == "account":
        try:
            core.set_git_account(p, str(d.get("account") or ""))
        except ValueError as e:
            return {"ok": False, "message": str(e)}
        who = core.git_account(p)
        return {"ok": True, "account": who, "message": f"This project now uses the GitHub account {who}." if who
                else "This project now uses git's own credentials and gh's active account."}
    if what == "switch":
        return core.gh_switch(str(d.get("account") or ""), str(d.get("host") or "github.com"))
    return core.git_pr_create(p, str(d.get("title") or ""), str(d.get("body") or ""), str(d.get("head") or ""),
                              str(d.get("base") or ""), d.get("draft") is True)


# ----------------------------------------------------------------------------- request guard

GATEWAY_HEADER = "X-AgentDesk-Gateway-Token"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class Guard:
    """Which Host / Origin values a request may carry (DNS-rebinding / CSRF protection) and the optional shared
    secret every request must send as X-AgentDesk-Gateway-Token (how the gateway talks to an instance).
    allowed: entries "host" (any port) or "host:port"; None = loopback names on any port (the desktop default)."""

    def __init__(self, allowed=None, token: str | None = None):
        self.allowed = None if allowed is None else {a.strip().lower() for a in allowed if a.strip()}
        self.token = token or None

    @staticmethod
    def split(hostport: str) -> tuple[str, str]:
        hostport = hostport.strip().lower()
        if hostport.startswith("["):
            host, _, rest = hostport.partition("]")
            return host + "]", rest.lstrip(":")
        host, _, port = hostport.partition(":")
        return host, port

    def host_ok(self, hostport: str) -> bool:
        host, port = self.split(hostport)
        if not host:
            return False
        if host in LOOPBACK_NAMES:
            return True
        if self.allowed is None:
            return False
        return hostport.strip().lower() in self.allowed or host in self.allowed

    def origin_ok(self, origin: str) -> bool:
        m = re.fullmatch(r"https?://([^/\s]+)", origin.strip(), re.I)
        return bool(m) and self.host_ok(m.group(1))

    def token_ok(self, given: str | None) -> bool:
        if self.token is None:
            return True
        return hmac.compare_digest((given or "").encode("utf-8"), self.token.encode("utf-8"))

    def check(self, host: str | None, origin: str | None, token: str | None, post: bool) -> tuple[int, str] | None:
        """None if the request may go on, else (status, reason)."""
        if not self.token_ok(token):
            return 401, "missing or wrong gateway token"
        if not self.host_ok(host or ""):
            return 403, "forbidden host"
        if post and origin and not self.origin_ok(origin):
            return 403, "forbidden"
        return None


GUARD = Guard()


def default_allowed(host: str, port: int, extra=()) -> list[str]:
    """Allowed Host/Origin names for a bind address: loopback always; the bind address itself (with its port);
    plus anything given with --allowed-host."""
    out = [f"{n}:{port}" if port else n for n in LOOPBACK_NAMES]
    if host not in ("0.0.0.0", "::", ""):
        out.append(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
    return [*out, *extra]


def is_loopback(host: str) -> bool:
    import ipaddress
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# ----------------------------------------------------------------------------- http

def repo_dir_for_import(d: dict, upload: Path | None) -> Path:
    """A fresh, empty code folder under the data folder for an import that named none."""
    name = str(d.get("new_name") or "").strip()
    if not name and upload:
        try:
            name = str(core._read_zip(upload)[0].get("project") or "")
        except Exception:
            name = ""
    if not name:
        url = str(d.get("repo_url") or d.get("source_url") or "").rstrip("/")
        name = re.sub(r"\.git$", "", re.split(r"[/:]", url)[-1]) if url else ""
    base = core.slug(name or "imported")
    root = core.DATA / "repos"
    root.mkdir(parents=True, exist_ok=True)
    n, dest = 1, root / base
    while True:
        try:
            dest.mkdir()
            return dest
        except FileExistsError:
            n += 1
            dest = root / f"{base}-{n}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 200_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def proj(self, slug: str) -> core.Project | None:
        try:
            return core.project(slug)
        except KeyError:
            self.json({"error": "no such project"}, 404)
            return None

    def import_route(self, d: dict, query: dict) -> dict:
        """Import a project: an uploaded .zip (raw body, options in the query), or JSON naming a zip/folder/repo
        (`source`) or a git URL to clone (`source_url`). Never overwrites: a taken name asks for a new one."""
        upload = None
        cloned = None
        auto_repo = None
        gate = core.disk_gate()
        if gate:  # the server's disk budget / this user's quota: no new clones, imports or uploads
            if "zip" in (self.headers.get("Content-Type") or ""):
                self.close_connection = True  # the upload body is not read
            return {"ok": False, "disk_budget": True, "message": f"not importing: {gate}"}
        try:
            if not d and "zip" in (self.headers.get("Content-Type") or ""):
                import tempfile
                n = int(self.headers.get("Content-Length") or 0)
                limit = min(core.MAX_TOTAL_BYTES, int(float(core.server_policy().get("max_upload_mb") or 1e9) * 1024 * 1024))
                if n <= 0 or n > limit:
                    self.close_connection = True
                    return {"ok": False, "message": f"the file is empty or too large (limit {limit // (1024 * 1024)} MB)"}
                spool = core.DATA / "imports"  # swept by disk hygiene after 24 h if an import dies half way
                spool.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=spool)
                with os.fdopen(fd, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise ValueError("the upload broke off")
                        f.write(chunk)
                        left -= len(chunk)
                upload = Path(tmp)
                d = query
            repo = str(d.get("repo") or "").strip()
            if not repo:  # AgentDesk picks the code folder: <data>/repos/<project name>
                auto_repo = repo_dir_for_import(d, upload)
                repo = str(auto_repo)
            if d.get("repo_url"):  # clone the code repo into `repo` first (must be a new/empty folder)
                if core.repo_state(repo) != "plain" or (Path(repo).exists() and any(Path(repo).iterdir())):
                    return {"ok": False, "message": f"{repo} already exists and is not empty; choose a new folder to clone into"}
                core.clone_for_import(str(d["repo_url"]), Path(repo))
            if d.get("source_url") and not upload:
                cloned = core.clone_for_import(str(d["source_url"]), None, str(d.get("source_branch") or "").strip() or None)
            src = upload or cloned or Path(str(d.get("source") or ""))
            if not str(src):
                return {"ok": False, "message": "choose an export file, folder or repository"}
            p = core.import_project(src, repo, str(d.get("new_name") or ""), str(d.get("account") or "") or None,
                                    str(d.get("project") or "") or None, d.get("pull") in (True, "true", "1"))
            auto_repo = None  # kept now
            return {"ok": True, "slug": p.slug, "name": p.name, "repo": str(p.repo), "tasks": len(core.load_tasks(p)),
                    "account": p.account.get("name")}
        except core.NameClash as e:
            return {"ok": False, "name_clash": True, "suggested": e.suggested, "message": str(e)}
        except ValueError as e:  # BadExport is a ValueError
            return {"ok": False, "message": str(e)}
        finally:
            if auto_repo is not None:  # the import failed: remove the folder we created
                shutil.rmtree(auto_repo, ignore_errors=True)
            if upload:
                upload.unlink(missing_ok=True)
            if cloned is not None and hygiene.inside(cloned, core.DATA / "imports"):  # the temporary config-repo clone
                hygiene.safe_delete(core.DATA / "imports", cloned)

    def guard(self, post: bool) -> bool:
        bad = GUARD.check(self.headers.get("Host"), self.headers.get("Origin"), self.headers.get(GATEWAY_HEADER), post)
        if bad:
            self.json({"error": bad[1]}, bad[0])
        return bad is None

    def do_GET(self):
        if not self.guard(False):
            return None
        parts = [x for x in self.path.split("?")[0].split("/") if x]
        if not parts or parts == ["index.html"]:
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parts == ["api", "attention"]:
            return self.json(attention_all())
        if parts == ["api", "version"]:
            return self.json(core.version_info())
        if parts == ["api", "update"]:
            return self.json(update_status())
        if parts == ["api", "overview"]:
            return self.json({**overview(), "desktop": _WINDOW is not None})  # desktop: folder pickers work
        if parts == ["api", "busy"]:  # the gateway keeps an instance alive while this says busy
            return self.json(busy_info())
        if len(parts) >= 3 and parts[:2] == ["api", "p"]:
            p = self.proj(parts[2])
            if p is None:
                return None
            if len(parts) == 4 and parts[3] == "state":
                return self.json(project_state(p))
            if len(parts) == 4 and parts[3] == "git":
                return self.json(core.git_status(p))
            if len(parts) == 5 and parts[3] == "git":
                return self.json(git_get(p, parts[4], {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}), 200)
            if len(parts) == 4 and parts[3] == "build":
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                info = core.build_job(p).info()
                if q.get("lite") != "1":  # the log poll during a build skips the git calls of the version/task list
                    info.update(core.build_plan(p, q.get("version") or None))
                return self.json(info)
            if len(parts) == 4 and parts[3] == "disk":
                return self.json(disk_view(p, "refresh=1" in (urllib.parse.urlparse(self.path).query or "")))
            if len(parts) == 4 and parts[3] == "chat":
                since = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("since", [""])[0]
                return self.json(chat(p).info(int(since) if since.isascii() and since.isdigit() else None))
            if len(parts) == 4 and parts[3] == "pending":
                return self.json({"pending": chat(p).approvals.pending()})
            if len(parts) == 5 and parts[3] == "task":
                t = next((t for t in core.load_tasks(p) if t.id == parts[4]), None)
                if t is None:
                    return self.json({"error": "no such task"}, 404)
                return self.json(task_detail(p, t))
        self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self.guard(True):
            return None
        parts = [x for x in self.path.split("?")[0].split("/") if x]
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        upload = parts == ["api", "projects", "import"] and "zip" in (self.headers.get("Content-Type") or "")
        d = {} if upload else self.body()
        try:
            if parts == ["api", "update", "install"]:
                return self.json(update_install(d.get("confirm") is True))
            if parts == ["api", "update", "open"]:
                return self.json(update_open())
            if parts == ["api", "projects"]:
                try:
                    p = core.add_project(str(d.get("name", "")).strip(), str(d.get("repo", "")).strip(), str(d.get("base", "")).strip() or "main",
                                         str(d.get("preset", "none")), str(d.get("account", "default")),
                                         local_git=d.get("local_git") is True)
                except core.NeedsLocalGit as e:
                    return self.json({"needs_local_git": True, "reason": e.reason, "message": str(e)})
                return self.json({"ok": True, "slug": p.slug})
            if parts == ["api", "projects", "import"]:
                return self.json(self.import_route(d, query))
            if parts == ["api", "pick"]:
                return self.json(pick_path(str(d.get("kind") or "folder"), str(d.get("start") or "")))
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "export":
                p = self.proj(parts[2])
                if p is None:
                    return None
                if runner(p).running():
                    return self.json({"ok": False, "message": "a run is in progress for this project; stop it first"})
                logs = d.get("include_logs") is True
                if d.get("mode") == "branch":
                    if d.get("confirm") is not True:
                        return self.json({"ok": False, "message": "this needs confirmation"}, 400)
                    return self.json(core.export_to_branch(p, logs, push=d.get("push") is True))
                if d.get("mode") == "git":
                    if d.get("confirm") is not True:
                        return self.json({"ok": False, "message": "this needs confirmation"}, 400)
                    return self.json(core.export_to_git(p, str(d.get("config_repo") or "").strip() or None, logs, push=d.get("push") is True))
                if d.get("file"):
                    return self.json({"ok": True, "file": str(core.export_project(p, str(d["file"]), logs))})
                return self.send(200, core.export_project(p, None, logs), "application/zip",
                                 {"Content-Disposition": f'attachment; filename="{p.slug}.agentdesk.zip"'})
            if parts == ["api", "autorunner"]:
                core.set_autorunner(None, d.get("enabled") is True)
                return self.json({"ok": True, "enabled": core.autorunner_cfg()["enabled"]})
            if parts == ["api", "accounts"]:
                err = add_account(str(d.get("name", "")).strip(), str(d.get("kind", "subscription")), str(d.get("value", "")).strip())
                return self.json({"error": err} if err else {"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "accounts"] and parts[3] == "usage":
                if not any(a.get("name") == parts[2] for a in core.settings().get("accounts", [])):
                    return self.json({"error": "no such account"}, 404)
                try:
                    core.set_manual_usage(parts[2], float(d.get("percent")))
                except (TypeError, ValueError) as e:
                    return self.json({"error": str(e) if "between" in str(e) else "enter a number from 0 to 100"}, 400)
                return self.json({"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "accounts"] and parts[3] == "login":
                err = open_login(parts[2])
                return self.json({"error": err} if err else {"ok": True})
            if len(parts) >= 4 and parts[:2] == ["api", "p"]:
                p = self.proj(parts[2])
                if p is None:
                    return None
                act = parts[3]
                if act == "run":
                    args = run_args(p, str(d.get("mode") or ""), str(d.get("id") or ""))
                    if args is None:
                        return self.json({"error": "unknown mode"}, 400)
                    err = start_run(p, args)
                    return self.json({"error": err} if err else {"ok": True})
                if act == "git" and len(parts) == 5 and parts[4] in GIT_GH_POSTS:
                    if parts[4] == "remote" and runner(p).running():
                        return self.json({"ok": False, "message": "a run is in progress for this project; stop it first"})
                    if parts[4] != "open" and d.get("confirm") is not True:
                        return self.json({"ok": False, "message": "this needs confirmation"}, 400)
                    return self.json(git_post(p, parts[4], d))
                if act == "git" and len(parts) == 5:
                    if parts[4] not in ("fetch", "merge", "pull", "push"):
                        return self.json({"error": "unknown git action"}, 400)
                    if runner(p).running():
                        return self.json({"ok": False, "message": "a run is in progress for this project; stop it first"})
                    if parts[4] != "fetch" and d.get("confirm") is not True:
                        return self.json({"ok": False, "message": "this needs confirmation"}, 400)
                    if parts[4] == "fetch":
                        res = core.git_fetch(p)
                    elif parts[4] == "merge":
                        res = core.git_merge(p, str(d.get("branch", "")))
                    elif parts[4] == "pull":
                        res = core.git_pull(p)
                    else:
                        res = core.git_push(p, str(d.get("branch") or "") or None)
                    return self.json({**res, "status": core.git_status(p)})
                if act == "build" and len(parts) <= 5:
                    sub = parts[4] if len(parts) == 5 else ""
                    job = core.build_job(p)
                    if sub == "open":
                        err = job.open_folder()
                        return self.json({"error": err} if err else {"ok": True})
                    if sub not in ("", "stop"):
                        return self.json({"error": "unknown build action"}, 400)
                    if d.get("confirm") is not True:
                        return self.json({"ok": False, "message": "this needs confirmation"}, 400)
                    if sub == "stop":
                        err = job.stop()
                    else:
                        sel = None
                        if "version" in d or "tasks" in d:  # a versioned build: build/<version> = base + chosen tasks
                            sel, why = core.check_build_selection(p, d.get("version"), d.get("tasks", []))
                            if why:
                                return self.json({"ok": False, "error": why, "message": why}, 400)
                        blocked = "a task run is in progress for this project; stop it first" if runner(p).running() else None
                        err = job.start(str(d.get("kind") or "build"), str(d.get("ref") or ""), blocked, sel)
                        if sel and not err:
                            core.save_build_selection(p, sel["version"], sel["tasks"], sel["seen"])
                    return self.json({"ok": False, "message": err} if err else {"ok": True})
                if act == "disk" and parts[4:] == ["clean"]:
                    return self.json(disk_clean(p, d))
                if act == "autorunner" and len(parts) == 4:
                    core.set_autorunner(p, d.get("enabled") is True)
                    return self.json({"ok": True, "enabled": core.autorunner_cfg(core.project(p.slug))["enabled"]})
                if act == "stop":
                    err = runner(p).stop()
                    return self.json({"error": err} if err else {"ok": True})
                if act == "tasks":
                    tid = str(d.get("id") or core.next_task_id(p))
                    deps = [x.strip() for x in str(d.get("depends", "")).split(",") if x.strip()]
                    core.add_task(p, tid, str(d.get("title", "")).strip() or "Untitled", str(d.get("body", "")), d.get("tier") or None, deps)
                    return self.json({"ok": True, "id": tid})
                if act == "chat" and len(parts) == 4:
                    text = str(d.get("text", "")).strip()[:4000]
                    if not text:
                        return self.json({"error": "empty"})
                    return self.json(chat(p).start(text))
                if act == "chat" and parts[4:] == ["interrupt"]:
                    return self.json({"ok": chat(p).interrupt()})
                if act == "chat" and parts[4:] == ["reset"]:
                    chat(p).reset()
                    return self.json({"ok": True})
                if act == "chat" and parts[4:] == ["mode"]:
                    mode = str(d.get("mode", ""))
                    core.set_chat_mode(p, mode)
                    chat(p).reset(keep_history=True)
                    return self.json({"ok": True, "mode": mode})
                if act == "plan" and len(parts) == 5:
                    c = chat(p)
                    actions = {"approve": c.approve_plan, "cancel": c.cancel_plan, "start": lambda: {"ok": True, "note": c.start_plan()},
                               "stop": c.stop_plan, "close": c.close_plan}
                    if parts[4] not in actions:
                        return self.json({"error": "unknown plan action"}, 400)
                    return self.json(actions[parts[4]]())
                if act == "pending" and len(parts) == 5:
                    prefix = str(d.get("prefix") or "").strip() or None
                    if not chat(p).approvals.resolve(parts[4], str(d.get("action", "")), prefix):
                        return self.json({"error": "that request isn't waiting any more"}, 404)
                    return self.json({"ok": True})
        except (ValueError, KeyError, OSError) as e:
            return self.json({"error": str(e)}, 400)
        self.send(404, b"not found", "text/plain")


def pid_file() -> Path:
    return core.DATA / "dashboard.pid"


def answers(port: int, host: str = "127.0.0.1") -> bool:
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


WINDOW_DEFAULTS = {"width": 1280, "height": 860}


def window_settings(save: dict | None = None) -> dict:
    """The [window] table of settings.toml. Missing keys are added; existing values are never touched,
    except the ones in `save` (size/position remembered when the window closes)."""
    path = core.DATA / "settings.toml"
    core.settings()
    text = path.read_bytes().decode("utf-8")  # bytes: keep CRLF visible
    cur = core.settings().get("window", {})
    want = {**{k: v for k, v in WINDOW_DEFAULTS.items() if k not in cur}, **(save or {})}
    if not want:
        return cur
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    head = next((n for n, ln in enumerate(lines) if ln.strip() == "[window]"), None)
    if head is None:
        lines += ["", "[window]"] + [f"{k} = {int(v)}" for k, v in want.items()]
    else:
        end = next((n for n in range(head + 1, len(lines)) if lines[n].lstrip().startswith("[")), len(lines))
        for k, v in want.items():
            at = next((n for n in range(head + 1, end) if re.match(rf"\s*{k}\s*=", lines[n])), None)
            if at is None:
                lines.insert(head + 1, f"{k} = {int(v)}")
                end += 1
            else:
                lines[at] = f"{k} = {int(v)}"
    new = nl.join(lines) + nl
    try:
        tomllib.loads(new)  # never write a settings file the app can't read back
    except tomllib.TOMLDecodeError as e:
        log_error(f"AgentDesk: not saving window settings, settings.toml would be invalid ({e})")
        return cur
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8", newline="")
    core.replace_file(tmp, path)
    return core.settings().get("window", {})


def log_error(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
        reliability.rotate(core.DATA / "agentdesk.log")
        with (core.DATA / "agentdesk.log").open("a", encoding="utf-8") as f:
            print(msg, file=f)
    except OSError:
        pass


QUIT = threading.Event()  # set by the tray "Quit": main() then stops the server


def start_tray(win, show, quit_app):
    """Start a tray icon (Open / Quit) in its own thread; returns the icon, or None (reason logged) if it can't run."""
    try:
        import pystray
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(37, 99, 235, 255))
        d.polygon([(18, 46), (32, 16), (46, 46), (39, 46), (32, 31), (25, 46)], fill=(255, 255, 255, 255))
        menu = pystray.Menu(pystray.MenuItem("Open", lambda icon, item: show(), default=True),
                            pystray.MenuItem("Quit", lambda icon, item: quit_app()))
        icon = pystray.Icon("AgentDesk", img, "AgentDesk", menu)
        icon.run_detached()  # webview.start() must own the main thread
        return icon
    except Exception as e:  # not installed, or no tray on this platform / session
        log_error(f"AgentDesk: no tray icon, closing the window will end it ({type(e).__name__}: {e}). "
                  f"Install it with `pip install -r requirements-tray.txt`.")
        return None


_WINDOW = None  # the native window, once open_window created it (None in a browser)


def pick_path(kind: str, start: str = "") -> dict:
    """Native folder / file picker through the desktop window. {"available": False} in a browser (the page keeps
    its plain text field). {"ok": True, "path": ""} when the user cancels."""
    win = _WINDOW
    if win is None:
        return {"ok": False, "available": False, "message": "no native window: type the path"}
    try:
        import webview
        kinds = webview.FileDialog if hasattr(webview, "FileDialog") else webview
        folder = kinds.FOLDER if hasattr(kinds, "FOLDER") else webview.FOLDER_DIALOG
        opened = kinds.OPEN if hasattr(kinds, "OPEN") else webview.OPEN_DIALOG
        kw = {"directory": start} if start and Path(start).is_dir() else {}
        if kind == "file":
            kw["file_types"] = ("Export (*.zip)",)
        res = win.create_file_dialog(folder if kind == "folder" else opened, **kw)
    except Exception as e:
        return {"ok": False, "available": True, "message": f"could not open the dialog: {e}"}
    return {"ok": True, "available": True, "path": str(res[0]) if res else ""}


def open_window(url: str, tray: bool = False) -> bool:
    """Show the dashboard in a native window; blocks until it is closed. False if no window could be shown.
    tray=True: closing (X) hides the window to a tray icon instead (if the tray can start); tray Quit sets QUIT."""
    global _WINDOW
    try:
        import webview
        w = window_settings()
        geo: dict = {}
        pos = {k: int(w[k]) for k in ("x", "y") if isinstance(w.get(k), int)}
        win = webview.create_window("AgentDesk", url, width=int(w.get("width", WINDOW_DEFAULTS["width"])),
                                    height=int(w.get("height", WINDOW_DEFAULTS["height"])), **pos)
        _WINDOW = win

        def resized(width, height):
            geo.update(width=width, height=height)

        def moved(x, y):
            geo.update(x=x, y=y)

        def closed():
            try:
                for k in ("width", "height", "x", "y"):
                    try:  # on a window that is closing these can raise (not only AttributeError)
                        v = getattr(win, k, None)
                    except Exception:
                        v = None
                    if k not in geo and isinstance(v, (int, float)):
                        geo[k] = int(v)
                window_settings({k: v for k, v in geo.items() if v is not None})
            except Exception as e:  # best effort
                log_error(f"AgentDesk: could not save window position: {e}")

        win.events.resized += resized
        win.events.moved += moved
        win.events.closed += closed

        icon = None
        if tray:
            state = {"quitting": False}

            def show():
                win.show()
                win.restore()

            def quit_app():
                state["quitting"] = True
                QUIT.set()
                try:
                    if icon is not None:
                        icon.stop()
                finally:
                    win.destroy()

            def closing():
                if state["quitting"]:
                    return True
                threading.Thread(target=win.hide, daemon=True).start()  # not from inside the close event
                return False  # cancel the close

            icon = start_tray(win, show, quit_app)
            if icon is not None:
                win.events.closing += closing
        try:
            ico = WEB / "agentdesk.ico"
            webview.start(icon=str(ico)) if ico.exists() else webview.start()
        finally:
            if icon is not None:
                icon.stop()
        return True
    except Exception as e:  # ImportError, missing WebView2 / engine, no display ...
        log_error(f"AgentDesk: cannot open the native window ({type(e).__name__}: {e}); falling back to the browser. "
                  f"Use `python -m agentdesk dashboard --browser` to skip the window.")
        return False


def main(port: int = 8765, browser: bool = False, window: bool = True) -> None:
    """browser=True: open a browser tab. Otherwise window=True shows a native window (browser if that fails);
    window=False just serves."""
    core.settings()  # creates the data folder and default settings on first start
    prev, now = core.record_version()
    if prev is not None or not (core.DATA / "agentdesk.log").exists():
        log_error(f"{'Upgraded from ' + prev + ' to ' if prev else 'First start of '}{core.version_text()}")
    url = f"http://127.0.0.1:{port}"
    if answers(port):
        print(f"AgentDesk is already running at {url}")
        if browser or (window and not open_window(url)):
            webbrowser.open(url)
        return
    if sys.stdout is None or sys.stderr is None:  # started by pythonw: no console, log to a file
        log = (core.DATA / "agentdesk.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    server = quiet_errors(ThreadingHTTPServer(("127.0.0.1", port), Handler))
    pid_file().write_text(str(os.getpid()), encoding="utf-8")
    print(f"AgentDesk at {url}  (data: {core.DATA}; stop with `python -m agentdesk stop` — runs keep going)")
    thread = threading.Thread(target=serve_supervised, args=(server, QUIT), daemon=True)
    thread.start()
    QUIT.clear()
    # Only this process (the one that owns the port, checked above) runs the loop.
    threading.Thread(target=autorun_loop, daemon=True, name="autorunner").start()
    threading.Thread(target=usage_loop, daemon=True, name="usage-reader").start()
    threading.Thread(target=checkup_loop, daemon=True, name="checkups").start()
    threading.Thread(target=hygiene_loop, daemon=True, name="disk-hygiene").start()
    threading.Thread(target=_load_sdk, daemon=True, name="sdk-preload").start()  # off the startup path
    try:
        if browser:
            webbrowser.open(url)
        elif window and not open_window(url, tray=True):
            webbrowser.open(url)
        # Closing the window hides it to the tray (or ends just the window without a tray): the server and runs
        # go on until tray Quit or `stop`.
        while thread.is_alive() and not QUIT.is_set():
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        QUIT.set()
        if thread.is_alive():
            server.shutdown()
        pid_file().unlink(missing_ok=True)
