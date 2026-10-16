"""AgentDesk core: projects, accounts, and the route → work → check → escalate pipeline.

Data lives in the data folder (`AGENTDESK_HOME`, else `data/` next to the app, so the whole app
folder can be copied to another computer):

    data/settings.toml                    accounts, default tiers, limits
    data/accounts/<name>.json             last usage-limit readings for that account
    data/projects/<slug>/project.toml     repo, base branch, gates, account
    data/projects/<slug>/tasks/*.md       the queue (TOML header between +++ lines)
    data/projects/<slug>/state.json, notes.md, reports/, agentdesk.log
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

APP = Path(__file__).resolve().parent.parent


def _hide_sdk_console() -> None:
    """Windows: start the SDK's claude.exe with CREATE_NO_WINDOW.

    Without a console (pythonw), every console child gets its own visible window.
    The SDK calls anyio.open_process without creationflags, so only the SDK
    module's `anyio` name is swapped for a proxy that adds the flag.
    """
    if os.name != "nt":
        return
    try:
        import anyio
        from claude_agent_sdk._internal.transport import subprocess_cli as sc
    except Exception:
        return
    if getattr(sc.anyio, "_agentdesk_nowindow", False):
        return

    class _Proxy:
        _agentdesk_nowindow = True

        def __getattr__(self, name):
            return getattr(anyio, name)

        @staticmethod
        async def open_process(*args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            return await anyio.open_process(*args, **kw)

    sc.anyio = _Proxy()


_hide_sdk_console()
DATA = Path(os.environ.get("AGENTDESK_HOME") or APP / "data")
TIER_ORDER = ["mechanical", "standard", "hard"]

DEFAULT_SETTINGS = """# AgentDesk settings. Edit here or in the dashboard.

# Accounts the projects can use. kind = "subscription": a Claude login kept in its own folder
# (empty config_dir = your normal Claude Code login). kind = "api_key": the key is read from the
# environment variable named in key_env (never stored here).
[[accounts]]
name = "default"
kind = "subscription"
config_dir = ""

[limits]
pause_at_utilization = 0.85
run_budget_usd = 40.0
retries_per_tier = 2

[router]
model = "haiku"
max_turns = 12
budget_usd = 0.30

[tiers.mechanical]
model = "haiku"
max_turns = 40
budget_usd = 1.0
next = "standard"

[tiers.standard]
model = "sonnet"
effort = "medium"
max_turns = 80
budget_usd = 4.0
next = "hard"

[tiers.hard]
model = "opus"
effort = "high"
max_turns = 120
budget_usd = 12.0

[chat.assistant]
model = "haiku"

# Coworker: autonomous chat (no approval cards), guarded like the workers; budget_usd is per message.
[chat.coworker]
model = "sonnet"
effort = "medium"
budget_usd = 2.0

# Auto-runner: the dashboard starts the next ready task of every project by itself (no model calls),
# recovers runs that died, retries a crashed task retry_crashed times, and flags the rest.
# A project can override `enabled` in its project.toml under [autorunner].
[autorunner]
enabled = true
interval_s = 30
retry_crashed = 1

