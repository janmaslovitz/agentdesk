"""AgentDesk as an MCP server on stdin/stdout: `python -m agentdesk mcp` (or AgentDesk.cmd mcp / ./agentdesk.sh mcp).

Register it with an MCP client, e.g. Claude Code:

    claude mcp add agentdesk -- /path/to/AgentDesk/.venv/bin/python -m agentdesk mcp

(set AGENTDESK_HOME in the client's env if the data folder isn't the default one). Every tool is an operation of
service.py: list_projects, status, graph, task, log, add_task, plan, propose_plan, approve_plan, cancel_plan,
close_plan, architect, run, stop, retry, integrate. Results are JSON (text content plus structuredContent).

The protocol is JSON-RPC 2.0, one message per line (MCP stdio transport); only the tools capability is offered.
Nothing but protocol messages may go to stdout, so the core's prints are sent to stderr while a tool runs.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import traceback

from . import core, service

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "agentdesk", "title": "AgentDesk"}
INSTRUCTIONS = ("AgentDesk runs coding tasks with Claude worker agents in parallel, in dependency order, each in its own "
                "git worktree and branch (auto/<id>); nothing is pushed. Typical flow: list_projects → architect(goal) → "
                "poll plan until the draft is there → review it → approve_plan(start=true) → poll status/graph → "
                "integrate → review the auto/integration branch. add_task/propose_plan let you design the graph yourself.")

_out_lock = threading.Lock()


def _version() -> str:
    try:
        return core.version_info().get("version") or "0"
    except Exception:
        return "0"


def tools() -> list[dict]:
    out = []
    for op in service.OPERATIONS:
        out.append({"name": op.name, "description": op.description, "inputSchema": op.schema,
                    "annotations": {"readOnlyHint": not op.write, "destructiveHint": op.name in ("stop", "cancel_plan"),
                                    "openWorldHint": False}})
    return out


def call_tool(name: str, args: dict) -> dict:
    with contextlib.redirect_stdout(sys.stderr):  # core logs print; stdout is the protocol channel
        try:
            res = service.call(name, args)
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=1)}],
                    "structuredContent": res, "isError": False}
        except service.ApiError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # report, never crash the server
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message → its response (None for notifications)."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if mid is None:  # notification (initialized, cancelled, ...): nothing to answer
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {**SERVER_INFO, "version": _version()}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tools()})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return err(-32602, "tools/call needs a tool name")
        if name not in service.BY_NAME:
            return err(-32602, f"unknown tool {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        return ok(call_tool(name, args))
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"method not found: {method}")


def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    with _out_lock:
        sys.__stdout__.write(line + "\n")
        sys.__stdout__.flush()


def serve(stdin=None) -> None:
    stdin = stdin or sys.stdin
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = []
        for m in batch:
            if not isinstance(m, dict):
                replies.append({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
                continue
            try:
                r = handle(m)
            except Exception as e:
                r = {"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
            if r is not None:
                replies.append(r)
        if isinstance(msg, list):
            if replies:
                _send(replies)  # type: ignore[arg-type]
        elif replies:
            _send(replies[0])


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.__stdout__, "reconfigure"):
        sys.__stdout__.reconfigure(encoding="utf-8")
    serve()
