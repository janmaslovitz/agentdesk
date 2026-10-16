"""AgentDesk dashboard: projects, accounts, queues, runs, tokens, limits, logs and a chat per project.

Listens on 127.0.0.1 only. Runs are separate processes (`python -m agentdesk run …`), one per
project at a time, so closing the dashboard doesn't stop them (Stop does).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import tomllib
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from . import core

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
        out = self.p.path("runner.log").open("a", encoding="utf-8")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "AGENTDESK_HOME": str(core.DATA)}
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
- run_next, run_task(id), run_all, plan_task(id) (preview the router's plan), stop_run. The user
  approves each of these on the dashboard; only one run per project goes at a time.
Shell commands (Bash/PowerShell) also wait for the user's approval, so prefer Read/Grep/Glob and use
the shell only when it's really needed (e.g. `git log`), one plain command at a time. If the user
denies a call, don't retry it: say what you wanted to do.
When the user asks for work, write a clear task (ask one short question first only if the goal is
unclear), then say it's queued, and offer to start it. Never invent results: read the files.
"""

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
run_next, run_task(id), run_all, plan_task(id), stop_run. Only one run per project goes at a time.
Delegate bigger pieces of work to the workers as tasks; do small things yourself.

Shell commands (Bash/PowerShell) run without asking, but a guard refuses pushing, changing remotes,
publishing, pull requests, resetting or switching to main, and editing AgentDesk's config; edits
outside your worktree, tasks/ and notes.md are refused too. Don't try to work around a refusal:
say what you wanted to do and let the user do it. You have a budget per message
(${budget:.2f}); finish with a short summary of what you did and what's left. Never invent results.
"""

MCP = "agentdesk"
MCP_PREFIX = f"mcp__{MCP}__"
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
        with self.lock:
            entry = self.items.get(rid)
        if entry is None:
            return False
        fut = entry[1]
        answer = {"action": action, "prefix": prefix}
        self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(answer))
        return True

    def deny_all(self) -> None:
        for req in self.pending():
            self.resolve(req["id"], "deny")


def tool_summary(name: str, args: dict) -> str:
    if name in ("Bash", "PowerShell"):
        return str(args.get("command", ""))
    if args.get("file_path"):
        return str(args["file_path"])
    if args.get("pattern"):
        return str(args["pattern"]) + (f"  in {args['path']}" if args.get("path") else "")
    return json.dumps(args, ensure_ascii=False)[:300] if args else ""


def result_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


class Chat:
    def __init__(self, loop: asyncio.AbstractEventLoop, p: core.Project):
        self.loop, self.p = loop, p
        self.client: ClaudeSDKClient | None = None
        self.history: list[dict] = []
        self.lock = threading.Lock()
        self.approvals = Approvals(loop)
        self.busy = False
        self.model = ""
        self.mode = ""  # the mode the current session was started in
        self.session_id: str | None = None  # the CLI session; coworker messages resume it in a new process
        self.session_cost = 0.0  # the session's running total_cost_usd (the CLI reports totals, not per message)
        self.generation = 0  # bumped by reset, so an answer still coming in doesn't revive the old session

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
        if name.startswith(MCP_PREFIX):
            return PermissionResultAllow()
        wt = core.cowork_path(self.p)
        check = core.guard(wt, also=(self.p.dir / "tasks", self.p.dir / "notes.md"), never=(Path(self.p.cfg["repo"]["worktrees"]),),
                           extra_deny=core.COWORK_DENY, who="the Coworker")
        return await check(name, args, ctx)

    async def coworker_hook(self, data: dict, _tool_use_id=None, _ctx=None) -> dict:
        """PreToolUse hook running the same check for every tool call, including those the CLI allows on its
        own without asking can_use_tool (e.g. commands it thinks are read-only, like `echo`)."""
        r = await self.coworker_permission(str(data.get("tool_name", "")), dict(data.get("tool_input") or {}))
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
            e = runner(p).start(args or [])
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
            return ok("\n".join(lines))

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

        return [add_task, queue_status, run_next, run_task, run_all, plan_task, stop_run]

    # -- sessions: the Assistant keeps one CLI process; the Coworker starts one per message (resuming the
    # session), because --max-budget-usd counts the spend of the process, which makes it a per-message budget.

    def _assistant_options(self):
        self.model = core.chat_settings("assistant")["model"]
        prompt = CHAT_PROMPT.format(name=self.p.name, repo=self.p.repo, next_id=core.next_task_id(self.p))
        # Only read-only tools go in allowed_tools: anything listed there skips can_use_tool.
        return core.options(self.p, model=self.model, cwd=str(self.p.dir), system_prompt=prompt, max_turns=15,
                            allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.repo)],
                            mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.tools())},
                            disallowed_tools=["WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"], permission_mode="default",
                            can_use_tool=self.permission, setting_sources=[])

    def _coworker_options(self):
        cs = core.chat_settings("coworker")
        self.model = cs["model"]
        wt = core.cowork_worktree(self.p)
        prompt = COWORKER_PROMPT.format(name=self.p.name, wt=wt, repo=self.p.repo, folder=self.p.dir, budget=float(cs["budget_usd"]))
        gate_env = {k: str(v) for k, v in self.p.cfg.get("gates", {}).get("env", {}).items()}
        # Shell, edit and run tools must not go in allowed_tools (that skips can_use_tool, i.e. the guard).
        # No setting_sources: a project's .claude/settings.json could allow commands past the guard.
        return core.options(self.p, model=self.model, effort=cs.get("effort") or None, cwd=str(wt), system_prompt=prompt,
                            max_turns=int(cs["max_turns"]), max_budget_usd=float(cs["budget_usd"]), resume=self.session_id,
                            allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.dir)],
                            mcp_servers={MCP: create_sdk_mcp_server(MCP, tools=self.tools(approval=False))},
                            disallowed_tools=["WebFetch", "WebSearch", "NotebookEdit", "Task", "Agent"], permission_mode="default",
                            can_use_tool=self.coworker_permission, setting_sources=[], env=gate_env,
                            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self.coworker_hook])]})

    async def _client(self, mode: str) -> ClaudeSDKClient:
        if self.client is not None and self.mode != mode:
            await self._close()
        if self.client is None:
            self.mode = mode
            # The coworker's worktree may need a `git worktree add` first: off the event loop.
            opts = await asyncio.to_thread(self._coworker_options) if mode == "coworker" else self._assistant_options()
            self.client = ClaudeSDKClient(options=opts)
            await self.client.connect()
        return self.client

    async def _close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # the process may be gone already
                pass

    def _answer(self, text: str, tools=(), cost: float = 0.0, tokens: dict | None = None) -> dict:
        return {"text": text, "tools": list(tools), "cost": cost, "tokens": tokens or {}, "model": self.model}

    async def _ask(self, text: str) -> dict:
        mode = core.chat_mode(self.p)
        coworker = mode == "coworker"
        acct = self.p.account.get("name", "default")
        gen = self.generation
        if coworker:
            if why := core.usage_paused(acct):
                self.model = core.chat_settings("coworker")["model"]
                return self._answer(f"Paused: {why}. The Coworker doesn't start new work until the account's usage "
                                    "window resets (`[limits] pause_at_utilization` in settings.toml).")
            await self._close()  # a fresh process per message, so the budget counts this message only
        client = await self._client(mode)
        reply, calls, result, paused = [], {}, None, None
        try:
            await client.query(text)
            async for msg in client.receive_response():
                if isinstance(msg, RateLimitEvent):
                    core.record_rate_limit(acct, msg.rate_limit_info)
                    if coworker and not paused and (paused := core.over_pause(msg.rate_limit_info.utilization)):
                        await client.interrupt()
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
        self.p.path("chat_usage.json").write_text(json.dumps(u), encoding="utf-8")
        parts = ["\n\n".join(reply).strip()]
        if paused:
            parts.append(f"Paused: {paused}. The Coworker stopped here instead of continuing.")
        if coworker and (note := budget_note(result, cost)):
            parts.append(note)
        return self._answer("\n\n".join(x for x in parts if x) or "(no answer)", calls.values(), cost, tokens)

    def usage(self) -> dict:
        f = self.p.path("chat_usage.json")
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"spent_usd": 0.0, "tokens": {}}

    def ask(self, text: str) -> dict:
        with self.lock:
            self.history.append({"role": "user", "text": text})
            self.busy = True
            fut = asyncio.run_coroutine_threadsafe(self._ask(text), self.loop)
            try:
                # Approvals can wait up to 10 minutes each, so the answer may take a while.
                out = fut.result(timeout=3 * APPROVAL_TIMEOUT_S)
            except Exception as e:  # shown in the chat; the dashboard keeps going
                fut.cancel()
                out = {"text": f"Error: {e or type(e).__name__}", "tools": [], "cost": 0.0}
                self.client = None
            finally:
                self.busy = False
            self.history.append({"role": "assistant", **out})
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

    def info(self) -> dict:
        mode = core.chat_mode(self.p)
        cs = core.chat_settings(mode)
        out = {"history": self.history, "mode": mode, "busy": self.busy, "model": cs["model"]}
        if mode == "coworker":
            out.update(budget_usd=float(cs["budget_usd"]), worktree=str(core.cowork_path(self.p)), branch=f"auto/{core.COWORK}")
        return out


def budget_note(result, cost: float) -> str | None:
    """The chat's message when a coworker message hit `[chat.coworker] budget_usd`."""
    if result is None or result.subtype != "error_max_budget_usd":
        return None
    budget = float(core.chat_settings("coworker")["budget_usd"])
    return (f"Stopped: this message spent {usd(cost)} and reached the Coworker budget of {usd(budget)} per message "
            "(`[chat.coworker] budget_usd` in settings.toml). Send another message to continue.")