[guard]
deny = [
  'git\\s+push', 'git\\s+remote', 'gh\\s+(pr|release|repo|api|issue|workflow)', 'git\\s+reset\\s+--hard',
  'git\\s+(checkout|switch)\\s+(main|master|release)', 'git\\s+branch\\s+-D', 'git\\s+worktree\\s+remove',
  'rm\\s+-rf\\s+/', 'curl\\s+.*(-T\\b|--upload|-d\\b|--data|-X\\s*POST)', 'Invoke-WebRequest.*-Method\\s+Post',
  'cargo\\s+publish', 'npm\\s+publish', 'twine\\s+upload', 'setup\\.exe', 'msiexec',
]
"""

# Gate presets for new projects. `{crates}` (Rust) becomes `-p a -p b` for changed crates.
PRESETS = {
    "rust": {"quick": ["cargo fmt --all --check", "cargo clippy {crates} --all-targets -- -D warnings", "cargo test {crates}"],
             "final": ["cargo clippy --workspace --all-targets -- -D warnings", "cargo test --workspace"]},
    "node": {"quick": ["npm run -s lint --if-present", "npm test --silent"], "final": ["npm run -s build --if-present", "npm test --silent"]},
    "python": {"quick": ["python -m pytest -q -x"], "final": ["python -m pytest -q"]},
    "go": {"quick": ["go vet ./...", "go test ./..."], "final": ["go test ./..."]},
    "none": {"quick": [], "final": []},
}


# ----------------------------------------------------------------------------- settings, accounts, projects

def settings() -> dict:
    p = DATA / "settings.toml"
    if not p.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_SETTINGS, encoding="utf-8")
    with p.open("rb") as f:
        return tomllib.load(f)


def account(name: str | None) -> dict:
    accts = settings().get("accounts", [])
    return next((a for a in accts if a.get("name") == name), accts[0] if accts else {"name": "default", "kind": "subscription"})


def account_env(acct: dict) -> dict[str, str]:
    """Environment for the Claude CLI so it uses this account."""
    env: dict[str, str] = {}
    if acct.get("kind") == "api_key":
        key = os.environ.get(acct.get("key_env", ""), "")
        if key:
            env["ANTHROPIC_API_KEY"] = key
    elif acct.get("config_dir"):
        env["CLAUDE_CONFIG_DIR"] = str(Path(acct["config_dir"]).expanduser())
    return env


def bundled_cli() -> str:
    import claude_agent_sdk
    p = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    return str(p) if p.exists() else (shutil.which("claude") or "claude")


def account_usage(name: str) -> dict:
    p = DATA / "accounts" / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_account_usage(name: str, usage: dict) -> None:
    p = DATA / "accounts" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(usage, indent=2), encoding="utf-8")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


@dataclass
class Project:
    slug: str
    dir: Path
    cfg: dict

    @property
    def name(self) -> str:
        return self.cfg.get("name", self.slug)

    @property
    def repo(self) -> Path:
        return Path(self.cfg["repo"]["path"])

    @property
    def tasks_dir(self) -> Path:
        return self.dir / "tasks"

    @property
    def account(self) -> dict:
        return account(self.cfg.get("account"))

    def path(self, name: str) -> Path:
        return self.dir / name


def projects() -> list[Project]:
    out = []
    for d in sorted((DATA / "projects").glob("*")):
        p = d / "project.toml"
        if p.exists():
            with p.open("rb") as f:
                out.append(Project(d.name, d, tomllib.load(f)))
    return out


def project(slug_: str) -> Project:
    for p in projects():
        if p.slug == slug_:
            return p
    raise KeyError(f"no project {slug_!r}")


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


class NeedsLocalGit(ValueError):
    """The folder needs local version tracking set up first; `reason` is "not_a_repo" or "no_commits"."""

    def __init__(self, reason: str, repo: str):
        self.reason = reason
        super().__init__(f"{repo} is not a git repository" if reason == "not_a_repo"
                         else f"{repo} is a git repository with no commits yet")


GITIGNORE = "node_modules\ntarget\ndist\nbuild\n.venv\n__pycache__\n*.log\n.DS_Store\n"


def repo_state(repo: str) -> str:
    """"plain" (not a git repo), "empty" (repo without commits) or "ready"."""
    if not (Path(repo) / ".git").exists():
        return "plain"
    code, _ = git(["rev-parse", "--verify", "-q", "HEAD"], Path(repo))
    return "ready" if code == 0 else "empty"


def setup_local_git(repo: str) -> str:
    """Local-only version tracking: init if needed, .gitignore if absent, one first commit. Returns the base branch.
    Never adds remotes, never pushes, never touches global config."""
    path = Path(repo)
    if repo_state(repo) == "plain":
        code, out = git(["init"], path)
        if code != 0:
            raise RuntimeError(f"git init failed: {out}")
    if not (path / ".gitignore").exists():
        (path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    for key, val in (("user.name", "AgentDesk"), ("user.email", "agentdesk@localhost")):
        if not git(["config", key], path)[1].strip():
            git(["config", "--local", key, val], path)
    git(["add", "-A"], path)
    code, out = git(["commit", "--allow-empty", "-m", "AgentDesk: starting point"], path)
    if code != 0:
        raise RuntimeError(f"git commit failed: {out}")
    return git(["symbolic-ref", "--short", "HEAD"], path)[1].strip()


# ----------------------------------------------------------------------------- chat settings (project.toml / settings.toml)

CHAT_MODES = ("assistant", "coworker")
CHAT_DEFAULT_MODELS = {"assistant": "haiku", "coworker": "sonnet"}
# Defaults for `[chat.<mode>]` keys missing from settings.toml (the model has its own fallback chain).
CHAT_DEFAULTS = {"assistant": {}, "coworker": {"effort": "medium", "budget_usd": 2.0, "max_turns": 40}}
COWORK = "cowork"  # the Coworker's worktree folder and branch (auto/cowork)
# On top of `[guard] deny`: the Coworker may not touch AgentDesk's own config from the shell (the
# orchestrator runs project.toml's gate commands unguarded, and settings.toml holds the guard itself).
COWORK_DENY = (r"project\.toml", r"settings\.toml")
# Auto-approving a command by prefix is only safe for a single plain command: anything that could
# chain, pipe, redirect or evaluate a second command (also PowerShell's (...), $(...), {...}) asks.
SHELL_META = re.compile(r"[;&|`<>(){}$\n\r]")


def chat_settings(mode: str) -> dict:
    """`[chat.<mode>]` from settings.toml over CHAT_DEFAULTS; the assistant's model falls back to the older
    `[chat] model`, then haiku (coworker: sonnet)."""
    chat = settings().get("chat", {})
    out = {**CHAT_DEFAULTS.get(mode, {}), **chat.get(mode, {})}
    legacy = chat.get("model") if mode == "assistant" else None  # older settings.toml: [chat] model
    out["model"] = out.get("model") or legacy or CHAT_DEFAULT_MODELS.get(mode, "haiku")
    return out


def _project_text(p: Project) -> str:
    return p.path("project.toml").read_text(encoding="utf-8")


def _project_cfg(p: Project) -> dict:
    return tomllib.loads(_project_text(p))


def _write_project(p: Project, text: str) -> None:
    p.cfg = tomllib.loads(text)  # refuses to write a file it can't read back
    tmp = p.path("project.toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p.path("project.toml"))


def chat_mode(p: Project) -> str:
    m = _project_cfg(p).get("chat_mode", "assistant")
    return m if m in CHAT_MODES else "assistant"


def set_chat_mode(p: Project, mode: str) -> None:
    if mode not in CHAT_MODES:
        raise ValueError(f"chat mode is one of {', '.join(CHAT_MODES)}")
    text = _project_text(p)
    line = f"chat_mode = {toml_str(mode)}"
    first_table = re.search(r"(?m)^\[", text)
    head, rest = (text[:first_table.start()], text[first_table.start():]) if first_table else (text, "")
    if re.search(r"(?m)^chat_mode\s*=.*$", head):
        head = re.sub(r"(?m)^chat_mode\s*=.*$", lambda _m: line, head, count=1)
    else:
        head = line + "\n" + head
    _write_project(p, head + rest)


def allowed_commands(p: Project) -> list[str]:
    return [str(x) for x in _project_cfg(p).get("chat", {}).get("allowed_commands", [])]


def allow_command(p: Project, prefix: str) -> None:
    """Add a command prefix to `[chat] allowed_commands` in project.toml."""
    prefix = prefix.strip()
    if not prefix or SHELL_META.search(prefix):
        raise ValueError("an allowed command is a plain command prefix, e.g. `git status`")
    cmds = allowed_commands(p)
    if prefix in cmds:
        return
    cmds.append(prefix)
    line = "allowed_commands = [" + ", ".join(toml_str(c) for c in cmds) + "]"
    text = _project_text(p)
    m = re.search(r"(?m)^\[chat\][ \t]*$", text)
    if m:
        start = m.end()
        nxt = re.search(r"(?m)^\[", text[start:])
        end = start + nxt.start() if nxt else len(text)
        body = text[start:end]
        if re.search(r"(?m)^allowed_commands\s*=.*$", body):
            body = re.sub(r"(?m)^allowed_commands\s*=.*$", lambda _m: line, body, count=1)
        else:
            body = "\n" + line + body
        text = text[:start] + body + text[end:]
    else:
        text = text.rstrip("\n") + "\n\n[chat]\n" + line + "\n"
    if tomllib.loads(text).get("chat", {}).get("allowed_commands") != cmds:
        raise ValueError("couldn't update [chat] allowed_commands in project.toml; edit it by hand")
    _write_project(p, text)


def command_allowed(cmd: str, prefixes: list[str]) -> bool:
    """True if `cmd` is one plain command starting with an allowed prefix (whole words)."""
    c = cmd.strip()
    if not c or SHELL_META.search(c):
        return False
    return any(pre.strip() and (c == pre.strip() or c.startswith(pre.strip() + " ")) for pre in prefixes)


def suggest_prefix(cmd: str) -> str | None:
    """A prefix for "Always allow": the command and its subcommand, e.g. `git log` for `git log -5`."""
    c = cmd.strip()
    if not c or SHELL_META.search(c):
        return None
    words = []
    for w in c.split():
        if words and (w.startswith("-") or re.search(r"[/\\.:\"'=*]", w)):
            break
        words.append(w)
        if len(words) == 2:
            break
    return " ".join(words) or None


def guard_denied(cmd: str) -> str | None:
    """The `[guard] deny` pattern a command matches, if any."""
    return next((x for x in settings().get("guard", {}).get("deny", []) if re.search(x, cmd, re.I)), None)


def add_project(name: str, repo: str, base: str, preset: str = "none", account_name: str = "default",
                quick: list[str] | None = None, final: list[str] | None = None, env: dict | None = None,
                local_git: bool = False) -> Project:
    s = slug(name)
    d = DATA / "projects" / s
    if (d / "project.toml").exists():
        raise ValueError(f"a project named {s} exists")
    if not Path(repo).is_dir():
        raise ValueError(f"{repo} is not a folder")
    state = repo_state(repo)
    if state != "ready":
        if not local_git:
            raise NeedsLocalGit("not_a_repo" if state == "plain" else "no_commits", repo)
        base = setup_local_git(repo)
    gates = PRESETS.get(preset, PRESETS["none"])
    quick = quick if quick is not None else gates["quick"]
    final = final if final is not None else gates["final"]
    lines = [
        f"name = {toml_str(name)}", f"account = {toml_str(account_name)}", "",
        "[repo]", f"path = {toml_str(Path(repo).as_posix())}", f"base = {toml_str(base)}",
        f"worktrees = {toml_str((d / 'worktrees').as_posix())}", "copy_into_worktree = []",
        'co_author = "Co-Authored-By: Claude <noreply@anthropic.com>"', "",
        "[gates]", "quick = [" + ", ".join(toml_str(c) for c in quick) + "]",
        "final = [" + ", ".join(toml_str(c) for c in final) + "]", "max_feedback_chars = 6000", "timeout_s = 3600", "",
        "[gates.env]",
    ] + [f"{k} = {toml_str(str(v))}" for k, v in (env or {}).items()]
    (d / "tasks").mkdir(parents=True, exist_ok=True)
    (d / "project.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "notes.md").write_text(f"# Notes for agents working on {name} (durable facts; finished tasks append here)\n", encoding="utf-8")
    return project(s)


# ----------------------------------------------------------------------------- per-project state

class Store:
    def __init__(self, p: Project):
        self.p = p

    def log(self, msg: str) -> None:
        line = f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        with self.p.path("agentdesk.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load(self) -> dict:
        s = self.p.path("state.json")
        return json.loads(s.read_text(encoding="utf-8")) if s.exists() else {"tasks": {}, "spent_usd": 0.0}

    def save(self, state: dict) -> None:
        s = self.p.path("state.json")
        tmp = s.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(s)

    def notes(self) -> str:
        n = self.p.path("notes.md")
        return n.read_text(encoding="utf-8")[-4000:] if n.exists() else "(none yet)"


@dataclass
class Task:
    id: str
    title: str
    body: str
    tier: str | None = None
    depends: list[str] = field(default_factory=list)
    crates: list[str] = field(default_factory=list)


def load_tasks(p: Project) -> list[Task]:
    tasks = []
    for f in sorted(p.tasks_dir.glob("*.md")):
        m = re.match(r"\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)", f.read_text(encoding="utf-8"), re.S)
        if not m:
            continue
        try:
            h = tomllib.loads(m.group(1))
            tasks.append(Task(str(h["id"]), h["title"], m.group(2).strip(), h.get("tier"), list(h.get("depends", [])), list(h.get("crates", []))))
        except (tomllib.TOMLDecodeError, KeyError):
            continue
    return tasks


def add_task(p: Project, task_id: str, title: str, body: str, tier: str | None = None, depends: list[str] | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("task ids use letters, digits, - and _")
    head = [f"id = {toml_str(task_id)}", f"title = {toml_str(title)}"]
    if tier:
        head.append(f"tier = {toml_str(tier)}")
    if depends:
        head.append("depends = [" + ", ".join(toml_str(d) for d in depends) + "]")
    p.tasks_dir.mkdir(parents=True, exist_ok=True)
    f = p.tasks_dir / f"{task_id}.md"
    f.write_text("+++\n" + "\n".join(head) + "\n+++\n\n" + body.strip() + "\n", encoding="utf-8")
    return f


def next_task_id(p: Project) -> str:
    nums = [int(t.id) for t in load_tasks(p) if t.id.isdigit()]
    return f"{(max(nums) + 1) if nums else 1:03d}"


# ----------------------------------------------------------------------------- processes, worktrees, gates

def scratch_home() -> str:
    """A throw-away AGENTDESK_HOME for commands run by workers and checks, so tests of AgentDesk itself
    (or a stray `stop`) can never touch the real data folder, pid files or settings."""
    import tempfile
    d = Path(tempfile.gettempdir()) / "agentdesk-worker-home"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def run(cmd, cwd: Path, env: dict | None = None, timeout: int = 3600, shell: bool = False) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, env={**os.environ, "AGENTDESK_HOME": scratch_home(), **(env or {})}, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, shell=shell)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"timed out after {timeout}s\n{str(e.stdout or '')[-2000:]}"
    except OSError as e:
        return 127, str(e)


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    return run(["git", *args], cwd)


def add_worktree(p: Project, wt: Path, branch: str, base: str) -> None:
    """`git worktree add` at `wt` on a new `branch` from `base` (or on `branch` if it exists already)."""
    wt.parent.mkdir(parents=True, exist_ok=True)
    code, out = git(["worktree", "add", str(wt), "-b", branch, base], p.repo)
    if code != 0:
        code, out = git(["worktree", "add", str(wt), branch], p.repo)
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out}")
    for name in p.cfg["repo"].get("copy_into_worktree", []):
        src = p.repo / name
        if src.is_file():
            (wt / name).write_bytes(src.read_bytes())


def ensure_worktree(p: Project, store: Store, task: Task) -> Path:
    wt = Path(p.cfg["repo"]["worktrees"]) / task.id
    if wt.exists():
        return wt
    branch, base = f"auto/{task.id}", p.cfg["repo"]["base"]
    state = store.load()
    for dep in task.depends:
        if state["tasks"].get(dep, {}).get("status") == "done":
            base = f"auto/{dep}"
    add_worktree(p, wt, branch, base)
    store.log(f"{task.id}: worktree {wt} on {branch} from {base}")
    return wt


def cowork_path(p: Project) -> Path:
    return Path(p.cfg["repo"]["worktrees"]) / COWORK


def cowork_worktree(p: Project) -> Path:
    """The Coworker chat's own worktree `<worktrees>/cowork` on branch `auto/cowork` (created from the base
    branch on first use, then kept): it edits and commits there, never in the user's checkout."""
    wt = cowork_path(p)
    if not wt.exists():
        add_worktree(p, wt, f"auto/{COWORK}", p.cfg["repo"]["base"])
        Store(p).log(f"chat: coworker worktree {wt} on auto/{COWORK}")
    return wt


