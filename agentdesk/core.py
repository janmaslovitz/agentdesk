"""AgentDesk core: projects, accounts, and the route → work → check → escalate pipeline.

Data lives in the data folder (`AGENTDESK_HOME`, else `data/` next to the app, so the whole app
folder can be copied to another computer):

    data/settings.toml                    accounts, default tiers, limits
    data/accounts/<name>.json             last usage-limit readings for that account
    data/projects/<slug>/project.toml     repo, base branch, gates, account
    data/projects/<slug>/tasks/*.md       the queue (TOML header between +++ lines)
    data/projects/<slug>/tasks/drafts/    the Planner's draft tasks (never run), with plan.json
    data/projects/<slug>/state.json, notes.md, reports/, agentdesk.log
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field, replace
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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller program
APP = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent


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


def no_console_flags(flags: int = 0) -> int:
    """Windows creationflags for a child process: no console window unless it asked for its own
    (CREATE_NEW_CONSOLE / DETACHED_PROCESS). The packaged AgentDesk.exe has no console, so without this
    every `git` or shell command it runs pops up a window."""
    if os.name != "nt":
        return flags
    own = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    return flags if flags & own else flags | subprocess.CREATE_NO_WINDOW


def _hide_child_consoles() -> None:
    """Make every subprocess.Popen in this program (git, checks, taskkill, the CLI ...) windowless on Windows."""
    if os.name != "nt" or getattr(subprocess.Popen, "_agentdesk_nowindow", False):
        return
    orig = subprocess.Popen.__init__

    def init(self, *args, **kw):
        kw["creationflags"] = no_console_flags(kw.get("creationflags", 0))
        orig(self, *args, **kw)

    subprocess.Popen.__init__ = init
    subprocess.Popen._agentdesk_nowindow = True


_hide_child_consoles()
def _data_home() -> Path:
    """AGENTDESK_HOME, else `data` next to the app. The packaged program (often under Program Files, which
    users can't write to) uses `data` next to the .exe only if it already exists there, else the AgentDesk folder in LOCALAPPDATA."""
    if os.environ.get("AGENTDESK_HOME"):
        return Path(os.environ["AGENTDESK_HOME"])
    if FROZEN and not (APP / "data").is_dir():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AgentDesk"
    return APP / "data"


DATA = _data_home()
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
usage_max_age_min = 30
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

# Coworker: the project's curator, autonomous (no approval cards); budget_usd is per message.
# full_access = true: works in the project checkout itself (main included) with every tool and command, no guard.
# full_access = false: guarded like the workers, in its own worktree (branch auto/cowork).
[chat.coworker]
full_access = true
# live = true (needs full_access): one persistent session that streams into the chat, runs background jobs and
# subagents, and can be woken by finished jobs. false: a fresh process per message, as before.
live = true
# budget_usd caps one burst of work (a message or a wake-up); daily_budget_usd caps everything the Coworker
# spends per day (0 = no cap). checkup_interval_min > 0: the Coworker checks the project by itself this often
# while a session is open (0 = never).
daily_budget_usd = 25.0
checkup_interval_min = 0
model = "sonnet"
effort = "medium"
budget_usd = 5.0

# Auto-runner: the dashboard starts the next ready task of every project by itself (no model calls),
# recovers runs that died, retries a crashed task retry_crashed times, and flags the rest.
# A project can override `enabled` in its project.toml under [autorunner].
[autorunner]
enabled = true
interval_s = 30
retry_crashed = 1

# Planner: reads only and writes a draft plan of tasks; nothing runs before you approve it. budget_usd is per message.
[chat.planner]
model = "sonnet"
effort = "medium"
budget_usd = 2.0

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

_VERSION: dict | None = None


def version_info() -> dict:
    """{version, commit, built, installed}: the version from agentdesk/__init__.py, plus the commit and build time
    stamped into the packaged program (tools/release.py buildinfo); from source the commit comes from git."""
    global _VERSION
    if _VERSION is None:
        from . import __version__
        commit, built = "", ""
        try:
            from . import _buildinfo
            commit, built = _buildinfo.COMMIT, _buildinfo.BUILT
        except ImportError:
            if not FROZEN:
                try:
                    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=APP, capture_output=True, text=True, timeout=5,
                                       **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                    commit = r.stdout.strip() if r.returncode == 0 else ""
                except (OSError, subprocess.SubprocessError):
                    pass
        _VERSION = {"version": __version__, "commit": commit, "built": built, "installed": FROZEN}
    return dict(_VERSION)


def version_text() -> str:
    v = version_info()
    extra = ", ".join(x for x in (v["commit"], f"built {v['built']}" if v["built"] else "") if x)
    return f"AgentDesk {v['version']}" + (f" ({extra})" if extra else "")


def record_version() -> tuple[str | None, str]:
    """Remember which version last ran on this data folder (data/app_version.json). Returns (previous version or None
    when unchanged/first run, current version); the caller logs an upgrade."""
    v = version_info()
    f = DATA / "app_version.json"
    try:
        prev = json.loads(f.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        # no record yet: data that already has a log came from a build before versions were recorded
        prev = "an earlier build" if (DATA / "agentdesk.log").exists() else None
    if prev == v["version"]:
        return None, v["version"]
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({**v, "previous": prev, "since": dt.datetime.now().isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
    except OSError:
        pass
    return prev, v["version"]


def settings() -> dict:
    p = DATA / "settings.toml"
    if not p.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_SETTINGS, encoding="utf-8")
    return _merged(copy.deepcopy(_defaults()), _toml_cached(p))


_DEFAULTS: dict | None = None


def _defaults() -> dict:
    global _DEFAULTS
    if _DEFAULTS is None:
        _DEFAULTS = tomllib.loads(DEFAULT_SETTINGS)
    return _DEFAULTS


def _merged(base: dict, over: dict) -> dict:
    """`over` on top of `base`, section by section: a settings.toml from an older version (or with a key deleted)
    still has every key the program reads. Lists and values in `over` replace the defaults whole."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merged(base[k], v)
        else:
            base[k] = v
    return base


_TOML_CACHE: dict[str, tuple[int, int, dict]] = {}


def _toml_cached(path: Path) -> dict:
    """Parsed TOML, re-read only when the file's mtime/size changes (the dashboard polls these constantly)."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _TOML_CACHE.get(str(path))
    if hit and hit[:2] == key:
        return hit[2]
    with path.open("rb") as f:
        data = tomllib.load(f)
    _TOML_CACHE[str(path)] = (key[0], key[1], data)
    return data


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


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


class UsageUnavailable(Exception):
    """The automatic usage reading couldn't be made; the message is safe to show (never contains the token).
    `busy`: the service asked us to slow down (HTTP 429); `retry_after`: seconds it named, if it did."""

    def __init__(self, message: str, busy: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.busy, self.retry_after = busy, retry_after


def credentials_file(name: str) -> Path:
    a = account(name)
    base = Path(a["config_dir"]).expanduser() if a.get("config_dir") else Path.home() / ".claude"
    return base / ".credentials.json"


def _utilization(block) -> tuple[float, str | None] | None:
    if isinstance(block, dict) and isinstance(block.get("utilization"), (int, float)):
        return float(block["utilization"]) / 100, block.get("resets_at")
    return None


def fetch_usage(name: str, timeout: float = 15) -> dict:
    """The account's real usage from Anthropic's usage endpoint, using the account's own sign-in token (read
    from its login folder for this one request; never stored, logged or returned). Returns
    {"five_hour": 0-1, "seven_day": 0-1, "five_hour_resets": str, "seven_day_resets": str} (missing windows left out)."""
    import urllib.error
    import urllib.request
    if account(name).get("kind") == "api_key":
        raise UsageUnavailable("API-key accounts have no subscription usage to read")
    try:
        oauth = json.loads(credentials_file(name).read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        token = str(oauth["accessToken"])
    except (OSError, ValueError, KeyError):
        raise UsageUnavailable("not signed in (no login found for this account): press Log in") from None
    if float(oauth.get("expiresAt") or 0) / 1000 < time.time():
        raise UsageUnavailable("sign-in token expired: run something with this account or press Log in")
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                                                     "User-Agent": "AgentDesk", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        wait = None
        try:
            wait = float(e.headers.get("Retry-After")) if e.headers is not None and e.headers.get("Retry-After") else None
        except (TypeError, ValueError):
            pass
        raise UsageUnavailable({401: "sign-in rejected: press Log in", 403: "usage not available for this plan",
                                429: "usage service is rate-limiting us"}.get(e.code, f"usage service answered {e.code}"),
                               busy=e.code == 429, retry_after=wait) from None
    except (OSError, ValueError) as e:  # URLError, timeouts, bad JSON
        raise UsageUnavailable(f"couldn't reach the usage service ({type(e).__name__})") from None
    out: dict = {}
    for key in ("five_hour", "seven_day"):
        u = _utilization(data.get(key) if isinstance(data, dict) else None)
        if u:
            out[key], out[key + "_resets"] = u[0], u[1]
    if "five_hour" not in out and "seven_day" not in out:
        raise UsageUnavailable("the usage service returned no figures for this account")
    return out


def usage_auto_enabled() -> bool:
    return bool(settings().get("limits", {}).get("usage_auto", True))


USAGE_BACKOFF_S = (120, 240, 480, 900, 1800)  # after each failure in a row that the service answered with "busy"


def refresh_usage(name: str, min_interval_s: float = 180, force: bool = False) -> str | None:
    """Read the real usage now (at most once per `min_interval_s`, and not before a "busy" answer's wait is over,
    unless forced) and save it as the account's reading with source "api". Returns None on success or when it's not
    due, else the reason it failed; a failure is remembered for the dashboard and leaves the previous (e.g. manual)
    reading in place. A rate-limited (429) answer backs off 2, 4, 8, 15, then 30 minutes (or the service's Retry-After)."""
    if not usage_auto_enabled() or account(name).get("kind") == "api_key":
        return None
    usage = account_usage(name)
    last = usage.get("auto") or {}
    nowdt = dt.datetime.now()
    try:
        age = (nowdt - dt.datetime.fromisoformat(str(last.get("checked")))).total_seconds()
    except ValueError:
        age = float("inf")
    try:
        wait_left = (dt.datetime.fromisoformat(str(last.get("next"))) - nowdt).total_seconds() if last.get("next") else 0
    except ValueError:
        wait_left = 0
    if not force and (age < min_interval_s or wait_left > 0):
        return last.get("error")
    err, fails, nxt = None, 0, None
    try:
        got = fetch_usage(name)
    except UsageUnavailable as e:
        got, err = None, str(e)
        if e.busy:
            fails = int(last.get("fails") or 0) + 1
            wait = e.retry_after or USAGE_BACKOFF_S[min(fails, len(USAGE_BACKOFF_S)) - 1]
            nxt = (nowdt + dt.timedelta(seconds=wait)).isoformat(timespec="seconds")
            err = f"{err}: next try at {nxt[11:16]}"
    usage = account_usage(name)  # re-read: another process may have written meanwhile
    now = nowdt.isoformat(timespec="seconds")
    if got:
        usage["reading"] = {"utilization": max(v for k, v in got.items() if k in ("five_hour", "seven_day")),
                            "at": now, "source": "api", **got}
    usage["auto"] = {"checked": now, "error": err, "fails": fails, "next": nxt}
    save_account_usage(name, usage)
    return err


def usage_max_age_min() -> float:
    return float(settings()["limits"].get("usage_max_age_min", 30))


def save_reading(name: str, fraction: float, source: str) -> None:
    """Save the account's current usage (0.0-1.0), when it was read and where from ("sdk" or "manual")."""
    usage = account_usage(name)
    usage["reading"] = {"utilization": float(fraction), "at": dt.datetime.now().isoformat(timespec="seconds"), "source": source}
    save_account_usage(name, usage)


def current_reading(name: str) -> dict | None:
    """The newest usage reading {utilization, at, source, age_min}, or None when there is none (null values don't count).
    Old files without "reading" fall back to their rate-limit readings."""
    usage = account_usage(name)
    found = []
    r = usage.get("reading")
    if isinstance(r, dict) and r.get("utilization") is not None:
        found.append((r.get("at"), float(r["utilization"]), r.get("source") or "manual", r))
    for r in (usage.get("rate_limits") or {}).values():
        if isinstance(r, dict) and r.get("utilization") is not None:
            found.append((r.get("seen"), float(r["utilization"]), "sdk", None))
    best = None
    for at, u, src, raw in found:
        try:
            t = dt.datetime.fromisoformat(str(at))
        except ValueError:
            continue
        if best is None or t > best[0]:
            best = (t, u, src, at, raw)
    if best is None:
        return None
    extra = {k: best[4][k] for k in ("five_hour", "seven_day", "five_hour_resets", "seven_day_resets") if best[4] and k in best[4]}
    return {"utilization": best[1], "at": best[3], "source": best[2], **extra,
            "age_min": max(0.0, (dt.datetime.now() - best[0]).total_seconds() / 60)}


def set_manual_usage(name: str, percent: float) -> None:
    if not 0 <= percent <= 100:
        raise ValueError("usage must be between 0 and 100")
    save_reading(name, percent / 100, "manual")


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
            out.append(Project(d.name, d, copy.deepcopy(_toml_cached(p))))
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
def replace_file(tmp: Path, target: Path, tries: int = 40) -> None:
    """`tmp.replace(target)`, retried for a moment: on Windows the rename fails while another handle has
    the target open (the dashboard or the plan watcher reading state.json, a virus scanner, ...)."""
    for i in range(tries):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


def write_atomic(target: Path, text: str) -> None:
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    replace_file(tmp, target)


# ----------------------------------------------------------------------------- chat settings (project.toml / settings.toml)

CHAT_MODES = ("assistant", "coworker", "planner")
CHAT_DEFAULT_MODELS = {"assistant": "haiku", "coworker": "sonnet", "planner": "sonnet"}
# Defaults for `[chat.<mode>]` keys missing from settings.toml (the model has its own fallback chain).
# full_access: the Coworker curates the whole project: it works in the project checkout itself (main included),
# with every tool and command and no guard. false = the older guarded mode in its own worktree (auto/cowork).
CHAT_DEFAULTS = {"assistant": {}, "coworker": {"effort": "medium", "budget_usd": 5.0, "max_turns": 60, "full_access": True,
                             "live": True, "daily_budget_usd": 25.0, "checkup_interval_min": 0},
                 "planner": {"effort": "medium", "budget_usd": 2.0, "max_turns": 30}}
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
    write_atomic(p.path("project.toml"), text)


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
        write_atomic(self.p.path("state.json"), json.dumps(state, indent=2, ensure_ascii=False))

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
    """The queue: `tasks/*.md` only. Draft plans live in `tasks/drafts/` (not globbed) and carry
    `status = "draft"` in their header, which is skipped too wherever such a file ends up."""
    tasks = []
    for f in sorted(p.tasks_dir.glob("*.md")):
        m = re.match(r"\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)", f.read_text(encoding="utf-8"), re.S)
        if not m:
            continue
        try:
            h = tomllib.loads(m.group(1))
            if h.get("status") == "draft":
                continue
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


# ----------------------------------------------------------------------------- draft plans (the Planner chat)
#
# One plan per project in <project>/plan.json: status "draft" (proposed, its tasks are files in
# tasks/drafts/ marked `status = "draft"`, which the queue never loads), then "approved" (the drafts
# became real tasks and the queue was started), then "finished". Only the user approves (dashboard).

PLAN_LOCK = threading.Lock()
MAX_PLAN_TASKS = 20


def drafts_dir(p: Project) -> Path:
    return p.tasks_dir / "drafts"


def load_plan(p: Project) -> dict | None:
    f = p.path("plan.json")
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except ValueError:
        return None


def save_plan(p: Project, plan: dict) -> None:
    write_atomic(p.path("plan.json"), json.dumps(plan, indent=2, ensure_ascii=False))


def plan_active(plan: dict | None) -> bool:
    return bool(plan) and plan.get("status") == "approved"


def plan_order(tasks: list[dict]) -> list[dict]:
    """The plan's tasks so each comes after the drafts it depends on (plan order where free)."""
    keys = {t["key"] for t in tasks}
    left, done, order = list(tasks), set(), []
    while left:
        nxt = next((t for t in left if all(d in done or d not in keys for d in t["depends"])), None)
        if nxt is None:
            raise ValueError("the plan's depends form a cycle: " + ", ".join(t["key"] for t in left))
        order.append(nxt)
        done.add(nxt["key"])
        left.remove(nxt)
    return order


def check_plan_tasks(p: Project, tasks) -> list[dict]:
    """The planner's task list, checked and normalized: keys d1..dN by position; depends name other
    drafts (dN) or tasks already in the queue; no cycles."""
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("a plan needs at least one task")
    if len(tasks) > MAX_PLAN_TASKS:
        raise ValueError(f"at most {MAX_PLAN_TASKS} tasks per plan")
    keys = [f"d{i}" for i in range(1, len(tasks) + 1)]
    queued = {t.id for t in load_tasks(p)}
    out = []
    for key, t in zip(keys, tasks):
        if not isinstance(t, dict):
            raise ValueError(f"{key}: each task is an object")
        title = str(t.get("title") or "").strip()
        if not title:
            raise ValueError(f"{key}: the title is missing")
        tier = t.get("tier") or None
        if tier is not None and tier not in TIER_ORDER:
            raise ValueError(f"{key}: tier is one of {', '.join(TIER_ORDER)} (or empty: the router decides)")
        deps = list(dict.fromkeys(str(d).strip() for d in t.get("depends") or [] if str(d).strip()))
        for d in deps:
            if d == key or (d not in keys and d not in queued):
                raise ValueError(f"{key}: depends on {d!r}, which is neither another task of this plan (d1..d{len(keys)}) nor a queued task")
        out.append({"key": key, "title": title[:200], "goal": str(t.get("goal") or "").strip(), "body": str(t.get("body") or "").strip(),
                    "files": [str(f) for f in t.get("files") or []][:30], "tier": tier, "depends": deps})
    plan_order(out)  # refuses cycles
    return out


def plan_task_body(t: dict) -> str:
    parts = [t["goal"]]
    if t["files"]:
        parts.append("## Where\n" + "\n".join(f"- {f}" for f in t["files"]))
    parts.append(t["body"])
    return "\n\n".join(x for x in parts if x) or t["title"]


def _write_drafts(p: Project, tasks: list[dict]) -> None:
    _clear_drafts(p)
    d = drafts_dir(p)
    d.mkdir(parents=True, exist_ok=True)
    for t in tasks:
        head = [f"id = {toml_str(t['key'])}", f"title = {toml_str(t['title'])}", 'status = "draft"']
        if t["tier"]:
            head.append(f"tier = {toml_str(t['tier'])}")
        if t["depends"]:
            head.append("depends = [" + ", ".join(toml_str(x) for x in t["depends"]) + "]")
        (d / f"{t['key']}.md").write_text("+++\n" + "\n".join(head) + "\n+++\n\n" + plan_task_body(t) + "\n", encoding="utf-8")


def _clear_drafts(p: Project) -> int:
    d, n = drafts_dir(p), 0
    if d.exists():
        for f in d.glob("*.md"):
            f.unlink()
            n += 1
        try:
            d.rmdir()
        except OSError:  # something else in there: leave it
            pass
    return n


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def propose_plan(p: Project, summary: str, tasks) -> dict:
    with PLAN_LOCK:
        old = load_plan(p)
        if old and old.get("status") == "draft":
            raise ValueError("there is a draft plan already: change it with update_plan, or cancel_plan first")
        if plan_active(old):
            raise ValueError("an approved plan is still going (one approved plan per project at a time); "
                             "it has to finish, or the user closes it on the plan card")
        norm = check_plan_tasks(p, tasks)
        plan = {"status": "draft", "summary": str(summary or "").strip()[:500], "tasks": norm, "version": 1, "created": _now()}
        _write_drafts(p, norm)
        save_plan(p, plan)
    Store(p).log(f"planner: draft plan with {len(norm)} task(s) in tasks/drafts/")
    return plan


def update_plan(p: Project, summary: str | None, tasks) -> dict:
    with PLAN_LOCK:
        plan = load_plan(p)
        if not plan or plan.get("status") != "draft":
            raise ValueError("there is no draft plan to change (an approved plan isn't rewritten); propose_plan makes one")
        norm = check_plan_tasks(p, tasks)
        plan.update(tasks=norm, version=int(plan.get("version", 1)) + 1, updated=_now())
        if summary:
            plan["summary"] = str(summary).strip()[:500]
        _write_drafts(p, norm)
        save_plan(p, plan)
    Store(p).log(f"planner: draft plan changed ({len(norm)} task(s))")
    return plan


def cancel_plan(p: Project) -> int:
    """Delete the draft plan and its draft task files; returns how many drafts went."""
    with PLAN_LOCK:
        plan = load_plan(p)
        if plan_active(plan):
            raise ValueError("this plan is approved: its tasks are in the queue. Stop the run, or close the plan")
        n = _clear_drafts(p)
        p.path("plan.json").unlink(missing_ok=True)
    Store(p).log(f"planner: draft plan cancelled ({n} draft(s) removed)")
    return n


def close_plan(p: Project) -> None:
    """Forget an approved or finished plan (its tasks stay in the queue), so a new one can be made."""
    with PLAN_LOCK:
        plan = load_plan(p)
        if plan and plan.get("status") == "draft":
            raise ValueError("this plan is a draft: cancel it instead")
        p.path("plan.json").unlink(missing_ok=True)


def approve_plan(p: Project) -> dict[str, str]:
    """Turn the drafts into real tasks (new ids in dependency order, depends mapped) and mark the plan
    approved. Returns {draft key: task id}. Starting the queue is the caller's (server) job."""
    with PLAN_LOCK:
        plan = load_plan(p)
        if not plan or plan.get("status") != "draft":
            raise ValueError("there is no draft plan to approve")
        order = plan_order(check_plan_tasks(p, plan["tasks"]))  # checked again: the queue may have changed
        n, ids = int(next_task_id(p)), {}
        for t in order:
            while (p.tasks_dir / f"{n:03d}.md").exists():
                n += 1
            ids[t["key"]] = f"{n:03d}"
            n += 1
        for t in order:
            add_task(p, ids[t["key"]], t["title"], plan_task_body(t), t["tier"], [ids.get(d, d) for d in t["depends"]])
        _clear_drafts(p)
        plan.update(status="approved", ids=ids, approved=_now(), seen={})
        save_plan(p, plan)
    Store(p).log(f"planner: plan approved; tasks {', '.join(ids.values())} queued")
    return ids


def plan_estimate(tasks: list[dict]) -> tuple[float, float]:
    """A rough cost range: per task from ~15% of its tier's budget (unknown tier: standard) up to the
    full tier budget plus the router's."""
    s = settings()
    router = float(s.get("router", {}).get("budget_usd", 0.3))
    lo = hi = 0.0
    for t in tasks:
        b = float(s["tiers"].get(t.get("tier") or "standard", s["tiers"]["standard"])["budget_usd"])
        lo, hi = lo + 0.15 * b, hi + b + router
    return round(lo, 2), round(hi, 2)


def plan_view(p: Project, plan: dict | None = None) -> dict | None:
    """The plan card's data: tasks in dependency order with their real ids and status once approved."""
    plan = plan or load_plan(p)
    if not plan:
        return None
    state = Store(p).load()["tasks"]
    ids = plan.get("ids", {})
    rows = []
    for t in plan_order(plan["tasks"]):
        tid = ids.get(t["key"])
        rows.append({**t, "id": tid or t["key"], "depends": [ids.get(d, d) for d in t["depends"]],
                     "status": state.get(tid, {}).get("status", "pending") if tid else "draft"})
    lo, hi = plan_estimate(plan["tasks"])
    return {"status": plan["status"], "summary": plan.get("summary", ""), "version": plan.get("version", 1),
            "tasks": rows, "estimate": [lo, hi]}


def plan_brief(p: Project) -> str:
    """One line on the current plan for the Planner (it doesn't see the dashboard's buttons)."""
    v = plan_view(p)
    if not v:
        return "no plan"
    tasks = "; ".join(f"{t['id']} {t['status']} {t['title']}" for t in v["tasks"])
    return f"{v['status']} plan ({v['summary'] or 'no summary'}): {tasks}"


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
                           encoding="utf-8", errors="replace", timeout=timeout, shell=shell,
                           stdin=subprocess.DEVNULL, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"timed out after {timeout}s\n{str(e.stdout or '')[-2000:]}"
    except OSError as e:
        return 127, str(e)


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    return run(["git", *args], cwd)


# ----------------------------------------------------------------------------- git panel (user actions only)

def _g(p: "Project", *args: str, timeout: int = 120) -> tuple[int, str]:
    code, out = run(["git", *args], p.repo, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=timeout)
    return code, out.strip()


def git_remote(p: "Project") -> tuple[str, str] | None:
    """(name, url) of the project's remote (origin if present, else the first one), or None for local-only."""
    code, out = _g(p, "remote")
    names = out.split() if code == 0 else []
    if not names:
        return None
    name = "origin" if "origin" in names else names[0]
    code, url = _g(p, "remote", "get-url", name)
    return name, url if code == 0 else ""


def _count(p: "Project", rng: str) -> int:
    code, out = _g(p, "rev-list", "--count", rng)
    return int(out) if code == 0 and out.isdigit() else 0


def git_status(p: "Project") -> dict:
    base = p.cfg["repo"]["base"]
    _, branch = _g(p, "rev-parse", "--abbrev-ref", "HEAD")
    _, porcelain = _g(p, "status", "--porcelain")
    remote = git_remote(p)
    st = {"branch": branch, "base": base, "clean": porcelain == "", "changes": porcelain.splitlines()[:50],
          "remote": remote[0] if remote else None, "remote_url": remote[1] if remote else None,
          "upstream": None, "ahead": 0, "behind": 0, "autos": []}
    code, up = _g(p, "rev-parse", "--abbrev-ref", "@{u}")
    if code != 0 and remote:
        cand = f"{remote[0]}/{branch}"
        if _g(p, "rev-parse", "--verify", "-q", f"refs/remotes/{cand}")[0] == 0:
            code, up = 0, cand
    if code == 0 and up:
        st["upstream"] = up
        st["ahead"], st["behind"] = _count(p, f"{up}..HEAD"), _count(p, f"HEAD..{up}")
    code, rows = _g(p, "for-each-ref", f"--format=%(refname:short) %(ahead-behind:{base})", "refs/heads/auto/")
    if code == 0:  # one process for all branches (git 2.41+)
        for line in rows.splitlines():
            name, _, rest = line.partition(" ")
            ahead = rest.split()[0] if rest.split() else ""
            if ahead.isdigit() and int(ahead):
                st["autos"].append({"branch": name, "ahead": int(ahead)})
        return st
    _, branches = _g(p, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/")
    for b in branches.splitlines():
        n = _count(p, f"{base}..{b}")
        if n:
            st["autos"].append({"branch": b, "ahead": n})
    return st


def git_fetch(p: "Project") -> dict:
    if git_remote(p) is None:
        return {"ok": False, "message": "local-only project: there is no remote to fetch from"}
    code, out = _g(p, "fetch", "--prune")
    return {"ok": code == 0, "message": out or "Fetched. Nothing new." if code == 0 else out}


def git_merge(p: "Project", branch: str) -> dict:
    """Merge `branch` into the base branch (never rewrites history). A conflict is aborted, leaving the repo as it was."""
    base = p.cfg["repo"]["base"]
    if not branch or branch.startswith("-") or _g(p, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] != 0:
        return {"ok": False, "message": f"there is no local branch '{branch}'"}
    _, cur = _g(p, "rev-parse", "--abbrev-ref", "HEAD")
    if cur != base:
        return {"ok": False, "message": f"the repo is on '{cur}', not on the base branch '{base}'. Switch to '{base}' first."}
    if _g(p, "status", "--porcelain")[1]:
        return {"ok": False, "message": "the working tree has uncommitted changes; commit or stash them first"}
    if _count(p, f"{base}..{branch}") == 0:
        return {"ok": True, "message": f"'{branch}' has nothing new for '{base}'. Already up to date."}
    _, before = _g(p, "rev-parse", "HEAD")
    code, out = _g(p, "merge", "--no-edit", branch)
    if code == 0:
        return {"ok": True, "message": out or f"Merged '{branch}' into '{base}'."}
    _, files = _g(p, "diff", "--name-only", "--diff-filter=U")
    conflicts = files.splitlines()
    _g(p, "merge", "--abort")
    if conflicts:
        return {"ok": False, "conflicts": conflicts, "message": f"Merge conflict in: {', '.join(conflicts)}. The merge was aborted; nothing changed."}
    if _g(p, "rev-parse", "HEAD")[1] != before:
        _g(p, "reset", "--merge", before)
    return {"ok": False, "message": out}


def git_pull(p: "Project", ff_only: bool = True) -> dict:
    if git_remote(p) is None:
        return {"ok": False, "message": "local-only project: there is no remote to pull from"}
    code, out = _g(p, "pull", "--ff-only" if ff_only else "--no-rebase", timeout=300)
    if code != 0 and ff_only and "fast-forward" in out.lower():
        out += "\nThe branches have diverged, so a fast-forward is not possible. Nothing was changed."
    return {"ok": code == 0, "message": out}


def git_push(p: "Project", branch: str | None = None) -> dict:
    """Push the current or base branch to the remote. Never forced."""
    remote = git_remote(p)
    if remote is None:
        return {"ok": False, "message": "local-only project: there is no remote to push to"}
    _, cur = _g(p, "rev-parse", "--abbrev-ref", "HEAD")
    base = p.cfg["repo"]["base"]
    branch = branch or cur
    if branch not in (cur, base):
        return {"ok": False, "message": f"only the current branch '{cur}' or the base branch '{base}' can be pushed"}
    code, out = _g(p, "push", "-u", remote[0], f"refs/heads/{branch}:refs/heads/{branch}", timeout=300)
    return {"ok": code == 0, "message": out or f"Pushed '{branch}' to {remote[0]}."}


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


def merged_branches(p: Project) -> tuple[set[str], set[str]] | None:
    """(all local branches, those already merged into the base branch) from two git calls, cached for 20 s and shared
    by every branch asked about (the dashboard asks for each done task on every poll: one process per branch made the
    whole app stall every 20 s). None if git can't tell."""
    base = p.cfg.get("repo", {}).get("base", "main")
    key = (str(p.repo), base)
    with _MERGED_LOCK:
        hit = _MERGED_CACHE.get(key)
        if hit and time.time() - hit[0] < 20:
            return hit[1]
        try:
            c1, heads = git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], p.repo)
            c2, merged = git(["branch", "--merged", base, "--format=%(refname:short)"], p.repo)
            res = (set(heads.split()), set(merged.split())) if c1 == 0 and c2 == 0 else None
        except OSError:
            res = None
        _MERGED_CACHE[key] = (time.time(), res)
        return res


def branch_merged(p: Project, branch: str) -> bool | None:
    """Read-only: is `branch` an ancestor of the base branch? None if it can't be told (or there is no such branch)."""
    sets = merged_branches(p)
    if sets is None or branch not in sets[0]:
        return None
    return branch in sets[1]


_MERGED_CACHE: dict[tuple[str, str], tuple[float, tuple[set[str], set[str]] | None]] = {}
_MERGED_LOCK = threading.Lock()


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

    def mark(self) -> dict | None:
        """The current usage reading, to compare with a later one."""
        return current_reading(self.acct)

    def used(self, before: dict | None) -> dict:
        """usage_before / usage_after / usage_used_pct (percent points; None = unknown) for a run that started at `before`.
        Unknown unless a newer reading arrived since."""
        after = current_reading(self.acct)
        pct = lambda r: None if r is None else round(r["utilization"] * 100, 1)
        used = None
        if before and after and after["at"] != before["at"]:
            used = round(max(0.0, pct(after) - pct(before)), 1)
        return {"usage_before": pct(before), "usage_after": pct(after), "usage_used_pct": used}

    def see(self, msg) -> None:
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            record_rate_limit(self.acct, info)
        if isinstance(msg, ResultMessage):
            if msg.total_cost_usd:
                self.state["spent_usd"] = round(self.state.get("spent_usd", 0.0) + msg.total_cost_usd, 4)
            totals = self.state.setdefault("tokens", {})
            for k, v in usage_tokens(msg.usage).items():
                totals[k] = totals.get(k, 0) + v
            self.store.save(self.state)

    def stop_reason(self) -> str | None:
        if self.state.get("spent_usd", 0.0) >= float(self.limits["run_budget_usd"]) + self.state.get("budget_base", 0.0):
            return f"run budget {self.limits['run_budget_usd']} USD spent"
        return None


def record_rate_limit(acct: str, info) -> None:
    """Save a usage-limit reading (RateLimitInfo) for the account; the dashboard gauge reads it."""
    usage = account_usage(acct)
    usage.setdefault("rate_limits", {})[str(info.rate_limit_type or "limit")] = {
        "utilization": info.utilization, "status": str(info.status), "resets_at": info.resets_at,
        "seen": dt.datetime.now().isoformat(timespec="seconds")}
    if info.utilization is not None:
        usage["reading"] = {"utilization": float(info.utilization), "at": dt.datetime.now().isoformat(timespec="seconds"), "source": "sdk"}
    save_account_usage(acct, usage)


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


TRANSIENT = re.compile(r"ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|EPIPE|connection (dropped|refused|reset|closed|error)"
                       r"|socket hang up|fetch failed|network (error|is unreachable)|timed out|overloaded|API Error: (5\d\d|429)|"
                       r"Unable to connect|temporarily unavailable", re.I)


def is_transient(e: BaseException) -> bool:
    """A network or service hiccup (reset, refused, dropped, timeout, 5xx/overloaded): worth waiting and trying again,
    not a failure of the task. The SDK reports a dead CLI as ProcessError (often with no detail), so that counts too
    when the text says nothing else."""
    text = f"{type(e).__name__}: {e} {getattr(e, 'stderr', '') or ''}"
    return bool(TRANSIENT.search(text))


def network_delays() -> list[float]:
    d = settings().get("limits", {}).get("network_retry_s", [15, 45, 120, 300])
    return [float(x) for x in d] if isinstance(d, list) and d else []


def tool_summary(name: str, args: dict) -> str:
    """A one-line description of a tool call's input (command, file, pattern or the JSON arguments)."""
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


ACTIVITY_TEXT = 400              # chars kept of a text snippet, tool input or error
ACTIVITY_MAX_BYTES = 512 * 1024  # per task file; past it only the key events (start, result, check, retry ...) are kept
ACTIVITY_KEY = {"start", "routed", "result", "check", "retry", "network_retry", "escalate"}
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def activity_path(p: Project, task_id: str) -> Path:
    return p.path("runs") / f"{task_id}.jsonl"


class Activity:
    """The compact, append-only timeline of a task's runs: `runs/<id>.jsonl`, one JSON event per line (router and
    worker tool calls, files touched, text snippets, tool errors, results, checks, retries). Sizes are capped. A line
    is written with one call, so a reader sees whole lines (a torn last line is skipped by `read_activity`)."""

    def __init__(self, p: Project, task_id: str, role: str):
        self.path, self.role = activity_path(p, task_id), role
        self.calls: dict[str, str] = {}

    def add(self, kind: str, **fields) -> None:
        try:
            if kind not in ACTIVITY_KEY and self.path.exists() and self.path.stat().st_size > ACTIVITY_MAX_BYTES:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ev = {"at": dt.datetime.now().isoformat(timespec="seconds"), "role": self.role, "kind": kind}
            ev.update({k: (v[:ACTIVITY_TEXT] + "…" if isinstance(v, str) and len(v) > ACTIVITY_TEXT else v)
                       for k, v in fields.items() if v is not None})
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the timeline is a convenience: never fail a run over it

    def see(self, msg) -> None:
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    self.add("text", text=b.text.strip())
                elif isinstance(b, ToolUseBlock):
                    args = b.input or {}
                    self.calls[b.id] = b.name
                    path = args.get("file_path") or args.get("notebook_path")
                    self.add("tool", name=b.name, input=tool_summary(b.name, args),
                             file=str(path) if path and b.name in FILE_TOOLS else None)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.is_error:
                    self.add("tool_error", name=self.calls.get(b.tool_use_id, ""), text=result_text(b.content))
        elif isinstance(msg, ResultMessage):
            self.add("result", turns=msg.num_turns, cost_usd=msg.total_cost_usd, tokens=usage_tokens(msg.usage),
                     error=bool(msg.is_error), subtype=msg.subtype, text=(msg.result or None) if msg.is_error else None)


def read_activity(p: Project, task_id: str, limit: int = 500) -> list[dict]:
    """The last `limit` events of a task's timeline (unreadable lines skipped)."""
    f = activity_path(p, task_id)
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


async def route(p: Project, store: Store, task: Task, meter: Meter) -> dict:
    r = settings()["router"]
    opts = options(p, model=r["model"], max_turns=int(r["max_turns"]), max_budget_usd=float(r["budget_usd"]), cwd=str(p.repo),
                   allowed_tools=["Read", "Grep", "Glob"], disallowed_tools=["Edit", "Write", "Bash", "PowerShell"],
                   permission_mode="default", setting_sources=["project"], output_format={"type": "json_schema", "schema": ROUTER_SCHEMA})
    result = None
    before = meter.mark()
    delays, tries = network_delays(), 0
    act = Activity(p, task.id, "router")
    act.add("start", tier="router", model=r["model"], effort=r.get("effort"))
    while True:
        try:
            async for msg in query(prompt=ROUTER_PROMPT.format(notes=store.notes(), id=task.id, title=task.title, body=task.body), options=opts):
                meter.see(msg)
                act.see(msg)
                if isinstance(msg, ResultMessage):
                    entry = meter.state["tasks"].setdefault(task.id, {"title": task.title})
                    entry.setdefault("runs", []).append({"tier": "router", "model": r["model"], "effort": r.get("effort"),
                                                         "cost_usd": msg.total_cost_usd, "turns": msg.num_turns,
                                                         "error": msg.is_error, "tokens": usage_tokens(msg.usage), "at": dt.datetime.now().isoformat(timespec="seconds"),
                                                         **meter.used(before)})
                    store.save(meter.state)
                    if isinstance(msg.structured_output, dict):
                        result = msg.structured_output
                    elif msg.result and (m := re.search(r"\{.*\}", msg.result, re.S)):
                        try:
                            result = json.loads(m.group(0))
                        except ValueError:
                            pass
            break
        except Exception as e:
            if not is_transient(e) or tries >= len(delays):
                raise
            store.log(f"{task.id}: network error while routing ({str(e)[:120] or type(e).__name__}); retrying in {delays[tries]:g}s")
            act.add("network_retry", text=str(e)[:200] or type(e).__name__, delay_s=delays[tries])
            await asyncio.sleep(delays[tries])
            tries += 1
    if not result or result.get("tier") not in TIER_ORDER:
        result = {"tier": "standard", "why": "router gave no usable answer", "brief": task.body}
    if task.tier in TIER_ORDER:
        result.update(tier=task.tier, why="set in the task file")
    act.add("routed", tier=result.get("tier"), text=result.get("why", ""))
    return result


async def work(p: Project, store: Store, task: Task, wt: Path, tier: str, brief: str, meter: Meter) -> tuple[bool, str]:
    s = settings()
    t = s["tiers"][tier]
    entry = meter.state["tasks"][task.id]
    gate_env = {k: str(v) for k, v in p.cfg.get("gates", {}).get("env", {}).items()}
    opts = options(p, model=t["model"], effort=t.get("effort"), max_turns=int(t["max_turns"]), max_budget_usd=float(t["budget_usd"]),
                   cwd=str(wt), permission_mode="acceptEdits", setting_sources=["project"], can_use_tool=guard(wt), env=gate_env)
    summary = ""
    message = WORKER_PROMPT.format(id=task.id, title=task.title, brief=brief, body=task.body)
    attempts = int(s["limits"]["retries_per_tier"])
    attempt, net_try, session_id, delays = 1, 0, None, network_delays()
    act = Activity(p, task.id, "worker")
    while True:
        # A network error (reset, refused, dropped ...) ends the connection, not the task: wait, reconnect to the same
        # session and go on, instead of crashing the run (which counted against the task's retries).
        try:
            async with ClaudeSDKClient(options=replace(opts, resume=session_id) if session_id else opts) as client:
                while attempt <= attempts:
                    if meter.stop_reason():
                        return False, summary
                    before = meter.mark()
                    store.log(f"{task.id}: {tier} ({t['model']}{', ' + t['effort'] if t.get('effort') else ''}) attempt {attempt}")
                    act.add("start", tier=tier, model=t["model"], effort=t.get("effort"), attempt=attempt)
                    await client.query(message)
                    async for msg in client.receive_response():
                        meter.see(msg)
                        act.see(msg)
                        session_id = getattr(msg, "session_id", None) or session_id
                        if isinstance(msg, AssistantMessage):
                            for b in msg.content:
                                if isinstance(b, TextBlock):
                                    summary = b.text
                        if isinstance(msg, ResultMessage):
                            entry.setdefault("runs", []).append({"tier": tier, "model": t["model"], "effort": t.get("effort"),
                                                                 "cost_usd": msg.total_cost_usd, "turns": msg.num_turns,
                                                                 "error": msg.is_error, "attempt": attempt, "tokens": usage_tokens(msg.usage),
                                                                 "at": dt.datetime.now().isoformat(timespec="seconds"), **meter.used(before)})
                            store.save(meter.state)
                    ok, failure = run_gates(p, store, wt, "quick", task.crates or changed_crates(wt, p.cfg["repo"]["base"]))
                    gates = p.cfg.get("gates", {}).get("quick", [])
                    act.add("check", ok=ok, gates=len(gates), text="; ".join(gates) if ok else failure[-ACTIVITY_TEXT:])
                    if ok:
                        return True, summary
                    store.log(f"{task.id}: checks failed")
                    entry["last_failure"] = failure[-1500:]
                    store.save(meter.state)
                    if meter.stop_reason():
                        return False, summary
                    message = f"The checks failed. Fix the cause (not the check), then finish as before.\n\n{failure}"
                    attempt += 1
                    if attempt <= attempts:
                        act.add("retry", attempt=attempt, text="checks failed; the worker is asked to fix the cause")
            return False, summary
        except Exception as e:
            if not is_transient(e) or net_try >= len(delays):
                raise
            store.log(f"{task.id}: network error ({str(e)[:120] or type(e).__name__}); retrying in {delays[net_try]:g}s "
                      f"(try {net_try + 1}/{len(delays)})")
            act.add("network_retry", text=str(e)[:200] or type(e).__name__, delay_s=delays[net_try], resume=bool(session_id))
            if session_id:
                message = "A network error interrupted you. Continue the task exactly where you left off."
            await asyncio.sleep(delays[net_try])
            net_try += 1


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
            Activity(p, task.id, "orchestrator").add("escalate", tier=tier)
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


# ----------------------------------------------------------------------------- export / import of a project

EXPORT_FORMAT = 1
EXPORT_DIR = ".agentdesk"
MAX_FILE_BYTES = 20 * 1024 * 1024      # one file in an import
MAX_TOTAL_BYTES = 100 * 1024 * 1024    # everything in an import
MAX_ENTRIES = 5000
_EXPORT_TOP = ("project.toml", "notes.md", "state.json", "agentdesk.log")
_EXPORT_DIRS = ("tasks", "reports")
# runs/<id>.jsonl (the per-task run timelines) hold tool inputs and model text like agentdesk.log does, so they are
# logs: exported only with include_logs (redacted like everything else) and accepted back on import.
_EXPORT_LOG_DIRS = ("runs",)
_SECRET_KEY = re.compile(r"key|token|secret|passw|credential|auth", re.I)
_SECRET_TEXT = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}|\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{16,}")


class NameClash(ValueError):
    """A project with this name already exists; `suggested` is a free name."""

    def __init__(self, name: str, suggested: str):
        self.suggested = suggested
        super().__init__(f"a project named {name} exists; choose a new name (suggestion: {suggested})")


class BadExport(ValueError):
    """The export is not importable (bad paths, version, size, content)."""


def _secret_values() -> list[str]:
    """Values of local credentials that must never appear in an export (key from the environment, login folders)."""
    vals = []
    for a in settings().get("accounts", []):
        for v in (os.environ.get(a.get("key_env", "")) if a.get("key_env") else None, a.get("config_dir"), a.get("api_key"), a.get("token")):
            if v and len(str(v)) >= 6:
                vals.append(str(v))
    return vals


def _redact(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "[redacted]")
        text = text.replace(Path(v).as_posix(), "[redacted]")
    return _SECRET_TEXT.sub("[redacted]", text)


def _toml_edit(text: str, drop: set[tuple[str, str]], blank_secret_env: bool = False) -> str:
    out, sec = [], ""
    for line in text.splitlines():
        m = re.match(r"\s*\[+([^\]]+)\]+", line)
        if m:
            sec = m.group(1).strip()
        else:
            k = re.match(r"\s*([A-Za-z0-9_\-]+)\s*=", line)
            if k and (sec, k.group(1)) in drop:
                continue
            if k and blank_secret_env and sec == "gates.env" and _SECRET_KEY.search(k.group(1)):
                line = f'{k.group(1)} = ""'
        out.append(line)
    return "\n".join(out) + "\n"


def _portable_toml(text: str) -> str:
    """project.toml without the machine-specific repo path/worktrees and the account; secret-looking env values blanked."""
    res = _toml_edit(text, {("", "account"), ("repo", "path"), ("repo", "worktrees")}, blank_secret_env=True)
    tomllib.loads(res)
    return res


def _localize_toml(text: str, name: str, repo: str, worktrees: str, account: str | None) -> str:
    text = _toml_edit(text, {("", "account"), ("", "name"), ("repo", "path"), ("repo", "worktrees")}, blank_secret_env=True)
    lines = text.splitlines()
    head = [f"name = {toml_str(name)}"] + ([f"account = {toml_str(account)}"] if account else [])
    first = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    lines[first:first] = head + [""]
    repo_lines = [f"path = {toml_str(Path(repo).as_posix())}", f"worktrees = {toml_str(Path(worktrees).as_posix())}"]
    at = next((i for i, l in enumerate(lines) if l.strip() == "[repo]"), None)
    if at is None:
        lines += ["", "[repo]", *repo_lines]
    else:
        lines[at + 1:at + 1] = repo_lines
    res = "\n".join(lines) + "\n"
    tomllib.loads(res)
    return res


def export_files(p: Project, include_logs: bool = False) -> dict[str, bytes]:
    """The files of an export (relative posix path -> bytes). An allow-list: nothing else in the project folder
    (worktrees, pid files, runner log, account files) can leak in. Known secret values are redacted."""
    files: dict[str, bytes] = {}

    def add(rel: str, data: bytes) -> None:
        try:
            files[rel] = _redact(data.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            pass  # binary files are not part of an export

    for n in _EXPORT_TOP:
        f = p.path(n)
        if f.is_file() and (n != "agentdesk.log" or include_logs):
            text = f.read_text(encoding="utf-8", errors="replace")
            add(n, (_portable_toml(text) if n == "project.toml" else text).encode("utf-8"))
    for d in _EXPORT_DIRS + (_EXPORT_LOG_DIRS if include_logs else ()):
        for f in sorted(p.path(d).rglob("*")) if p.path(d).is_dir() else []:
            if f.is_file() and not f.is_symlink():
                add(f.relative_to(p.dir).as_posix(), f.read_bytes())
    for f in sorted(p.dir.glob("chat*.json")):  # chat history / per-project chat settings
        if f.is_file():
            add(f.name, f.read_bytes())
    return files


def _manifest(p: Project, files: dict[str, bytes], include_logs: bool) -> bytes:
    from . import __version__
    m = {"format": EXPORT_FORMAT, "project": p.name, "slug": p.slug, "base": p.cfg.get("repo", {}).get("base", "main"),
         "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "app_version": __version__,
         "includes_logs": include_logs, "files": sorted(files)}
    return json.dumps(m, indent=2, ensure_ascii=False).encode("utf-8")


def export_project(p: Project, dest: Path | str | None = None, include_logs: bool = False) -> bytes | Path:
    """Write the export as a .zip to `dest` (returns the path), or return the zip bytes if `dest` is None."""
    import io
    import zipfile
    files = export_files(p, include_logs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", _manifest(p, files, include_logs))
        for rel, data in files.items():
            z.writestr(rel, data)
    if dest is None:
        return buf.getvalue()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buf.getvalue())
    return dest


def _push_dir(repo: Path) -> dict:
    """Push the current branch of `repo` to its first remote (origin preferred). Never forced, never changes remotes."""
    def g(*a, timeout=120):
        code, out = run(["git", *a], repo, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=timeout)
        return code, out.strip()
    names = g("remote")[1].split()
    if not names:
        return {"ok": False, "message": "local-only: there is no remote to push to"}
    remote = "origin" if "origin" in names else names[0]
    branch = g("rev-parse", "--abbrev-ref", "HEAD")[1]
    code, out = g("push", "-u", remote, f"refs/heads/{branch}:refs/heads/{branch}", timeout=300)
    return {"ok": code == 0, "message": out or f"Pushed '{branch}' to {remote}."}


def export_to_git(p: Project, config_repo: str | None = None, include_logs: bool = False, push: bool = False) -> dict:
    """Commit the export into `<project repo>/.agentdesk/` (or `<config_repo>/<slug>/`). Only that folder is committed.
    Pushing is separate (`push=True`; the server requires the user's confirmation, as for the git panel)."""
    repo = Path(config_repo) if config_repo else p.repo
    if not repo.is_dir() or not (repo / ".git").exists():
        return {"ok": False, "message": f"{repo} is not a git repository"}
    folder = repo / (p.slug if config_repo else EXPORT_DIR)
    files = export_files(p, include_logs)
    manifest = _manifest(p, files, include_logs)
    old = folder / "manifest.json"
    if old.is_file():  # unchanged content keeps the old manifest (its timestamp) so there is nothing to commit
        try:
            same = {f.relative_to(folder).as_posix(): f.read_bytes() for f in folder.rglob("*") if f.is_file()}
            same.pop("manifest.json")
            if same == files and json.loads(old.read_bytes()).get("includes_logs") == include_logs:
                manifest = old.read_bytes()
        except (OSError, ValueError):
            pass
    if folder.exists():
        shutil.rmtree(folder)  # deleted tasks/reports disappear too; history keeps them
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_bytes(manifest)
    for rel, data in files.items():
        t = folder / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(data)
    rel = folder.relative_to(repo).as_posix()
    ident = ["-c", "user.name=AgentDesk", "-c", "user.email=agentdesk@localhost"]
    if git(["add", "-A", "--", rel], repo)[0] != 0:
        return {"ok": False, "message": "git add failed"}
    if git(["diff", "--cached", "--quiet", "--", rel], repo)[0] == 0:
        res = {"ok": True, "committed": False, "message": "nothing changed since the last export"}
    else:
        code, out = git([*ident, "commit", "-m", f"AgentDesk: export project {p.name}", "--", rel], repo)
        if code != 0:
            return {"ok": False, "message": f"git commit failed: {out}"}
        res = {"ok": True, "committed": True, "message": f"committed to {rel}"}
    if push:
        pr = _push_dir(repo)
        res = {**res, "ok": pr["ok"], "pushed": pr["ok"], "message": res["message"] + "; " + pr["message"]}
    return res


def _safe_rel(name: str) -> str | None:
    """The cleaned relative path of an archive entry; raises BadExport if it escapes the project folder."""
    n = name.replace("\\", "/")
    parts = n.split("/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in parts or "\x00" in n:
        raise BadExport(f"unsafe path in the export: {name!r}")
    parts = [x for x in parts if x not in ("", ".")]
    return "/".join(parts) if parts else None


def _wanted(rel: str) -> bool:
    return rel in _EXPORT_TOP or (rel.split("/")[0] in _EXPORT_DIRS + _EXPORT_LOG_DIRS and "/" in rel) or re.fullmatch(r"chat[^/]*\.json", rel) is not None


def _read_zip(path: Path) -> tuple[dict, dict[str, bytes]]:
    import zipfile
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BadExport(f"not a valid export file: {e}")
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BadExport("too many files in the export")
        total, out = 0, {}
        for zi in infos:
            rel = _safe_rel(zi.filename)  # checks every entry, wanted or not
            if rel is None or zi.is_dir():
                continue
            if zi.file_size > MAX_FILE_BYTES:
                raise BadExport(f"{rel} is too large ({zi.file_size} bytes; limit {MAX_FILE_BYTES})")
            data = z.open(zi).read(MAX_FILE_BYTES + 1)  # the header size can lie
            if len(data) > MAX_FILE_BYTES:
                raise BadExport(f"{rel} is too large (limit {MAX_FILE_BYTES})")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise BadExport("the export is too large")
            out[rel] = data
    return _check_manifest(out)


def _check_manifest(files: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    if "manifest.json" not in files:
        raise BadExport("no manifest.json: this is not an AgentDesk export")
    try:
        m = json.loads(files.pop("manifest.json").decode("utf-8"))
    except ValueError:
        raise BadExport("manifest.json is not valid")
    if not isinstance(m, dict) or m.get("format") != EXPORT_FORMAT:
        raise BadExport(f"unknown export format version {m.get('format') if isinstance(m, dict) else m!r} (this app reads {EXPORT_FORMAT})")
    if "project.toml" not in files:
        raise BadExport("the export has no project.toml")
    return m, {k: v for k, v in files.items() if _wanted(k)}


def _read_manifest_name(d: Path) -> str:
    try:
        return str(json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("project", ""))
    except (ValueError, OSError):
        return ""


def find_export_dir(root: Path, project_name: str | None = None) -> Path:
    """The folder holding manifest.json inside a repo/folder: root, `.agentdesk/`, or one subfolder (config repo)."""
    subs = sorted(x for x in root.iterdir() if x.is_dir() and x.name != ".git")
    cands = [c for c in [root, *subs] if (c / "manifest.json").is_file()]
    if project_name:
        cands = [c for c in cands if _read_manifest_name(c) in (project_name,) or c.name == slug(project_name)] or cands
    if not cands:
        raise BadExport(f"no AgentDesk export (manifest.json) found in {root}")
    if len(cands) > 1:
        raise BadExport("several exports found there; say which project: " + ", ".join(c.name for c in cands))
    return cands[0]


def _read_folder(d: Path) -> tuple[dict, dict[str, bytes]]:
    files, total = {}, 0
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(d).as_posix()
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            raise BadExport(f"{rel} is too large (limit {MAX_FILE_BYTES})")
        total += size
        if total > MAX_TOTAL_BYTES or len(files) > MAX_ENTRIES:
            raise BadExport("the export is too large")
        files[rel] = f.read_bytes()
    return _check_manifest(files)


def _free_name(name: str) -> str:
    n = 2
    while (DATA / "projects" / slug(f"{name} {n}")).exists():
        n += 1
    return f"{name} {n}"


def clone_for_import(url: str, into: Path | None = None) -> Path:
    """Clone a config repo (git URL or local path) into a new folder and return it."""
    import tempfile
    if into is not None and into.exists() and any(into.iterdir()):
        raise BadExport(f"{into} already exists and is not empty")
    imports = DATA / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    dest = into or Path(tempfile.mkdtemp(prefix="agentdesk-import-", dir=imports))
    code, out = run(["git", "clone", "--", url, str(dest)], imports, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=600)
    if code != 0:
        raise BadExport(f"git clone failed: {out.strip()}")
    return dest


def import_project(src: Path | str, repo: str, new_name: str | None = None, account_name: str | None = None,
                   project_name: str | None = None, pull: bool = False) -> Project:
    """Create a project from a .zip, or from a folder / git repo holding an export (`.agentdesk/`, config repo).
    Never overwrites: if the name is taken, raises NameClash (give `new_name`). The repo path is chosen here (this
    machine); the account must be a local one or is left unset. A local git folder is pulled first if `pull`."""
    src = Path(src)
    if src.is_dir():
        if pull and (src / ".git").exists():
            code, out = run(["git", "pull", "--ff-only"], src, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=300)
            if code != 0:
                raise BadExport(f"git pull failed: {out.strip()}")
        sub = src / EXPORT_DIR
        manifest, files = _read_folder(sub if (sub / "manifest.json").is_file() else find_export_dir(src, project_name))
    elif src.is_file():
        manifest, files = _read_zip(src)
    else:
        raise BadExport(f"{src} does not exist")
    name = (new_name or "").strip() or str(manifest.get("project") or "").strip()
    if not name:
        raise BadExport("the export has no project name")
    s = slug(name)
    d = DATA / "projects" / s
    if d.exists():
        raise NameClash(name, _free_name(name))
    if not Path(repo).is_dir():
        raise ValueError(f"{repo} is not a folder")
    acct = account_name if account_name and any(a.get("name") == account_name for a in settings().get("accounts", [])) else None
    try:
        toml = _localize_toml(files["project.toml"].decode("utf-8"), name, repo, str(d / "worktrees"), acct)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise BadExport(f"project.toml in the export is not valid: {e}")
    d.mkdir(parents=True)  # exist_ok=False: never overwrite
    try:
        for rel, data in files.items():
            if rel == "project.toml":
                continue
            t = d / rel
            if d.resolve() not in t.resolve().parents:
                raise BadExport(f"unsafe path in the export: {rel!r}")
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(data)
        (d / "tasks").mkdir(exist_ok=True)
        (d / "project.toml").write_text(toml, encoding="utf-8")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return project(s)
# ----------------------------------------------------------------------------- build panel (user actions only)
# The command is never taken from a request: it is the built-in build_exe.ps1 of the checked-out source, or
# `[build] command` from the project's config. The build runs in its own worktree under <project>/build/src.

BUILD_KINDS = ("build", "clean", "custom")
BUILD_SCRIPT = "build_exe.ps1"
BUILD_RESULT = "dist/AgentDesk/AgentDesk.exe"


class BuildJob:
    def __init__(self, p: "Project"):
        self.p = p
        self.dir = p.dir / "build"
        self.wt = self.dir / "src"
        self.logf = self.dir / "build.log"
        self.statf = self.dir / "status.json"
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stopped = False
        self.thread: threading.Thread | None = None
        self.st: dict = {"status": "idle"}
        if self.statf.exists():
            try:
                self.st = json.loads(self.statf.read_text(encoding="utf-8"))
                if self.st.get("status") == "running":  # the dashboard died mid-build
                    self.st["status"] = "failed"
            except (OSError, ValueError):
                pass

    def running(self) -> bool:
        return self.st.get("status") == "running"

    def _set(self, **kw) -> None:
        self.st.update(kw)
        try:
            self.statf.write_text(json.dumps(self.st), encoding="utf-8")
        except OSError:
            pass

    def _say(self, msg: str) -> None:
        with self.logf.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    def command(self, kind: str) -> tuple[object, bool]:
        """(argv or shell string, use_shell). Only ever the built-in script or the project's own config."""
        if kind == "custom":
            cmd = str(self.p.cfg.get("build", {}).get("command", "")).strip()
            if not cmd:
                raise ValueError("no [build] command is set in this project's config")
            return cmd, True
        if sys.platform == "win32":
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BUILD_SCRIPT], False
        return ["pwsh", "-NoProfile", "-File", BUILD_SCRIPT], False

    def result_path(self) -> Path:
        return self.wt / self.p.cfg.get("build", {}).get("result", BUILD_RESULT)

    def start(self, kind: str, ref: str, blocked: str | None = None) -> str | None:
        """Returns an error message, or None when the build was started."""
        if kind not in BUILD_KINDS:
            return "unknown build kind"
        ref = (ref or "").strip() or self.p.cfg["repo"]["base"]
        if ref.startswith("-"):
            return "bad branch or commit"
        if blocked:
            return blocked
        with self.lock:
            if self.running():
                return "a build is already running"
            try:
                cmd, shell = self.command(kind)
            except ValueError as e:
                return str(e)
            code, sha = _g(self.p, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
            if code != 0:
                return f"'{ref}' is not a branch or commit of this repository"
            self.dir.mkdir(parents=True, exist_ok=True)
            self.logf.write_text("", encoding="utf-8")
            self.stopped = False
            self._set(status="running", kind=kind, ref=ref, commit=sha[:12], started=time.time(), ended=None, exit=None)
            self.thread = threading.Thread(target=self._work, args=(kind, sha, cmd, shell), daemon=True)
            self.thread.start()
        return None

    def _prepare(self, kind: str, sha: str) -> None:
        if kind == "clean" and self.wt.exists():
            self._say("== clean: removing the build worktree")
            git(["worktree", "remove", "--force", str(self.wt)], self.p.repo)
            shutil.rmtree(self.wt, ignore_errors=True)
        git(["worktree", "prune"], self.p.repo)
        if (self.wt / ".git").exists():
            code, out = git(["checkout", "--detach", "--force", sha], self.wt)
        else:
            shutil.rmtree(self.wt, ignore_errors=True)
            code, out = git(["worktree", "add", "--detach", "--force", str(self.wt), sha], self.p.repo)
        if code != 0:
            raise RuntimeError(f"could not prepare the build worktree: {out.strip()}")

    def _work(self, kind: str, sha: str, cmd, shell: bool) -> None:
        code = 1
        try:
            self._say(f"== {kind} of {self.st.get('ref')} ({sha[:12]}) in {self.wt}")
            self._prepare(kind, sha)
            if self.stopped:
                raise RuntimeError("stopped")
            if not shell and not (self.wt / BUILD_SCRIPT).exists():
                raise RuntimeError(f"{BUILD_SCRIPT} does not exist in that revision")
            kw: dict = {}
            if sys.platform == "win32":
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "AGENTDESK_HOME": scratch_home()}
            with self.logf.open("a", encoding="utf-8", errors="replace") as out:
                with self.lock:
                    if self.stopped:
                        raise RuntimeError("stopped")
                    self.proc = subprocess.Popen(cmd, cwd=self.wt, shell=shell, stdin=subprocess.DEVNULL, stdout=out,
                                                 stderr=subprocess.STDOUT, env=env, **kw)
                code = self.proc.wait()
            self._say(f"== exit code {code}")
        except Exception as e:  # noqa: BLE001
            self._say(f"== {'build stopped' if self.stopped else 'build could not run'}: {e}")
        self.proc = None
        self._set(status="stopped" if self.stopped else ("ok" if code == 0 else "failed"), ended=time.time(), exit=code)

    def stop(self) -> str | None:
        with self.lock:
            if not self.running():
                return "no build is running"
            self.stopped = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            kill_tree(proc)
        return None

    def info(self, tail: int = 20000) -> dict:
        st = dict(self.st)
        st["running"] = self.running()
        end = st.get("ended") or time.time()
        st["duration"] = round(end - st["started"], 1) if st.get("started") else None
        st["has_custom"] = bool(str(self.p.cfg.get("build", {}).get("command", "")).strip())
        st["base"] = self.p.cfg["repo"]["base"]
        st["result"] = str(self.result_path())
        st["result_exists"] = self.result_path().exists()
        st["log"] = ""
        if self.logf.exists():
            with self.logf.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - tail))
                st["log"] = f.read().decode("utf-8", errors="replace")
        return st

    def open_folder(self) -> str | None:
        r = self.result_path()
        folder = r.parent if r.parent.is_dir() else (self.wt if self.wt.is_dir() else None)
        if folder is None:
            return "there is no build output yet"
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
        return None


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process and all its descendants (PowerShell -> PyInstaller on Windows)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


BUILDS: dict[str, BuildJob] = {}
_BUILDS_LOCK = threading.Lock()


def build_job(p: "Project") -> BuildJob:
    with _BUILDS_LOCK:
        job = BUILDS.get(p.slug)
        if job is None:
            job = BUILDS[p.slug] = BuildJob(p)
        job.p = p
        return job
