"""AgentDesk dashboard: projects, accounts, queues, runs, tokens, limits, logs and a chat per project.

Listens on 127.0.0.1 only. Runs are separate processes (`python -m agentdesk run …`), one per
project at a time, so closing the dashboard doesn't stop them (Stop does).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from . import core

WEB = Path(__file__).resolve().parent / "web"
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
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen([sys.executable, "-m", "agentdesk", *args], cwd=core.APP, stdout=out, stderr=out, env=env, creationflags=flags)
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

You may read these, and you may read the repository ({repo}) to write better tasks. You may create
or edit task files in tasks/ and append to notes.md. You can't run anything: the user starts and
stops runs with the dashboard's buttons. When the user asks for work, write a clear task file (ask
one short question first only if the goal is unclear), then say it's queued. Never invent results:
read the files.
"""


class Chat:
    def __init__(self, loop: asyncio.AbstractEventLoop, p: core.Project):
        self.loop, self.p = loop, p
        self.client: ClaudeSDKClient | None = None
        self.history: list[dict] = []
        self.lock = threading.Lock()

    async def _client(self) -> ClaudeSDKClient:
        if self.client is None:
            root = str(self.p.dir.resolve()).lower().replace("\\", "/")

            async def can_use_tool(name, args, _ctx):
                if name in ("Write", "Edit"):
                    path = str(args.get("file_path", "")).lower().replace("\\", "/")
                    if not ((path.startswith(root + "/tasks/") and path.endswith(".md")) or path == root + "/notes.md"):
                        return PermissionResultDeny(message="you may only write task files in tasks/ or notes.md")
                if name in ("Bash", "PowerShell"):
                    return PermissionResultDeny(message="running commands isn't allowed here")
                return PermissionResultAllow()

            prompt = CHAT_PROMPT.format(name=self.p.name, repo=self.p.repo, next_id=core.next_task_id(self.p))
            opts = core.options(self.p, model=core.settings().get("chat", {}).get("model", "haiku"), cwd=str(self.p.dir), system_prompt=prompt,
                                max_turns=15, allowed_tools=["Read", "Glob", "Grep"], add_dirs=[str(self.p.repo)],
                                disallowed_tools=["Bash", "PowerShell", "WebFetch", "WebSearch"], permission_mode="default",
                                can_use_tool=can_use_tool, setting_sources=[])
            self.client = ClaudeSDKClient(options=opts)
            await self.client.connect()
        return self.client

    async def _ask(self, text: str) -> dict:
        client = await self._client()
        await client.query(text)
        reply, tools, cost, tokens = [], [], 0.0, {}
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        reply.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        target = b.input.get("file_path") or b.input.get("pattern") or ""
                        tools.append(f"{b.name} {Path(str(target)).name}".strip())
            if isinstance(msg, ResultMessage):
                cost, tokens = msg.total_cost_usd or 0.0, core.usage_tokens(msg.usage)
        u = self.usage()
        u["spent_usd"] = round(u.get("spent_usd", 0.0) + cost, 4)
        for k, v in tokens.items():
            u.setdefault("tokens", {})[k] = u.get("tokens", {}).get(k, 0) + v
        self.p.path("chat_usage.json").write_text(json.dumps(u), encoding="utf-8")
        return {"text": "\n\n".join(reply).strip() or "(no answer)", "tools": tools, "cost": cost}

    def usage(self) -> dict:
        f = self.p.path("chat_usage.json")
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"spent_usd": 0.0, "tokens": {}}

    def ask(self, text: str) -> dict:
        with self.lock:
            self.history.append({"role": "user", "text": text})
            try:
                out = asyncio.run_coroutine_threadsafe(self._ask(text), self.loop).result(timeout=300)
            except Exception as e:  # shown in the chat; the dashboard keeps going
                out = {"text": f"Error: {e}", "tools": [], "cost": 0.0}
                self.client = None
            self.history.append({"role": "assistant", **out})
            return out

    def reset(self) -> None:
        with self.lock:
            if self.client is not None:
                asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
                self.client = None
            self.history = []


LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
CHATS: dict[str, Chat] = {}


def chat(p: core.Project) -> Chat:
    with LOCK:
        if p.slug not in CHATS:
            CHATS[p.slug] = Chat(LOOP, p)
        return CHATS[p.slug]


# ----------------------------------------------------------------------------- accounts