def changed_crates(wt: Path, base: str) -> list[str] | None:
    code, out = git(["diff", "--name-only", f"{base}...HEAD"], wt)
    _, dirty = git(["status", "--porcelain"], wt)
    files = set(out.split()) if code == 0 else set()
    files |= {l[3:].strip() for l in dirty.splitlines() if len(l) > 3}
    crates: set[str] = set()
    for f in files:
        f = f.replace("\\", "/")
        m = re.match(r"crates/([^/]+)/", f)
        if not m:
            if f.endswith((".rs", "Cargo.toml", "Cargo.lock")):
                return None
            continue
        manifest = wt / "crates" / m.group(1) / "Cargo.toml"
        if manifest.exists():
            name = tomllib.loads(manifest.read_text(encoding="utf-8")).get("package", {}).get("name")
            if name:
                crates.add(name)
    return sorted(crates)


def trim(output: str, limit: int) -> str:
    lines = output.splitlines()
    keep: list[str] = []
    for i, l in enumerate(lines):
        if re.search(r"^\s*(error|warning)(\[|:)|^---- |panicked at|^failures:|test result: FAILED|Diff in|FAILED|Error:|AssertionError|Traceback", l):
            keep.extend(lines[i:i + 12])
    text = "\n".join(dict.fromkeys(keep))
    tail = "\n".join(lines[-40:])
    return ((text + "\n...\n" + tail) if text else tail)[-limit:]