def usd(x: float) -> str:
    return f"${x:.2f}" if x >= 0.1 or x == 0 else f"${x:.4f}"


LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
CHATS: dict[str, Chat] = {}


def chat(p: core.Project) -> Chat:
    with LOCK:
        if p.slug not in CHATS:
            CHATS[p.slug] = Chat(LOOP, p)
        return CHATS[p.slug]


# ----------------------------------------------------------------------------- accounts

_AUTH_CACHE: dict[str, tuple[float, dict]] = {}


def auth_status(a: dict, fresh: bool = False) -> dict:
    """Whether the Claude CLI is really signed in for this account: `claude auth status` run against the
    account's own login folder. {"connected": bool | None, "email": str, "method": str}; connected is None
    when the CLI couldn't be asked. Cached for a few seconds because the dashboard polls."""
    name = a.get("name", "default")
    hit = _AUTH_CACHE.get(name)
    if hit and not fresh and time.time() - hit[0] < 15:
        return hit[1]
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


def project_state(p: core.Project) -> dict:
    state = core.Store(p).load()
    tasks = []
    for t in core.load_tasks(p):
        e = state["tasks"].get(t.id, {})
        runs = e.get("runs", [])
        tok: dict[str, int] = {}
        for r in runs:
            for k, v in (r.get("tokens") or {}).items():
                tok[k] = tok.get(k, 0) + v
        tasks.append({"id": t.id, "title": t.title, "depends": t.depends, "tier": e.get("tier") or t.tier, "status": e.get("status", "pending"),
                      "used_pct": task_used(runs), "why": e.get("why", ""), "runs": runs, "cost": round(sum((r.get("cost_usd") or 0) for r in runs), 4), "tokens": tok,
                      "commit": e.get("commit"), "branch": e.get("branch"), "failure": e.get("last_failure", "")})
    s = core.settings()
    mode = core.chat_mode(p)
    cs = core.chat_settings(mode)
    return {"project": {"slug": p.slug, "name": p.name, "repo": str(p.repo), "base": p.cfg["repo"]["base"], "account": p.account.get("name"),
                        "gates": p.cfg.get("gates", {}).get("quick", []), "chat_mode": mode,
                        "chat_model": cs["model"] + (f" ({cs['effort']})" if cs.get("effort") else ""),
                        "chat_budget": cs.get("budget_usd"), "cowork": str(core.cowork_path(p))},
            "tasks": tasks, "spent_usd": state.get("spent_usd", 0.0), "tokens": state.get("tokens", {}), "chat": chat(p).usage(),
            "runner": runner(p).info(), "log": tail(p.path("agentdesk.log"), 150), "errors": tail(p.path("runner.log"), 25),
            "limits": {"pause_at": s["limits"]["pause_at_utilization"], "run_budget": s["limits"]["run_budget_usd"]},
            "usage": core.account_usage(p.account.get("name", "default")).get("rate_limits", {}),
            "reading": reading_view(p.account.get("name", "default")), "max_age_min": core.usage_max_age_min(),
            "auto_error": auto_error(p.account.get("name", "default")) if p.account.get("kind") != "api_key" else None,
            "tier_avg": tier_averages(tasks),
            "tiers": {k: {"model": v["model"], "effort": v.get("effort")} for k, v in s["tiers"].items()}}