def account_rows() -> list[dict]:
    rows = []
    for a in core.settings().get("accounts", []):
        cfg_dir = Path(a["config_dir"]).expanduser() if a.get("config_dir") else Path.home() / ".claude"
        if a.get("kind") == "api_key":
            ready = bool(os.environ.get(a.get("key_env", "")))
            where = f"key from ${a.get('key_env', '')}" + ("" if ready else " (not set)")
        else:
            ready = (cfg_dir / ".credentials.json").exists() or sys.platform == "darwin"
            where = str(cfg_dir)
        rows.append({"name": a.get("name"), "kind": a.get("kind", "subscription"), "where": where, "ready": ready,
                     "usage": core.account_usage(a.get("name", "default")).get("rate_limits", {})})
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
                      "why": e.get("why", ""), "runs": runs, "cost": round(sum((r.get("cost_usd") or 0) for r in runs), 4), "tokens": tok,
                      "commit": e.get("commit"), "branch": e.get("branch"), "failure": e.get("last_failure", "")})
    s = core.settings()
    return {"project": {"slug": p.slug, "name": p.name, "repo": str(p.repo), "base": p.cfg["repo"]["base"], "account": p.account.get("name"),
                        "gates": p.cfg.get("gates", {}).get("quick", [])},
            "tasks": tasks, "spent_usd": state.get("spent_usd", 0.0), "tokens": state.get("tokens", {}), "chat": chat(p).usage(),
            "runner": runner(p).info(), "log": tail(p.path("agentdesk.log"), 150), "errors": tail(p.path("runner.log"), 25),
            "limits": {"pause_at": s["limits"]["pause_at_utilization"], "run_budget": s["limits"]["run_budget_usd"]},
            "usage": core.account_usage(p.account.get("name", "default")).get("rate_limits", {}),
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
                     "spent_usd": state.get("spent_usd", 0.0), "running": runner(p).running()})
    return {"projects": rows, "accounts": account_rows(), "presets": list(core.PRESETS), "data": str(core.DATA)}


# ----------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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

    def do_GET(self):
        parts = [x for x in self.path.split("?")[0].split("/") if x]
        if not parts or parts == ["index.html"]:
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parts == ["api", "overview"]:
            return self.json(overview())
        if len(parts) >= 3 and parts[:2] == ["api", "p"]:
            p = self.proj(parts[2])
            if p is None:
                return None
            if len(parts) == 4 and parts[3] == "state":
                return self.json(project_state(p))
            if len(parts) == 4 and parts[3] == "chat":
                return self.json({"history": chat(p).history})
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
        d = self.body()
        try:
            if parts == ["api", "projects"]:
                try:
                    p = core.add_project(str(d.get("name", "")).strip(), str(d.get("repo", "")).strip(), str(d.get("base", "")).strip() or "main",
                                         str(d.get("preset", "none")), str(d.get("account", "default")),
                                         local_git=d.get("local_git") is True)
                except core.NeedsLocalGit as e:
                    return self.json({"needs_local_git": True, "reason": e.reason, "message": str(e)})
                return self.json({"ok": True, "slug": p.slug})
            if parts == ["api", "accounts"]:
                err = add_account(str(d.get("name", "")).strip(), str(d.get("kind", "subscription")), str(d.get("value", "")).strip())
                return self.json({"error": err} if err else {"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "accounts"] and parts[3] == "login":
                err = open_login(parts[2])
                return self.json({"error": err} if err else {"ok": True})
            if len(parts) >= 4 and parts[:2] == ["api", "p"]:
                p = self.proj(parts[2])
                if p is None:
                    return None
                act = parts[3]
                if act == "run":
                    mode, tid = d.get("mode"), str(d.get("id") or "")
                    if tid and not ID.fullmatch(tid):
                        return self.json({"error": "bad task id"}, 400)
                    args = {"next": ["run", p.slug, "--max", "1"], "all": ["run", p.slug], "task": ["run", p.slug, tid],
                            "plan": ["plan", p.slug] + ([tid] if tid else []), "final": ["final", p.slug, tid]}.get(mode)
                    if args is None:
                        return self.json({"error": "unknown mode"}, 400)
                    err = runner(p).start(args)
                    return self.json({"error": err} if err else {"ok": True})
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
        except (ValueError, KeyError, OSError) as e:
            return self.json({"error": str(e)}, 400)
        self.send(404, b"not found", "text/plain")


def main(port: int = 8765, browser: bool = True) -> None:
    core.settings()  # creates the data folder and default settings on first start
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"AgentDesk at {url}  (data: {core.DATA}; Ctrl+C to quit — runs keep going)")
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