def run_gates(p: Project, store: Store, wt: Path, kind: str, crates: list[str] | None) -> tuple[bool, str]:
    g = p.cfg.get("gates", {})
    sel = "--workspace" if not crates else " ".join(f"-p {c}" for c in crates)
    for cmd in g.get(kind, []):
        cmd = cmd.replace("{crates}", sel)
        store.log(f"  gate: {cmd}")
        code, out = run(cmd, wt, env={k: str(v) for k, v in g.get("env", {}).items()}, timeout=int(g.get("timeout_s", 3600)), shell=True)
        if code != 0:
            return False, f"$ {cmd}\n{trim(out, int(g.get('max_feedback_chars', 6000)))}"
    return True, ""


# ----------------------------------------------------------------------------- auto-runner (pure logic; the loop is in server.py)

NL = chr(10)
AUTO_DEFAULTS = {"enabled": True, "interval_s": 30, "retry_crashed": 1}


def autorunner_cfg(p: Project | None = None, global_cfg: dict | None = None, project_cfg: dict | None = None) -> dict:
    """Effective auto-runner settings: code defaults < settings.toml [autorunner] < project.toml [autorunner]."""
    g = settings().get("autorunner", {}) if global_cfg is None else global_cfg
    pc = (p.cfg.get("autorunner", {}) if p is not None else {}) if project_cfg is None else project_cfg
    out = {**AUTO_DEFAULTS, **g, **pc}
    out["enabled"] = bool(out["enabled"])
    out["interval_s"] = max(5, int(out["interval_s"]))
    out["retry_crashed"] = max(0, int(out["retry_crashed"]))
    return out