def overview() -> dict:
    rows = []
    for p in core.projects():
        state = core.Store(p).load()
        counts: dict[str, int] = {}
        for t in core.load_tasks(p):
            st = state["tasks"].get(t.id, {}).get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        rows.append({"slug": p.slug, "name": p.name, "repo": str(p.repo), "account": p.account.get("name"), "counts": counts,
                     "spent_usd": state.get("spent_usd", 0.0), "running": runner(p).running(),
                     "auto": autorun_status(p), "auto_enabled": core.autorunner_cfg(p)["enabled"]})
    return {"auto_global": core.autorunner_cfg()["enabled"], "attention": attention_all(), "projects": rows, "accounts": account_rows(), "presets": list(core.PRESETS), "data": str(core.DATA)}


# ----------------------------------------------------------------------------- auto-runner

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
    if core.usage_paused(p.account.get("name", "default")):
        return "paused: usage limit"
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
    if core.usage_paused(p.account.get("name", "default")):
        return did
    if chat(p).approvals.pending():
        return did
    t = core.next_startable(tasks, state, running, cfg["retry_crashed"])
    if t is None:
        return did
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


def attention_all() -> dict:
    items = []
    for p in core.projects():
        for it in project_attention(p):
            items.append({**it, "name": p.name})
    return {"count": len(items), "items": items}