def _set_toml_key(text: str, table: str, key: str, value: str) -> str:
    """Set `key = value` inside [table] of a TOML text (the table is added at the end if missing)."""
    lines = text.splitlines()
    head = next((n for n, ln in enumerate(lines) if ln.strip() == f"[{table}]"), None)
    if head is None:
        return text.rstrip(NL) + f"{NL}{NL}[{table}]{NL}{key} = {value}{NL}"
    end = next((n for n in range(head + 1, len(lines)) if lines[n].lstrip().startswith("[")), len(lines))
    at = next((n for n in range(head + 1, end) if re.match(rf"\s*{key}\s*=", lines[n])), None)
    if at is None:
        lines.insert(head + 1, f"{key} = {value}")
    else:
        lines[at] = f"{key} = {value}"
    return NL.join(lines) + NL


def set_autorunner(p: Project | None, enabled: bool) -> None:
    """Per project (project.toml) or, with p=None, app-wide (settings.toml). Only called when the user changes it."""
    val = "true" if enabled else "false"
    if p is not None:
        _write_project(p, _set_toml_key(_project_text(p), "autorunner", "enabled", val))
        return
    path = DATA / "settings.toml"
    settings()
    new = _set_toml_key(path.read_text(encoding="utf-8"), "autorunner", "enabled", val)
    tomllib.loads(new)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def classify_failure(failure: str | None, status: str | None = None, exit_code: int | None = None) -> str:
    """"crash" (process died / infrastructure: worth one retry), "gate" (the checks failed), "task" (the task
    itself failed: blocked) or "none"."""
    f = (failure or "").lstrip()
    if f.startswith("worker exited unexpectedly"):
        return "crash"
    if f.startswith("$ ") or f.startswith("The checks failed"):
        return "gate"
    if status == "blocked":
        return "task"
    if status in ("working", "routing") and exit_code not in (None, 0):
        return "crash"
    return "none"


def recover_stale(state: dict, alive: bool, exit_code: int | None = None) -> list[str]:
    """No run process is alive: tasks still `working`/`routing` go back to `pending` with last_failure noting
    the exit, and their `crashes` counter goes up. Mutates `state`; returns the task ids changed."""
    if alive:
        return []
    out = []
    for tid, e in state.get("tasks", {}).items():
        if e.get("status") in ("working", "routing"):
            code = f" (code {exit_code})" if exit_code is not None else ""
            e.update(status="pending", last_failure=f"worker exited unexpectedly{code}", crashes=int(e.get("crashes", 0)) + 1)
            out.append(tid)
    return out


def next_startable(tasks: list[Task], state: dict, run_going: bool, retry_crashed: int = 1) -> Task | None:
    """The first pending task whose dependencies are all done; None while a run is going. A task that crashed
    more than `retry_crashed` times, or whose checks failed, is flagged (attention_items) and never auto-started."""
    if run_going:
        return None
    for t in tasks:
        e = state.get("tasks", {}).get(t.id, {})
        if e.get("status", "pending") != "pending" or not ready(t, state):
            continue
        if int(e.get("crashes", 0)) > retry_crashed or classify_failure(e.get("last_failure"), "pending") == "gate":
            continue
        return t
    return None


def attention_items(slug: str, tasks: list[Task], state: dict, merged: dict[str, bool | None] | None = None,
                    retry_crashed: int = 1) -> list[dict]:
    """What needs a human (or the Coworker); nothing here is acted on. `merged`: task id -> branch merged into base
    (None = unknown). Each item: project, task, kind, reason, open ("report" | "log")."""
    out = []
    merged = merged or {}
    for t in tasks:
        e = state.get("tasks", {}).get(t.id, {})
        st, fail = e.get("status", "pending"), e.get("last_failure", "")
        kind = classify_failure(fail, st)

        def add(k: str, why: str, open_: str = "log") -> None:
            out.append({"project": slug, "task": t.id, "title": t.title, "kind": k, "reason": why, "open": open_})

        if int(e.get("crashes", 0)) > retry_crashed:
            add("repeated", f"crashed {e['crashes']} times; auto-retry used up: {fail.splitlines()[0] if fail else ''}".strip())
        elif kind == "gate" and st != "done":
            add("gate", "checks failed: " + (fail.splitlines()[0] if fail else ""), "log")
        elif st == "blocked":
            add("blocked", "blocked after all tiers: " + (fail.splitlines()[0][:120] if fail else "see the log"))
        if e.get("merge") == "conflict" or (st != "done" and "merge conflict" in fail.lower()):
            add("conflict", "merge conflict")
        if st == "done" and e.get("branch") and merged.get(t.id) is False and not e.get("reviewed"):
            add("unmerged", f"done, branch {e['branch']} is not merged and not reviewed", "report")
    return out


def branch_merged(p: Project, branch: str) -> bool | None:
    """Read-only: is `branch` an ancestor of the base branch? None if it can't be told."""
    base = p.cfg.get("repo", {}).get("base", "main")
    try:
        if git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], p.repo)[0] != 0:
            return None
        return git(["merge-base", "--is-ancestor", branch, base], p.repo)[0] == 0
    except OSError:
        return None


# ----------------------------------------------------------------------------- agents

def usage_tokens(usage) -> dict:
    u = usage if isinstance(usage, dict) else {}
    return {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0), "cache_write": int(u.get("cache_creation_input_tokens") or 0)}


class Meter:
    """Spend and usage-limit readings: per project (spend) and per account (limits)."""

    def __init__(self, p: Project, store: Store, state: dict):
        self.p, self.store, self.state = p, store, state
        self.limits = settings()["limits"]
        self.acct = p.account.get("name", "default")
        self.utilization = 0.0

    def see(self, msg) -> None:
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if info.utilization is not None:
                self.utilization = max(self.utilization, float(info.utilization))
            record_rate_limit(self.acct, info)
        if isinstance(msg, ResultMessage):
            if msg.total_cost_usd:
                self.state["spent_usd"] = round(self.state.get("spent_usd", 0.0) + msg.total_cost_usd, 4)
            totals = self.state.setdefault("tokens", {})
            for k, v in usage_tokens(msg.usage).items():
                totals[k] = totals.get(k, 0) + v
            self.store.save(self.state)

    def stop_reason(self) -> str | None:
        if self.utilization >= float(self.limits["pause_at_utilization"]):
            return f"usage limit {self.utilization:.0%} reached the pause threshold"
        if self.state.get("spent_usd", 0.0) >= float(self.limits["run_budget_usd"]) + self.state.get("budget_base", 0.0):
            return f"run budget {self.limits['run_budget_usd']} USD spent"
        return None


def record_rate_limit(acct: str, info) -> None:
    """Save a usage-limit reading (RateLimitInfo) for the account; the dashboard gauge reads it."""
    usage = account_usage(acct)
    usage.setdefault("rate_limits", {})[str(info.rate_limit_type or "limit")] = {
        "utilization": info.utilization, "status": str(info.status), "resets_at": info.resets_at,
        "seen": dt.datetime.now().isoformat(timespec="seconds")}
    save_account_usage(acct, usage)


def over_pause(utilization: float | None) -> str | None:
    """Why work pauses at this usage reading (`[limits] pause_at_utilization`), or None."""
    at = float(settings()["limits"]["pause_at_utilization"])
    if utilization is not None and float(utilization) >= at:
        return f"account usage {float(utilization):.0%} reached the pause threshold ({at:.0%})"
    return None


def usage_paused(acct: str, now: float | None = None) -> str | None:
    """The pause reason from the account's saved readings that still apply (window not reset yet; readings
    without a reset time count for 5 hours)."""
    now = dt.datetime.now().timestamp() if now is None else now
    for kind, r in account_usage(acct).get("rate_limits", {}).items():
        resets = r.get("resets_at")
        if resets is None:
            try:
                resets = dt.datetime.fromisoformat(str(r.get("seen"))).timestamp() + 5 * 3600
            except ValueError:
                continue
        if float(resets) > now and (why := over_pause(r.get("utilization"))):
            return f"{why} ({kind})"
    return None


SHELL_TOOLS = ("Bash", "PowerShell")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def norm_path(path: str | Path, base: Path) -> str:
    """Absolute, normalized (`..` and symlinks resolved), lower-case, forward slashes."""
    p = Path(path)
    return os.path.realpath(p if p.is_absolute() else base / p).lower().replace("\\", "/")


def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def guard(wt: Path, also: tuple[Path, ...] = (), never: tuple[Path, ...] = (), extra_deny: tuple[str, ...] = (),
          who: str = "automated workers"):
    """can_use_tool for agents that work on their own: shell commands matching `[guard] deny` (or
    `extra_deny`) are refused, and edits must land inside `wt` or one of `also` (folders or files), but
    never inside `never` (other than `wt` itself). Everything else is allowed."""
    deny = [re.compile(x, re.I) for x in [*settings()["guard"]["deny"], *extra_deny]]
    root = norm_path(wt, wt)
    roots = [norm_path(a, wt) for a in also]
    blocked = [norm_path(n, wt) for n in never]

    async def can_use_tool(name: str, args: dict, _ctx):
        if name in SHELL_TOOLS:
            cmd = str(args.get("command", ""))
            for x in deny:
                if x.search(cmd):
                    return PermissionResultDeny(message=f"not allowed for {who}: {x.pattern}")
        if name in EDIT_TOOLS:
            raw = str(args.get("file_path") or args.get("notebook_path") or "")
            target = norm_path(raw, wt) if raw else ""
            ok = bool(target) and (inside(target, root) or (any(inside(target, r) for r in roots)
                                                            and not any(inside(target, b) for b in blocked)))
            if not ok:
                return PermissionResultDeny(message=f"edit files inside your worktree only ({wt})")
        return PermissionResultAllow()

    return can_use_tool