# ----------------------------------------------------------------------------- http

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
        try:
            if not d and "zip" in (self.headers.get("Content-Type") or ""):
                import tempfile
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > core.MAX_TOTAL_BYTES:
                    return {"ok": False, "message": "the file is empty or too large"}
                fd, tmp = tempfile.mkstemp(suffix=".zip")
                with os.fdopen(fd, "wb") as f:
                    f.write(self.rfile.read(n))
                upload = Path(tmp)
                d = query
            repo = str(d.get("repo") or "").strip()
            if not repo:
                return {"ok": False, "message": "choose the repository folder on this computer"}
            if d.get("repo_url"):  # clone the code repo into `repo` first (must be a new/empty folder)
                if core.repo_state(repo) != "plain" or (Path(repo).exists() and any(Path(repo).iterdir())):
                    return {"ok": False, "message": f"{repo} already exists and is not empty; choose a new folder to clone into"}
                core.clone_for_import(str(d["repo_url"]), Path(repo))
            src = upload or (core.clone_for_import(str(d["source_url"])) if d.get("source_url") else Path(str(d.get("source") or "")))
            if not str(src):
                return {"ok": False, "message": "choose an export file, folder or repository"}
            p = core.import_project(src, repo, str(d.get("new_name") or ""), str(d.get("account") or "") or None,
                                    str(d.get("project") or "") or None, d.get("pull") in (True, "true", "1"))
            return {"ok": True, "slug": p.slug}
        except core.NameClash as e:
            return {"ok": False, "name_clash": True, "suggested": e.suggested, "message": str(e)}
        except ValueError as e:  # BadExport is a ValueError
            return {"ok": False, "message": str(e)}
        finally:
            if upload:
                upload.unlink(missing_ok=True)

    def do_GET(self):
        parts = [x for x in self.path.split("?")[0].split("/") if x]
        if not parts or parts == ["index.html"]:
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parts == ["api", "attention"]:
            return self.json(attention_all())
        if parts == ["api", "overview"]:
            return self.json(overview())
        if len(parts) >= 3 and parts[:2] == ["api", "p"]:
            p = self.proj(parts[2])
            if p is None:
                return None
            if len(parts) == 4 and parts[3] == "state":
                return self.json(project_state(p))
            if len(parts) == 4 and parts[3] == "git":
                return self.json(core.git_status(p))
            if len(parts) == 4 and parts[3] == "build":
                return self.json(core.build_job(p).info())
            if len(parts) == 4 and parts[3] == "chat":
                return self.json(chat(p).info())
            if len(parts) == 4 and parts[3] == "pending":
                return self.json({"pending": chat(p).approvals.pending()})
            if len(parts) == 5 and parts[3] == "task":
                t = next((t for t in core.load_tasks(p) if t.id == parts[4]), None)
                if t is None:
                    return self.json({"error": "no such task"}, 404)
                rep = p.path("reports") / f"{t.id}.md"
                e = core.Store(p).load()["tasks"].get(t.id, {})
                return self.json({"body": t.body, "brief": e.get("brief", ""), "report": rep.read_text(encoding="utf-8") if rep.exists() else ""})
        self.send(404, b"not found", "text/plain")

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and not re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", origin):
            return self.json({"error": "forbidden"}, 403)
        parts = [x for x in self.path.split("?")[0].split("/") if x]
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        upload = parts == ["api", "projects", "import"] and "zip" in (self.headers.get("Content-Type") or "")
        d = {} if upload else self.body()
        try:
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
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "export":
                p = self.proj(parts[2])
                if p is None:
                    return None
                if runner(p).running():
                    return self.json({"ok": False, "message": "a run is in progress for this project; stop it first"})
                logs = d.get("include_logs") is True
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
                    err = runner(p).start(args)
                    return self.json({"error": err} if err else {"ok": True})
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
                        blocked = "a task run is in progress for this project; stop it first" if runner(p).running() else None
                        err = job.start(str(d.get("kind") or "build"), str(d.get("ref") or ""), blocked)
                    return self.json({"ok": False, "message": err} if err else {"ok": True})
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
                    return self.json(chat(p).ask(text) if text else {"error": "empty"})
                if act == "chat" and parts[4:] == ["reset"]:
                    chat(p).reset()
                    return self.json({"ok": True})
                if act == "chat" and parts[4:] == ["mode"]:
                    mode = str(d.get("mode", ""))
                    core.set_chat_mode(p, mode)
                    chat(p).reset(keep_history=True)
                    return self.json({"ok": True, "mode": mode})
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


def answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
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
    tmp.replace(path)
    return core.settings().get("window", {})


def log_error(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
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


def open_window(url: str, tray: bool = False) -> bool:
    """Show the dashboard in a native window; blocks until it is closed. False if no window could be shown.
    tray=True: closing (X) hides the window to a tray icon instead (if the tray can start); tray Quit sets QUIT."""
    try:
        import webview
        w = window_settings()
        geo: dict = {}
        pos = {k: int(w[k]) for k in ("x", "y") if isinstance(w.get(k), int)}
        win = webview.create_window("AgentDesk", url, width=int(w.get("width", WINDOW_DEFAULTS["width"])),
                                    height=int(w.get("height", WINDOW_DEFAULTS["height"])), **pos)

        def resized(width, height):
            geo.update(width=width, height=height)

        def moved(x, y):
            geo.update(x=x, y=y)

        def closed():
            try:
                for k in ("width", "height", "x", "y"):
                    if k not in geo and isinstance(getattr(win, k, None), (int, float)):
                        geo[k] = int(getattr(win, k))
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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    pid_file().write_text(str(os.getpid()), encoding="utf-8")
    print(f"AgentDesk at {url}  (data: {core.DATA}; stop with `python -m agentdesk stop` — runs keep going)")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    QUIT.clear()
    # Only this process (the one that owns the port, checked above) runs the loop.
    threading.Thread(target=autorun_loop, daemon=True, name="autorunner").start()
    threading.Thread(target=usage_loop, daemon=True, name="usage-reader").start()
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