ROUTER_SCHEMA = {"type": "object", "required": ["tier", "why", "brief"], "properties": {
    "tier": {"type": "string", "enum": TIER_ORDER}, "why": {"type": "string"}, "brief": {"type": "string"},
    "crates": {"type": "array", "items": {"type": "string"}}}}

ROUTER_PROMPT = """You route a coding task to the cheapest worker that will do it well, and write its brief.

Tiers: mechanical (clear, local edits: strings, renames, small fixes with an obvious cause, docs),
standard (normal feature work or debugging in a few files), hard (design across modules, subtle
algorithms, unclear root causes, UI architecture).

Explore the repository read-only (Read, Grep, Glob; keep it short) to find the files involved. Then
write the brief: the goal, the relevant files with line hints, constraints from the project's
instructions (CLAUDE.md, AGENTS.md, README) that matter, and how to tell it's done. At most 400
words. Don't solve the task.

Project notes from earlier tasks:
{notes}

Task {id}: {title}
{body}
"""

WORKER_PROMPT = """You are an automated worker on task {id}: {title}.
Your working directory is a dedicated git worktree; edit files only inside it. Follow the project's
instructions (CLAUDE.md / AGENTS.md / README). Never push, open pull requests, publish, or change git
remotes; the orchestrator commits for you. Before finishing, format the code and run the tests of
what you changed.
Finish with a short summary of what you changed, then a line `NOTES:` followed by at most three
lines of durable facts later tasks should know (or `NOTES: none`).

Brief:
{brief}

Full task:
{body}
"""


def options(p: Project, **kw) -> ClaudeAgentOptions:
    env = {**account_env(p.account), "AGENTDESK_HOME": scratch_home(), **kw.pop("env", {})}
    return ClaudeAgentOptions(env=env, **kw)


async def route(p: Project, store: Store, task: Task, meter: Meter) -> dict:
    r = settings()["router"]
    opts = options(p, model=r["model"], max_turns=int(r["max_turns"]), max_budget_usd=float(r["budget_usd"]), cwd=str(p.repo),
                   allowed_tools=["Read", "Grep", "Glob"], disallowed_tools=["Edit", "Write", "Bash", "PowerShell"],
                   permission_mode="default", setting_sources=["project"], output_format={"type": "json_schema", "schema": ROUTER_SCHEMA})
    result = None
    async for msg in query(prompt=ROUTER_PROMPT.format(notes=store.notes(), id=task.id, title=task.title, body=task.body), options=opts):
        meter.see(msg)
        if isinstance(msg, ResultMessage):
            entry = meter.state["tasks"].setdefault(task.id, {"title": task.title})
            entry.setdefault("runs", []).append({"tier": "router", "model": r["model"], "cost_usd": msg.total_cost_usd, "turns": msg.num_turns,
                                                 "error": msg.is_error, "tokens": usage_tokens(msg.usage), "at": dt.datetime.now().isoformat(timespec="seconds")})
            store.save(meter.state)
            if isinstance(msg.structured_output, dict):
                result = msg.structured_output
            elif msg.result and (m := re.search(r"\{.*\}", msg.result, re.S)):
                try:
                    result = json.loads(m.group(0))
                except ValueError:
                    pass
    if not result or result.get("tier") not in TIER_ORDER:
        result = {"tier": "standard", "why": "router gave no usable answer", "brief": task.body}
    if task.tier in TIER_ORDER:
        result.update(tier=task.tier, why="set in the task file")
    return result


async def work(p: Project, store: Store, task: Task, wt: Path, tier: str, brief: str, meter: Meter) -> tuple[bool, str]:
    s = settings()
    t = s["tiers"][tier]
    entry = meter.state["tasks"][task.id]
    gate_env = {k: str(v) for k, v in p.cfg.get("gates", {}).get("env", {}).items()}
    opts = options(p, model=t["model"], effort=t.get("effort"), max_turns=int(t["max_turns"]), max_budget_usd=float(t["budget_usd"]),
                   cwd=str(wt), permission_mode="acceptEdits", setting_sources=["project"], can_use_tool=guard(wt), env=gate_env)
    summary = ""
    async with ClaudeSDKClient(options=opts) as client:
        message = WORKER_PROMPT.format(id=task.id, title=task.title, brief=brief, body=task.body)
        for attempt in range(1, int(s["limits"]["retries_per_tier"]) + 1):
            store.log(f"{task.id}: {tier} ({t['model']}{', ' + t['effort'] if t.get('effort') else ''}) attempt {attempt}")
            await client.query(message)
            async for msg in client.receive_response():
                meter.see(msg)
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            summary = b.text
                if isinstance(msg, ResultMessage):
                    entry.setdefault("runs", []).append({"tier": tier, "model": t["model"], "cost_usd": msg.total_cost_usd, "turns": msg.num_turns,
                                                         "error": msg.is_error, "attempt": attempt, "tokens": usage_tokens(msg.usage),
                                                         "at": dt.datetime.now().isoformat(timespec="seconds")})
                    store.save(meter.state)
            ok, failure = run_gates(p, store, wt, "quick", task.crates or changed_crates(wt, p.cfg["repo"]["base"]))
            if ok:
                return True, summary
            store.log(f"{task.id}: checks failed")
            entry["last_failure"] = failure[-1500:]
            store.save(meter.state)
            if meter.stop_reason():
                return False, summary
            message = f"The checks failed. Fix the cause (not the check), then finish as before.\n\n{failure}"
    return False, summary


def commit(p: Project, task: Task, wt: Path, tiers: str) -> str:
    git(["add", "-A"], wt)
    code, out = git(["commit", "-q", "-m", f"{task.id}: {task.title}\n\nAutomated by AgentDesk ({tiers}).\n\n{p.cfg['repo']['co_author']}"], wt)
    if code != 0 and "nothing to commit" not in out:
        raise RuntimeError(out)
    return git(["rev-parse", "--short", "HEAD"], wt)[1].strip()


def keep_notes(p: Project, task: Task, summary: str) -> None:
    m = re.search(r"NOTES:\s*(.*)", summary, re.S)
    if not m or m.group(1).strip().lower().startswith("none"):
        return
    with p.path("notes.md").open("a", encoding="utf-8") as f:
        for l in [l.strip("-• ").strip() for l in m.group(1).strip().splitlines() if l.strip()][:3]:
            f.write(f"- ({task.id}) {l}\n")


def ready(task: Task, state: dict) -> bool:
    if state["tasks"].get(task.id, {}).get("status") in ("done", "blocked"):
        return False
    return all(state["tasks"].get(d, {}).get("status") == "done" for d in task.depends)


async def do_task(p: Project, store: Store, task: Task, meter: Meter) -> None:
    state = meter.state
    entry = state["tasks"].setdefault(task.id, {"title": task.title})
    entry.update(status="routing", started=dt.datetime.now().isoformat(timespec="seconds"))
    store.save(state)
    plan = await route(p, store, task, meter)
    entry.update(tier=plan["tier"], why=plan.get("why", ""), brief=plan.get("brief", ""), status="working")
    if plan.get("crates") and not task.crates:
        task.crates = [c for c in plan["crates"] if re.fullmatch(r"[A-Za-z0-9_-]+", c)]
    store.save(state)
    store.log(f"{task.id}: routed to {plan['tier']} ({plan.get('why', '')})")
    wt = ensure_worktree(p, store, task)
    tier, tried = plan["tier"], []
    while tier:
        tried.append(tier)
        ok, summary = await work(p, store, task, wt, tier, plan["brief"], meter)
        if ok:
            sha = commit(p, task, wt, " → ".join(tried))
            keep_notes(p, task, summary)
            entry.update(status="done", commit=sha, branch=f"auto/{task.id}", finished=dt.datetime.now().isoformat(timespec="seconds"))
            p.path("reports").mkdir(exist_ok=True)
            p.path("reports").joinpath(f"{task.id}.md").write_text(
                f"# {task.id}: {task.title}\n\nBranch `auto/{task.id}` at {sha}; tiers {' → '.join(tried)}.\n\n## Brief\n\n{plan['brief']}\n\n"
                f"## Worker summary\n\n{summary}\n", encoding="utf-8")
            store.save(state)
            store.log(f"{task.id}: done at {sha}")
            return
        if meter.stop_reason():
            entry["status"] = "paused"
            store.save(state)
            return
        tier = settings()["tiers"][tier].get("next")
        if tier:
            store.log(f"{task.id}: escalating to {tier}")
    entry["status"] = "blocked"
    store.save(state)
    store.log(f"{task.id}: blocked after {' → '.join(tried)}")


async def run_queue(p: Project, only: str | None = None, max_tasks: int | None = None) -> None:
    store = Store(p)
    state = store.load()
    state["budget_base"] = state.get("spent_usd", 0.0)  # the run budget counts from now
    meter = Meter(p, store, state)
    done = 0
    for task in load_tasks(p):
        if max_tasks and done >= max_tasks:
            break
        if only and task.id != only:
            continue
        if not only and not ready(task, state):
            continue
        if (why := meter.stop_reason()):
            store.log(f"stopping before {task.id}: {why}")
            break
        await do_task(p, store, task, meter)
        done += 1
    store.log(f"run finished: {done} task(s); project total {state.get('spent_usd', 0):.2f} USD")


async def plan_tasks(p: Project, only: str | None = None) -> None:
    store = Store(p)
    meter = Meter(p, store, store.load())
    for task in load_tasks(p):
        if only and task.id != only:
            continue
        plan = await route(p, store, task, meter)
        store.log(f"plan {task.id}: {plan['tier']} — {plan.get('why', '')}")
        meter.state["tasks"].setdefault(task.id, {"title": task.title})["brief"] = plan.get("brief", "")
        store.save(meter.state)


def final_gates(p: Project, task_id: str) -> None:
    store = Store(p)
    ok, failure = run_gates(p, store, Path(p.cfg["repo"]["worktrees"]) / task_id, "final", None)
    store.log(f"final checks for {task_id}: " + ("OK" if ok else "FAILED\n" + failure))


def run_async(coro) -> None:
    asyncio.run(coro)
