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

from . import reliability

_SDK_NAMES = ("AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient", "PermissionResultAllow", "PermissionResultDeny",
              "RateLimitEvent", "ResultMessage", "TextBlock", "ToolResultBlock", "ToolUseBlock", "UserMessage", "query")
_sdk_lock = threading.Lock()


def _load_sdk() -> None:
    """Import claude_agent_sdk (slow: ~1s) on first use instead of at program start; its names become
    module globals, so tests can still patch them. Call before using any SDK name."""
    if "query" in globals():
        return
    with _sdk_lock:
        if "query" in globals():
            return
        import claude_agent_sdk
        _hide_sdk_console()
        for n in _SDK_NAMES:
            globals().setdefault(n, getattr(claude_agent_sdk, n))


def __getattr__(name: str):
    if name in _SDK_NAMES:
        _load_sdk()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
SCRATCH_NAME = "agentdesk-worker-home"


def _is_scratch_path(p: str) -> bool:
    """The workers' throw-away AGENTDESK_HOME (now <data>/.scratch/agentdesk-worker-home; formerly in the temp folder)."""
    try:
        return Path(p).name == SCRATCH_NAME
    except (OSError, ValueError):
        return False


def user_data_dir(platform: str = sys.platform, environ=os.environ) -> Path:
    """Where the packaged program keeps its data by default: %LOCALAPPDATA%\\AgentDesk on Windows,
    ~/Library/Application Support/AgentDesk on macOS, $XDG_DATA_HOME/AgentDesk (~/.local/share/AgentDesk) elsewhere."""
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AgentDesk"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AgentDesk"
    return Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "AgentDesk"


def _data_home() -> Path:
    """AGENTDESK_HOME, else `data` next to the app. The packaged program (often under Program Files, which
    users can't write to) uses `data` next to the .exe only if it already exists there, else user_data_dir()."""
    env = os.environ.get("AGENTDESK_HOME")
    # The installed program never uses the workers' scratch folder (see scratch_home) as its data: it is started from an
    # agent's shell now and then (an install, a restart) that carries that setting, and the app would show no projects.
    if env and not (FROZEN and _is_scratch_path(env)):
        return Path(env)
    if FROZEN and not (APP / "data").is_dir():
        return user_data_dir()
    return APP / "data"


DATA = _data_home()
TIER_ORDER = ["mechanical", "standard", "hard"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]   # the models' effort levels, weakest first

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

# Runs: up to max_parallel tasks of a project at once, each in its own worktree, in dependency (DAG) order, the task
# on the longest remaining chain first. gate_parallel: how many tasks run their checks at the same time (0 = all;
# 1 when the checks share a build folder anyway, e.g. one CARGO_TARGET_DIR). integrate_after = true: when a run ends
# with finished work, merge the finished branches into auto/integration and run the final checks there.
# A project can override any key in its project.toml under [run].
[run]
max_parallel = 8
gate_parallel = 0
integrate_after = false
# A run looks for newly ready work (a retry, an answer, a task added meanwhile) at least this often, not only when a
# running task ends.
recheck_seconds = 30

# Notifications: a short message when something needs a person or a run ends. Telegram: set the bot token in the
# environment variable named by telegram_token_env (never here) and the chat id; webhook_url: a JSON POST
# {project, kind, text}. Nothing is sent while neither is set.
[notify]
telegram_token_env = "AGENTDESK_TELEGRAM_TOKEN"
telegram_chat_id = ""
webhook_url = ""
events = ["needs_decision", "blocked", "paused", "run_finished", "integration"]

# Review: after a worker's attempt passes the checks, a reviewer compares the change with the task (goal, acceptance
# criteria) and sends it back with concrete fixes when the task isn't actually done (rounds = how often per tier).
[review]
enabled = true
model = "sonnet"
effort = "low"
max_turns = 20
budget_usd = 0.6
rounds = 1

# Architect: reads the repository and turns a goal into a dependency graph of tasks (a draft plan you approve).
# rounds: how many times it may fix the plan after the checks (cycles, files two parallel tasks both touch).
[architect]
model = "opus"
effort = "high"
max_turns = 60
budget_usd = 4.0
rounds = 3

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
max_effort = "high"       # before moving up a tier, a task that keeps failing gets one more round at this effort
max_turns = 80
budget_usd = 4.0
next = "hard"

[tiers.hard]
model = "opus"
effort = "high"
max_effort = "xhigh"
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
run_timeout_min = 720     # kill a run (and its git / claude children) that is older than this; 0 = never
min_free_gb = 1.0         # no run starts with less free disk space

# The Coworker asks the user (an approval card) only before these shell commands: irreversible or public actions.
# Everything else it does on its own. Regular expressions, matched case-insensitively against the command.
[chat.coworker_confirm]
patterns = [
  'git\\s+push', 'git\\s+remote\\s+(add|set-url|remove|rm|rename)', '(?-i:git\\s+branch\\s+(-D|--delete\\s+--force))',
  'git\\s+reset\\s+--hard', 'git\\s+tag\\s+(-d|--delete)',
  'gh\\s+(pr\\s+(create|merge|close|edit|comment|review)|release\\s+(create|delete|edit|upload)|repo\\s+(create|delete|edit|rename|archive)|issue\\s+(create|close|edit|comment|delete)|workflow\\s+(run|enable|disable))',
  'gh\\s+api\\s+.*(-X\\s*(POST|PUT|PATCH|DELETE)|--method\\s+(POST|PUT|PATCH|DELETE)|-f\\s|--field)',
  '(cargo|npm|pnpm|yarn)\\s+publish', 'twine\\s+upload', 'curl\\s+.*(-T\\b|--upload|-d\\b|--data|-X\\s*(POST|PUT|PATCH|DELETE))',
  'rm\\s+-[a-z]*r[a-z]*f?\\s+/(\\s|$)', 'systemctl\\s+(stop|restart|disable)', '\\b(shutdown|reboot)\\b',
]

# Planner: reads only and writes a draft plan of tasks; nothing runs before you approve it. budget_usd is per message.
[chat.planner]
model = "sonnet"
effort = "medium"
budget_usd = 2.0

# Disk hygiene: the dashboard measures each project's cargo target dirs, worktrees and data folder in the background
# and shows them (Disk section). auto = false: it only suggests; the user confirms each cleanup. A project can override
# any key in its project.toml under [hygiene]. Sizes: max_target_gb per target dir, max_total_gb for the whole project.
# In-app updater: checks GitHub Releases once at start (in the background) and shows an "Update" button.
[updates]
check = true

[hygiene]
enabled = true
auto = false
max_target_gb = 60
max_total_gb = 150
stale_days = 3
max_runs_mb = 200
max_log_mb = 20
interval_min = 30

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


# ----------------------------------------------------------------------------- in-app updater

UPDATE_API = "https://api.github.com/repos/janmaslovitz/agentdesk/releases/latest"
RELEASES_PAGE = "https://github.com/janmaslovitz/agentdesk/releases/latest"  # where macOS / Linux users download updates
UPDATE_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
UPDATE_TTL = 3600.0
_UPDATE: dict = {"at": 0.0, "info": None}
_UPDATE_LOCK = threading.Lock()


def parse_version(v) -> tuple | None:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); None when it isn't numeric dotted."""
    m = re.fullmatch(r"\s*[vV]?(\d+(?:\.\d+)*)\s*", str(v or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _vcmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def update_url_ok(url: str) -> bool:
    import urllib.parse
    try:
        u = urllib.parse.urlsplit(str(url))
        return u.scheme == "https" and (u.hostname or "").lower() in UPDATE_HOSTS and u.port in (None, 443)
    except ValueError:
        return False


def pick_installer_asset(assets) -> dict | None:
    """The AgentDesk-Setup*.exe asset (with a https download url), or None."""
    for a in assets or []:
        n = str(a.get("name", "")) if isinstance(a, dict) else ""
        if re.fullmatch(r"AgentDesk-Setup.*\.exe", n, re.I) and update_url_ok(a.get("browser_download_url", "")):
            return a
    return None


def update_from_release(rel: dict, current: str, installer: bool | None = None) -> dict:
    """The check result for a GitHub release JSON. status: ok / unknown. installer (default: on Windows): an update
    counts only when the release has the Setup .exe; otherwise (macOS / Linux) a newer version is enough, and the
    dashboard links to `page`, the release's web page, for a manual download."""
    if installer is None:
        installer = os.name == "nt"
    latest = parse_version(rel.get("tag_name"))
    cur = parse_version(current)
    page = rel.get("html_url") if update_url_ok(rel.get("html_url", "")) else RELEASES_PAGE
    if latest is None or cur is None:
        return {"status": "unknown", "current": current, "latest": None, "available": False, "notes": "", "asset": None, "page": RELEASES_PAGE}
    asset = pick_installer_asset(rel.get("assets"))
    sha = None
    if asset:
        d = str(asset.get("digest") or "")
        if d.startswith("sha256:"):
            sha = d[7:].lower()
        else:
            for a in rel.get("assets") or []:
                if a.get("name") == asset["name"] + ".sha256" and update_url_ok(a.get("browser_download_url", "")):
                    sha = {"url": a["browser_download_url"]}
    return {"status": "ok", "current": current, "latest": ".".join(map(str, latest)), "page": page,
            "available": _vcmp(latest, cur) > 0 and (asset is not None or not installer),
            "notes": str(rel.get("body") or "")[:4000],
            "asset": {"name": asset["name"], "url": asset["browser_download_url"], "size": int(asset.get("size") or 0), "sha256": sha} if asset else None}


def check_update(force: bool = False) -> dict:
    """Latest GitHub release vs this version. Cached ~1h; never raises (offline/any failure: status "unknown")."""
    from . import __version__
    with _UPDATE_LOCK:
        if not force and _UPDATE["info"] is not None and time.time() - _UPDATE["at"] < UPDATE_TTL:
            return dict(_UPDATE["info"])
    try:
        import urllib.request
        req = urllib.request.Request(UPDATE_API, headers={"User-Agent": f"AgentDesk/{__version__}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.loads(r.read(2_000_000).decode("utf-8"))
        info = update_from_release(rel, __version__)
    except Exception:
        info = {"status": "unknown", "current": __version__, "latest": None, "available": False, "notes": "", "asset": None, "page": RELEASES_PAGE}
    with _UPDATE_LOCK:
        _UPDATE["at"], _UPDATE["info"] = time.time(), info
    return dict(info)


def download_update(info: dict | None = None, progress=None) -> Path:
    """Stream the installer to a fresh temp dir; verify size and sha256 (when published). Raises on any problem."""
    import hashlib, tempfile, urllib.request
    from . import __version__
    info = info or check_update()
    a = info.get("asset")
    if not a:
        raise RuntimeError("no installer to download")
    if not update_url_ok(a["url"]):
        raise RuntimeError("download address is not allowed")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", a["name"])
    want = a.get("sha256")
    if isinstance(want, dict):
        if not update_url_ok(want["url"]):
            raise RuntimeError("checksum address is not allowed")
        with urllib.request.urlopen(urllib.request.Request(want["url"], headers={"User-Agent": f"AgentDesk/{__version__}"}), timeout=15) as r:
            m = re.search(r"[0-9a-fA-F]{64}", r.read(4096).decode("utf-8", "replace"))
        want = m.group(0).lower() if m else None

    class _Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not update_url_ok(newurl):
                raise RuntimeError("redirected to a disallowed address")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    dest = Path(tempfile.mkdtemp(prefix="agentdesk-update-")) / name
    h, n = hashlib.sha256(), 0
    total = int(a.get("size") or 0)
    opener = urllib.request.build_opener(_Guard)
    with opener.open(urllib.request.Request(a["url"], headers={"User-Agent": f"AgentDesk/{__version__}"}), timeout=30) as r, dest.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            n += len(chunk)
            if progress:
                progress(n, total)
    if total and n != total:
        raise RuntimeError(f"download incomplete ({n} of {total} bytes)")
    if want and h.hexdigest() != want:
        raise RuntimeError("checksum mismatch; the download was discarded")
    return dest


def record_version() -> tuple[str | None, str]:
    """Remember which version last ran on this data folder (data/app_version.json). Returns (previous version or None
    when unchanged/first run, current version); the caller logs an upgrade."""
    v = version_info()
    f = DATA / "app_version.json"
    try:
        prev = json.loads(f.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):  # (a corrupt record is just replaced below)
        # no record yet: data that already has a log came from a build before versions were recorded
        prev = "an earlier build" if (DATA / "agentdesk.log").exists() else None
    if prev == v["version"]:
        return None, v["version"]
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        write_json(f, {**v, "previous": prev, "since": dt.datetime.now().isoformat(timespec="seconds")}, indent=2)
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
    if os.name != "nt" and p.exists() and not os.access(p, os.X_OK):  # a packed build may lose the executable bit
        try:
            p.chmod(p.stat().st_mode | 0o111)
        except OSError:
            pass
    return str(p) if p.exists() else (shutil.which("claude") or "claude")


def account_usage(name: str) -> dict:
    data = read_json(DATA / "accounts" / f"{name}.json", {})
    return data if isinstance(data, dict) else {}


def save_account_usage(name: str, usage: dict) -> None:
    write_json(DATA / "accounts" / f"{name}.json", usage, indent=2)


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


# ----------------------------------------------------------------------------- folders of imported projects
# The user never types a folder: it is derived from the source (project name, repo name, archive name).

_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])")


def folder_slug(name: str) -> str:
    """A safe folder name: letters, digits, - _ . only; no leading dots/dashes, no '..', no Windows device names."""
    s = re.sub(r"[^\w.-]+", "-", str(name or "").replace(chr(0), ""), flags=re.UNICODE)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"-{2,}", "-", s).lstrip(".-")[:64].rstrip(".- ")
    if _RESERVED.fullmatch(s.split(".")[0]):
        s = "_" + s
    return s or "project"


def repo_name_from_url(url: str) -> str:
    """`https://github.com/you/My_Repo.git` / `git@host:you/repo.git` / a local path -> the repo name ('' if none)."""
    u = str(url or "").strip().split("?")[0].split("#")[0].rstrip("/\\")
    last = re.split(r"[/\:]", u)[-1] if u else ""
    return re.sub(r"\.git$", "", last, flags=re.I)


def archive_name(filename: str) -> str:
    """`my-project-1.2.zip` -> `my-project`: the file name without extension and a trailing version / timestamp / ` (1)`."""
    stem = re.split(r"[/\\]", str(filename or "").strip())[-1]
    stem = re.sub(r"\.zip$", "", stem, flags=re.I).strip()
    while True:
        t = re.sub(r"\s*\(\d+\)$", "", stem)
        t = re.sub(r"[-_ ]v?\d+(\.\d+)+$", "", t)
        t = re.sub(r"[-_ ]\d{8}(?:[-_T ]?\d{4,6})?$", "", t).strip()
        if t == stem or not t:
            return stem
        stem = t


def import_folder_name(new_name: str = "", url: str = "", filename: str = "", export_name: str = "") -> str:
    """The folder name for an import: typed project name, else repo name, else the export's own name, else the file name."""
    return folder_slug((new_name or "").strip() or repo_name_from_url(url) or (export_name or "").strip()
                       or archive_name(filename))


def import_root() -> Path:
    return DATA / "repos"


def preview_import_dir(name: str, root: Path | None = None) -> Path:
    """Where `make_import_dir` would put it now (first free `name`, `name-2`, ...), without creating anything."""
    root = root or import_root()
    n, dest = 1, root / name
    while dest.exists():
        n += 1
        dest = root / f"{name}-{n}"
    return dest


def make_import_dir(name: str, root: Path | None = None) -> Path:
    """Atomically create a fresh folder `<root>/<name>` (or `-2`, `-3`, ...) and return it. Never reuses a folder;
    refuses a result outside `root` (symlinks included)."""
    root = root or import_root()
    root.mkdir(parents=True, exist_ok=True)
    real = root.resolve()
    name = folder_slug(name)
    n = 1
    while True:
        dest = root / (name if n == 1 else f"{name}-{n}")
        try:
            dest.mkdir()
        except FileExistsError:
            n += 1
            continue
        if dest.resolve().parent != real:
            try:
                dest.rmdir()
            except OSError:
                pass
            raise ValueError(f"{dest} is outside the allowed folder {root}")
        return dest



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

    def mcp(self, log=None) -> dict[str, dict]:
        """Valid `[mcp.<name>]` entries of project.toml: command (str), args (list of str), env (str -> str),
        enabled (bool, default false), roles (subset of worker/router, default ["worker"]). Invalid entries are
        skipped (one line to `log`); a bad config never breaks a run."""
        raw = self.cfg.get("mcp")
        out: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return out

        def bad(name: str, why: str) -> None:
            if log:
                log(f"mcp.{name}: skipped ({why})")

        for name, c in raw.items():
            if not isinstance(c, dict):
                bad(name, "not a table")
            elif not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)):
                bad(name, "bad name")
            elif not isinstance(c.get("command"), str) or not c["command"].strip():
                bad(name, "command must be a non-empty string")
            elif not isinstance(c.get("enabled", False), bool):
                bad(name, "enabled must be true/false")
            elif not isinstance(c.get("args", []), list) or not all(isinstance(a, str) for a in c.get("args", [])):
                bad(name, "args must be a list of strings")
            elif not isinstance(c.get("env", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in c.get("env", {}).items()):
                bad(name, "env must map strings to strings")
            elif (not isinstance(c.get("roles", ["worker"]), list)
                  or not all(r in ("worker", "router") for r in c.get("roles", ["worker"]))):
                bad(name, "roles must be a list of worker/router")
            elif c.get("enabled", False):
                out[name] = {"command": c["command"].strip(), "args": list(c.get("args", [])), "env": dict(c.get("env", {})),
                             "roles": list(c.get("roles", ["worker"]))}
        return out


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
    """Write `text` so that `target` is always either the old or the new content, also after a crash, a kill or a power
    cut: the data is flushed to disk before the rename. (A plain write_text can leave a file of zero bytes on Windows
    when the process dies mid-write, which blanked the whole dashboard once.)"""
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    replace_file(tmp, target)


def write_json(target: Path, obj, **kw) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(target, json.dumps(obj, **kw))


def quarantine(path: Path, why: str = "unreadable") -> Path | None:
    """Move a corrupt data file out of the way (kept for inspection as `<name>.corrupt-<time>`) and say so in the log."""
    dest = path.with_name(f"{path.name}.corrupt-{dt.datetime.now():%Y%m%d-%H%M%S}")
    try:
        path.replace(dest)
    except OSError:
        return None
    try:
        with (DATA / "agentdesk.log").open("a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%m-%d %H:%M:%S}] data file {path.name} was {why}; moved aside as {dest.name}\n")
    except OSError:
        pass
    return dest


def read_json(path: Path, default, backup: Path | None = None):
    """The parsed JSON file, or `default` when it is missing. A file that can't be parsed (empty, zero-filled after a
    crash, half written) is set aside with quarantine() and replaced by `backup` when that parses, else `default`:
    one bad data file must never make the dashboard fail."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except (OSError, ValueError):  # unreadable or not UTF-8 (a zero-filled file reads fine; this is the rest)
        text = None
    if text is not None:
        try:
            return json.loads(text)
        except ValueError:
            pass
    quarantine(path, "corrupt")
    if backup is not None:
        try:
            data = json.loads(backup.read_text(encoding="utf-8"))
            write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))
            return data
        except (OSError, ValueError):
            pass
    return default


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
    `[chat] model`, then CHAT_DEFAULT_MODELS (assistant: haiku, coworker and planner: sonnet)."""
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


def coworker_confirm_reason(cmd: str) -> str | None:
    """The `[chat.coworker_confirm]` pattern a shell command matches (an action the Coworker must have the user
    approve), or None."""
    for pat in settings().get("chat", {}).get("coworker_confirm", {}).get("patterns", []):
        try:
            if re.search(pat, cmd or "", re.I):
                return pat
        except re.error:
            continue
    return None


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


def valid_branch(name: str) -> bool:
    """A name git accepts for a new branch (and that can't be read as an option)."""
    if not name or name.startswith("-"):
        return False
    DATA.mkdir(parents=True, exist_ok=True)
    return git(["check-ref-format", "--branch", name], DATA)[0] == 0


def create_new_project(name: str, base: str = "main", preset: str = "none", account_name: str = "default") -> Project:
    """A brand-new project with nothing to start from: a fresh code folder under <data>/repos named after the project
    (as for imports), `git init` on `base` and a first commit (.gitignore, README.md). Local only: no remote, no push.
    The folder is removed again if the project can't be made."""
    name, base = (name or "").strip(), (base or "").strip() or "main"
    if not name:
        raise ValueError("enter a project name")
    if (DATA / "projects" / slug(name) / "project.toml").exists():
        raise ValueError(f"a project named {slug(name)} exists")
    if not valid_branch(base):
        raise ValueError(f"'{base}' is not a valid branch name")
    repo = make_import_dir(import_folder_name(name))
    try:
        code, out = git(["init"], repo)
        if code != 0:
            raise RuntimeError(f"git init failed: {out}")
        git(["symbolic-ref", "HEAD", f"refs/heads/{base}"], repo)  # `git init -b` needs git 2.28
        (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        base = setup_local_git(str(repo)) or base
        p = add_project(name, str(repo), base, preset, account_name)
    except BaseException:
        shutil.rmtree(repo, ignore_errors=True)
        raise
    return p


# ----------------------------------------------------------------------------- per-project state

_LOG_LOCK = threading.Lock()  # parallel tasks log from worker threads (checks, git) and the event loop at once


class Store:
    def __init__(self, p: Project):
        self.p = p
        # In a run process: the ids of the tasks this run is working on right now. save() then writes only those
        # tasks (and the run's totals) from memory and takes every other task from disk, into memory too, so a change
        # made meanwhile by the dashboard, the chat or the API (an answer, a retry) is neither lost nor overwritten.
        self.owned: set[str] | None = None

    def log(self, msg: str) -> None:
        line = f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}"
        with _LOG_LOCK:
            print(line, flush=True)
            lf = self.p.path("agentdesk.log")
            reliability.rotate(lf)
            with lf.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def load(self) -> dict:
        s = self.p.path("state.json")
        data = read_json(s, {"tasks": {}, "spent_usd": 0.0}, backup=self.p.path("state.json.bak"))
        return data if isinstance(data, dict) and isinstance(data.get("tasks"), dict) else {"tasks": {}, "spent_usd": 0.0}

    def save(self, state: dict) -> None:
        if self.owned is not None:
            disk = self.load()
            for tid, e in disk.get("tasks", {}).items():
                if tid not in self.owned:
                    state["tasks"][tid] = e
        s, bak = self.p.path("state.json"), self.p.path("state.json.bak")
        try:  # the previous good state is kept as a backup (at most once a minute: this is saved very often)
            if s.exists() and s.stat().st_size > 2 and time.time() - (bak.stat().st_mtime if bak.exists() else 0) > 60:
                shutil.copyfile(s, bak)
        except OSError:
            pass
        write_atomic(s, json.dumps(state, indent=2, ensure_ascii=False))

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
    files: list[str] = field(default_factory=list)   # what it touches (optional): the graph checks overlaps with it
    effort: str | None = None                          # low | medium | high (optional; else the router or the tier decides)


_TASK_SKIP = object()  # cached verdict for a file that isn't a runnable task (no header, draft, invalid)
_task_cache: dict[Path, tuple[tuple[int, int], object]] = {}
_task_cache_lock = threading.Lock()


def _parse_task(f: Path):
    m = re.match(r"\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)", f.read_text(encoding="utf-8"), re.S)
    if not m:
        return _TASK_SKIP
    try:
        h = tomllib.loads(m.group(1))
        if h.get("status") == "draft":
            return _TASK_SKIP
        return Task(str(h["id"]), h["title"], m.group(2).strip(), h.get("tier"), [str(d) for d in h.get("depends", [])],
                    list(h.get("crates", [])), [str(x) for x in h.get("files", [])],
                    h.get("effort") if h.get("effort") in EFFORTS else None)
    except (tomllib.TOMLDecodeError, KeyError):
        return _TASK_SKIP


def load_tasks(p: Project) -> list[Task]:
    """The queue: `tasks/*.md` only. Draft plans live in `tasks/drafts/` (not globbed) and carry
    `status = "draft"` in their header, which is skipped too wherever such a file ends up.
    Files are re-parsed only when their (mtime, size) changed; callers get copies they may mutate."""
    tasks = []
    seen = set()
    with _task_cache_lock:
        for f in sorted(p.tasks_dir.glob("*.md")):
            seen.add(f)
            st = f.stat()
            key = (st.st_mtime_ns, st.st_size)
            hit = _task_cache.get(f)
            if hit is None or hit[0] != key:
                hit = _task_cache[f] = (key, _parse_task(f))
            t = hit[1]
            if t is not _TASK_SKIP:
                tasks.append(replace(t, depends=list(t.depends), crates=list(t.crates), files=list(t.files)))
        for f in [f for f in _task_cache if f.parent == p.tasks_dir and f not in seen]:
            del _task_cache[f]
    return tasks


def add_task(p: Project, task_id: str, title: str, body: str, tier: str | None = None, depends: list[str] | None = None,
             files: list[str] | None = None, effort: str | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task_id) or task_id in (COWORK,):
        raise ValueError("task ids use letters, digits, - and _ (and start with a letter or digit)")
    if tier and tier not in TIER_ORDER:
        raise ValueError(f"tier is one of {', '.join(TIER_ORDER)} (or empty: the router decides)")
    head = [f"id = {toml_str(task_id)}", f"title = {toml_str(title)}"]
    if effort and effort not in EFFORTS:
        raise ValueError(f"effort is one of {', '.join(EFFORTS)} (or empty: the router or the tier decides)")
    if tier:
        head.append(f"tier = {toml_str(tier)}")
    if effort:
        head.append(f"effort = {toml_str(effort)}")
    if depends:
        head.append("depends = [" + ", ".join(toml_str(d) for d in depends) + "]")
    if files:
        head.append("files = [" + ", ".join(toml_str(str(x)) for x in files) + "]")
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
        effort = t.get("effort") or None
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"{key}: effort is one of {', '.join(EFFORTS)} (or empty)")
        deps = list(dict.fromkeys(str(d).strip() for d in t.get("depends") or [] if str(d).strip()))
        for d in deps:
            if d == key or (d not in keys and d not in queued):
                raise ValueError(f"{key}: depends on {d!r}, which is neither another task of this plan (d1..d{len(keys)}) nor a queued task")
        out.append({"key": key, "title": title[:200], "goal": str(t.get("goal") or "").strip(), "body": str(t.get("body") or "").strip(),
                    "files": [str(f) for f in t.get("files") or []][:30], "tier": tier, "effort": effort, "depends": deps})
    plan_order(out)  # refuses cycles
    return out


def plan_nodes(tasks: list[dict]):
    from . import dag
    return [dag.Node(t["key"], list(t["depends"]), list(t.get("files") or []), t.get("tier"), t["title"]) for t in tasks]


def plan_warnings(tasks: list[dict]) -> list[str]:
    """What is risky (not wrong) about a checked plan: tasks that may run at the same time but touch the same files
    (their branches will likely conflict: order them with depends, or merge them into one task)."""
    from . import dag
    nodes = plan_nodes(tasks)
    out = [f"{a} and {b} may run in parallel but both touch {', '.join(f[:5])}: make one depend on the other, or merge them"
           for a, b, f in dag.concurrent_conflicts(nodes)]
    no_files = [t["key"] for t in tasks if not t.get("files")]
    if no_files and len(tasks) > 1:
        out.append(f"no files listed for {', '.join(no_files[:10])}: overlaps with parallel tasks can't be checked")
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
        if t.get("effort"):
            head.append(f"effort = {toml_str(t['effort'])}")
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


def now_iso() -> str:
    """A fine-grained timestamp for ordering events within a run (a task started vs. put back in the queue)."""
    return dt.datetime.now().isoformat(timespec="microseconds")


def propose_plan(p: Project, summary: str, tasks) -> dict:
    with PLAN_LOCK:
        old = load_plan(p)
        if old and old.get("status") == "draft":
            raise ValueError("there is a draft plan already: change it with update_plan, or cancel_plan first")
        if plan_active(old):
            raise ValueError("an approved plan is still going (one approved plan per project at a time); "
                             "it has to finish, or the user closes it on the plan card")
        norm = check_plan_tasks(p, tasks)
        plan = {"status": "draft", "summary": str(summary or "").strip()[:500], "tasks": norm, "version": 1, "created": _now(),
                "warnings": plan_warnings(norm)}
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
        plan.update(tasks=norm, version=int(plan.get("version", 1)) + 1, updated=_now(), warnings=plan_warnings(norm))
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
            add_task(p, ids[t["key"]], t["title"], plan_task_body(t), t["tier"], [ids.get(d, d) for d in t["depends"]], t["files"],
                     t.get("effort"))
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


def plan_budget(p: Project, plan: dict | None = None) -> float:
    """The run budget for an approved plan: its estimate's upper end, never below [limits] run_budget_usd."""
    plan = plan or load_plan(p) or {}
    base = float(settings()["limits"]["run_budget_usd"])
    try:
        return round(max(base, plan_estimate(plan.get("tasks", []))[1]), 2)
    except Exception:
        return base


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
    from . import dag
    try:
        lv = dag.levels(plan_nodes(plan["tasks"]))
        width = dag.width(plan_nodes(plan["tasks"]))
    except dag.GraphError:
        lv, width = {}, None
    for r, t in zip(rows, plan_order(plan["tasks"])):
        r["level"] = lv.get(t["key"])
    return {"status": plan["status"], "summary": plan.get("summary", ""), "version": plan.get("version", 1),
            "tasks": rows, "estimate": [lo, hi], "warnings": plan.get("warnings", []), "width": width,
            "by": plan.get("by", "planner"), "goal": plan.get("goal", "")}


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
    (or a stray `stop`) can never touch the real data folder, pid files or settings. It lives in the data folder,
    not in the system temp folder: a service's private /tmp disappears when the service restarts, while its runs go
    on (and then couldn't start a single check)."""
    d = DATA / ".scratch" / SCRATCH_NAME
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def work_tmp() -> str:
    """TMPDIR for runs, workers and checks: in the data folder, so it outlives a restart of the service (see above)."""
    d = DATA / ".scratch" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def use_stable_tmp() -> None:
    """For a run / integration / architect process: temp files go to work_tmp() for this process and its children."""
    import tempfile
    t = work_tmp()
    os.environ["TMPDIR"] = os.environ["TEMP"] = os.environ["TMP"] = t
    tempfile.tempdir = None  # re-read on next use


ENV_FAILURE = "COULD NOT RUN (the environment, not the task)"


def run(cmd, cwd: Path, env: dict | None = None, timeout: int = 3600, shell: bool = False) -> tuple[int, str]:
    """Run a command; (exit code, output). A command that couldn't even start (a folder it needs vanished) is tried
    once more after recreating the scratch folders; if it still can't, code 127 and the error."""
    last = ""
    for attempt in range(2):
        try:
            full = {**os.environ, "AGENTDESK_HOME": scratch_home(), "TMPDIR": work_tmp(), **(env or {})}
            r = subprocess.run(cmd, cwd=cwd, env=full, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, shell=shell,
                               stdin=subprocess.DEVNULL, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired as e:
            return 124, f"timed out after {timeout}s\n{str(e.stdout or '')[-2000:]}"
        except OSError as e:
            last = str(e)
            if attempt == 0:
                time.sleep(1)
    return 127, last


def environment_failure(code: int, out: str) -> bool:
    """A check that failed because it couldn't run at all (exit 127: the command or a folder it needs is missing),
    as opposed to a check that ran and found a problem in the task's work."""
    if code != 127:
        return False
    o = (out or "").strip()
    return o.startswith("[Errno") or bool(re.search(r"(^|: )(command )?not found\b|No such file or directory", o[:500]))


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    return run(["git", *args], cwd)


# ----------------------------------------------------------------------------- git panel (user actions only)

def _g(p: "Project", *args: str, timeout: int = 120, env: dict | None = None) -> tuple[int, str]:
    code, out = run(["git", *args], p.repo, env={"GIT_TERMINAL_PROMPT": "0", **(env or {})}, timeout=timeout)
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
          "remote": remote[0] if remote else None, "remote_url": gh_redact(remote[1]) if remote else None,
          "upstream": None, "ahead": 0, "behind": 0, "autos": [], "account": git_account(p)}
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
    remote = git_remote(p)
    if remote is None:
        return {"ok": False, "message": "local-only project: there is no remote to fetch from"}
    cfg, env, who, err = _git_auth(p, remote[1])
    if err:
        return _as({"ok": False, "message": err}, who)
    code, out = _g(p, *cfg, "fetch", "--prune", env=env)
    return _as({"ok": code == 0, "message": out or "Fetched. Nothing new." if code == 0 else out}, who, env)


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
    remote = git_remote(p)
    if remote is None:
        return {"ok": False, "message": "local-only project: there is no remote to pull from"}
    cfg, env, who, err = _git_auth(p, remote[1])
    if err:
        return _as({"ok": False, "message": err}, who)
    code, out = _g(p, *cfg, "pull", "--ff-only" if ff_only else "--no-rebase", timeout=300, env=env)
    if code != 0 and ff_only and "fast-forward" in out.lower():
        out += "\nThe branches have diverged, so a fast-forward is not possible. Nothing was changed."
    return _as({"ok": code == 0, "message": out}, who, env)


def git_push(p: "Project", branch: str | None = None) -> dict:
    """Push the current or base branch to the remote, setting it as the upstream (-u). Never forced."""
    remote = git_remote(p)
    if remote is None:
        return {"ok": False, "message": "local-only project: there is no remote to push to"}
    _, cur = _g(p, "rev-parse", "--abbrev-ref", "HEAD")
    base = p.cfg["repo"]["base"]
    branch = branch or cur
    if branch not in (cur, base):
        return {"ok": False, "message": f"only the current branch '{cur}' or the base branch '{base}' can be pushed"}
    cfg, env, who, err = _git_auth(p, remote[1])
    if err:
        return _as({"ok": False, "message": err}, who)
    code, out = _g(p, *cfg, "push", "-u", remote[0], f"refs/heads/{branch}:refs/heads/{branch}", timeout=300, env=env)
    return _as({"ok": code == 0, "message": out or f"Pushed '{branch}' to {remote[0]}."}, who, env)


# ----------------------------------------------------------------------------- GitHub (gh CLI) for the git panel
# Accounts, repositories and pull requests come from the `gh` command-line tool. A project may name one gh account
# (project.toml `[git] account = "login"`); push/pull/fetch and gh calls then run as that account by giving the child
# process its token in the environment (from `gh auth token`), never on a command line, in a file, a log or a reply.
# Without an account, git uses its own credentials and gh its active account, as before.

GH_TTL = 5.0  # seconds a gh answer (accounts, repos, PRs, checks) is reused; Refresh asks again
_GH_CACHE: dict[tuple, tuple[float, object]] = {}
_GH_LOCK = threading.Lock()
GH_MISSING = ("The GitHub CLI (gh) is not installed or not on PATH. Install it from https://cli.github.com/, "
              "then run `gh auth login` in a terminal and press Refresh.")
GH_LOGIN = "gh is not logged in to GitHub. Run `gh auth login` in a terminal, then press Refresh."
_GH_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")  # GitHub logins and orgs (enterprise ones may have _)
_GH_REPO = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?")
_PATH_SEG = r"[A-Za-z0-9_.~%+-]+"
_REMOTE_BAD = re.compile(r"[\s;&|`$<>(){}'\"\\!*?\[\]^,]")
_TOKEN_SHAPE = re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}")
_CHECK_OK = {"SUCCESS", "NEUTRAL"}
_CHECK_SKIP = {"SKIPPED"}


def gh_redact(text: str, *secrets: str) -> str:
    """Text safe to show: the given secrets, anything shaped like a token and passwords inside URLs are masked."""
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    text = _TOKEN_SHAPE.sub("[redacted]", _SECRET_TEXT.sub("[redacted]", text))
    return re.sub(r"(://)[^/@\s:]*:[^/@\s]*@", r"\1[redacted]@", text)


def valid_login(name: str) -> bool:
    return bool(name) and _GH_OWNER.fullmatch(name) is not None


def _gh(args: list[str], cwd: Path | None = None, token: str | None = None, host: str | None = None,
        timeout: int = 30, raw: bool = False) -> tuple[int, str]:
    """Run gh windowless with a timeout, never prompting. `token` goes to the child's environment only.
    The output is redacted unless `raw` (only for reading a token)."""
    exe = shutil.which("gh")
    if not exe:
        return 127, GH_MISSING
    env = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GH_SPINNER_DISABLED": "1",
           "CLICOLOR": "0", "GIT_TERMINAL_PROMPT": "0"}
    if token:
        env["GH_TOKEN"] = token
    if host and host != "github.com":
        env["GH_HOST"] = host
    code, out = run([exe, *args], cwd or Path.home(), env=env, timeout=timeout)
    return code, out.strip() if raw else gh_redact(out, token or "").strip()


def _gh_cached(key: tuple, load, refresh: bool = False):
    with _GH_LOCK:
        hit = _GH_CACHE.get(key)
    if hit is not None and not refresh and time.time() - hit[0] < GH_TTL:
        return hit[1]
    val = load()
    with _GH_LOCK:
        _GH_CACHE[key] = (time.time(), val)
    return val


def _gh_forget(kind: str | None = None) -> None:
    with _GH_LOCK:
        for k in [k for k in _GH_CACHE if kind is None or k[0] == kind]:
            del _GH_CACHE[k]


def _json_or_none(out: str):
    i = min([j for j in (out.find("{"), out.find("[")) if j >= 0], default=-1)
    if i < 0:
        return None
    try:
        return json.loads(out[i:])
    except ValueError:
        return None


def parse_gh_accounts(out: str) -> list[dict]:
    """Accounts from `gh auth status --json hosts`, or from the text of `gh auth status` (older gh):
    [{host, login, active, ok, protocol, error}]."""
    rows: list[dict] = []
    data = _json_or_none(out)
    if isinstance(data, dict) and isinstance(data.get("hosts"), dict):
        for host, accts in data["hosts"].items():
            for a in accts if isinstance(accts, list) else []:
                if isinstance(a, dict) and a.get("login"):
                    rows.append({"host": str(a.get("host") or host), "login": str(a["login"]), "active": a.get("active") is True,
                                 "ok": str(a.get("state") or "success") == "success",
                                 "protocol": str(a.get("gitProtocol") or "https"), "error": gh_redact(str(a.get("error") or ""))})
        return rows
    for line in out.splitlines():
        s = line.strip()
        m = re.search(r"(Logged in|Failed to log in) to (\S+) (?:account|as) ([A-Za-z0-9_-]+)", s)
        if m:
            rows.append({"host": m.group(2), "login": m.group(3), "active": False, "ok": m.group(1) == "Logged in",
                         "protocol": "https", "error": "" if m.group(1) == "Logged in" else gh_redact(s)})
            continue
        m = re.match(r"- Active account: (true|false)", s)
        if m and rows:
            rows[-1]["active"] = m.group(1) == "true"
        m = re.match(r"- Git operations protocol: (\S+)", s)
        if m and rows:
            rows[-1]["protocol"] = m.group(1)
    for host in {r["host"] for r in rows}:  # a gh that predates several accounts: its one account is the active one
        same = [r for r in rows if r["host"] == host]
        if not any(r["active"] for r in same):
            same[0]["active"] = True
    return rows


def gh_accounts(refresh: bool = False) -> dict:
    """{ok, gh (installed), message, accounts}. Never raises; a missing or logged-out gh is a message."""
    def load() -> dict:
        code, out = _gh(["auth", "status", "--json", "hosts"], timeout=20)
        if code == 127 and out == GH_MISSING:
            return {"ok": False, "gh": False, "message": GH_MISSING, "accounts": []}
        rows = parse_gh_accounts(out)
        if not rows and "unknown flag" in out:  # an older gh has no --json here
            code, out = _gh(["auth", "status"], timeout=20)
            rows = parse_gh_accounts(out)
        if not rows:
            detail = out if out and _json_or_none(out) is None else ""
            return {"ok": False, "gh": True, "message": GH_LOGIN + (f"\n{detail}" if detail else ""), "accounts": []}
        return {"ok": True, "gh": True, "message": "", "accounts": rows}
    return _gh_cached(("accounts",), load, refresh)


def gh_switch_cmd(login: str, host: str = "github.com") -> list[str]:
    """The gh arguments that make `login` gh's active account on `host`."""
    if not valid_login(login):
        raise ValueError(f"'{login}' is not a GitHub account name")
    if not _HOST.fullmatch(host or ""):
        raise ValueError(f"'{host}' is not a host name")
    return ["auth", "switch", "--hostname", host, "--user", login]


def gh_switch(login: str, host: str = "github.com") -> dict:
    try:
        args = gh_switch_cmd(login, host)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    code, out = _gh(args, timeout=20)
    _gh_forget()
    return {"ok": code == 0, "message": out or f"gh now uses {login} on {host}.", "account": login}


def gh_token(login: str, host: str = "github.com") -> tuple[str | None, str | None]:
    """(token, error) of one gh account, for a child process's environment only: never logged, cached, returned to
    the dashboard or written anywhere."""
    if not valid_login(login) or not _HOST.fullmatch(host or ""):
        return None, f"'{login}' is not a GitHub account name"
    code, out = _gh(["auth", "token", "--hostname", host, "--user", login], timeout=20, raw=True)
    if code == 127 and out == GH_MISSING:
        return None, GH_MISSING
    tok = next((ln.strip() for ln in reversed(out.splitlines()) if re.fullmatch(r"[A-Za-z0-9_.-]{20,}", ln.strip())), None)
    if code == 0 and tok:
        return tok, None
    detail = gh_redact(out, tok or "").splitlines()
    return None, (f"gh has no login for {login} on {host}. Run `gh auth login --hostname {host}` and sign in as {login}, "
                  f"then press Refresh." + (f"\n{detail[0]}" if detail else ""))


def parse_github_remote(url: str) -> dict | None:
    """{host, owner, name, repo} of a GitHub-style remote URL (https, ssh:// or git@host:), else None."""
    u = (url or "").strip()
    seg = r"(?P<o>[^/\s:]+)/(?P<n>[^/\s]+?)(?:\.git)?/?"
    m = (re.fullmatch(rf"https?://(?:[^/@\s]+@)?(?P<h>[^/\s:@]+)(?::\d+)?/{seg}", u)
         or re.fullmatch(rf"ssh://(?:[^/@\s]+@)?(?P<h>[^/\s:@]+)(?::\d+)?/{seg}", u)
         or re.fullmatch(rf"[A-Za-z0-9_.-]+@(?P<h>[^/\s:@]+):/?{seg}", u))
    if not m or not _GH_OWNER.fullmatch(m["o"]) or not _GH_REPO.fullmatch(m["n"]):
        return None
    return {"host": m["h"].lower(), "owner": m["o"], "name": m["n"], "repo": f"{m['o']}/{m['n']}"}


def normalize_remote(text: str, host: str = "github.com", protocol: str = "https") -> tuple[str | None, str | None]:
    """(url, error) for a repository the user typed: an https or ssh URL, git@host:owner/name, or owner/name (made
    into a URL on `host` with gh's git protocol). Refuses anything with spaces, shell characters, a leading '-',
    a password in the URL, or another form (local paths, ext::, file://)."""
    s = (text or "").strip()
    if not s:
        return None, "enter a repository: a URL or owner/name"
    if s.startswith("-"):
        return None, "a repository can't start with '-'"
    if _REMOTE_BAD.search(s):
        return None, "the repository contains spaces or shell characters; paste a plain URL or owner/name"
    m = re.fullmatch(r"([^/:@]+)/([^/:@]+?)(?:\.git)?", s)
    if m:
        o, n = m.groups()
        if not _GH_OWNER.fullmatch(o) or not _GH_REPO.fullmatch(n) or n in (".", ".."):
            return None, f"'{s}' is not a valid owner/name"
        return (f"git@{host}:{o}/{n}.git" if protocol == "ssh" else f"https://{host}/{o}/{n}.git"), None
    if re.match(r"https?://[^/]*@", s):
        return None, "don't put a user name, password or token in the URL; choose the GitHub account instead"
    if re.fullmatch(rf"https://{_HOST.pattern}(?::\d{{1,5}})?(?:/{_PATH_SEG})+/?", s):
        return s, None
    if re.fullmatch(rf"ssh://(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?{_HOST.pattern}(?::\d{{1,5}})?(?:/{_PATH_SEG})+/?", s):
        return s, None
    if re.fullmatch(rf"[A-Za-z0-9_][A-Za-z0-9_.-]*@{_HOST.pattern}:{_PATH_SEG}(?:/{_PATH_SEG})*/?", s):
        return s, None
    return None, f"'{s}' is not a repository URL: use https://host/owner/name, git@host:owner/name.git or owner/name"


def git_account(p: "Project") -> str:
    """The gh account this project's git/GitHub operations run as ('' = git's own credentials, gh's active account)."""
    return str(p.cfg.get("git", {}).get("account") or "")


def set_git_account(p: "Project", login: str) -> None:
    login = (login or "").strip()
    if login and not valid_login(login):
        raise ValueError(f"'{login}' is not a GitHub account name")
    _write_project(p, _set_toml_key(_project_text(p), "git", "account", toml_str(login)))


def _account_row(login: str, host: str) -> dict | None:
    return next((a for a in gh_accounts()["accounts"] if a["login"] == login and a["host"] == host), None)


def _gh_identity(p: "Project", host: str = "github.com") -> tuple[str | None, str | None, str | None]:
    """(token for the child env or None, the account the gh call runs as, error)."""
    login = git_account(p)
    if login:
        tok, err = gh_token(login, host)
        return tok, login, err
    acc = gh_accounts()
    if not acc["ok"]:
        return None, None, acc["message"]
    active = next((a["login"] for a in acc["accounts"] if a["host"] == host and a["active"]), None)
    return None, active, None


def _git_auth(p: "Project", url: str) -> tuple[list[str], dict, str | None, str | None]:
    """(git -c options, env, account, error) to run a git network command as the project's gh account. Only for an
    https remote; ssh and local remotes keep git's own credentials. The token is only in the child's environment:
    the credential helper (replacing the configured ones for this one command) reads it from there."""
    login, gh = git_account(p), parse_github_remote(url or "")
    if not login or gh is None or not url.startswith("https://"):
        return [], {}, None, None
    tok, err = gh_token(login, gh["host"])
    if err:
        return [], {}, login, err
    helper = f'!f() {{ if [ "$1" = get ]; then echo username={login}; echo "password=$AGENTDESK_GIT_TOKEN"; fi; }}; f'
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper}"], {"AGENTDESK_GIT_TOKEN": tok}, login, None


def _as(res: dict, who: str | None, env: dict | None = None) -> dict:
    """The result with tokens redacted and the account it ran as."""
    res["message"] = gh_redact(str(res.get("message") or ""), *(env or {}).values())
    if who:
        res["account"] = who
        res["message"] = (res["message"] + "\n" if res["message"] else "") + f"(as GitHub account {who})"
    return res


def git_remote_info(p: "Project") -> dict:
    """origin (or the first remote) for the panel: {name, url (passwords masked), github: {host, owner, name, repo}}."""
    r = git_remote(p)
    if r is None:
        return {"name": None, "url": None, "github": None}
    return {"name": r[0], "url": gh_redact(r[1]), "github": parse_github_remote(r[1])}


def git_set_remote(p: "Project", text: str, protocol: str | None = None) -> dict:
    """Point origin at the repository the user chose (set-url, or add when there is no origin)."""
    login = git_account(p)
    host = "github.com"
    row = _account_row(login, host) if login else None
    url, err = normalize_remote(text, host, protocol or (row or {}).get("protocol") or "https")
    if err:
        return {"ok": False, "message": err}
    code, out = _g(p, "remote")
    if code != 0:
        return {"ok": False, "message": gh_redact(out)}
    if "origin" in out.split():
        _, old = _g(p, "remote", "get-url", "origin")
        if old == url:
            return {"ok": True, "message": f"origin is already {url}.", "url": url}
        code, out = _g(p, "remote", "set-url", "origin", url)
        msg = f"origin changed from {gh_redact(old)} to {url}."
    else:
        code, out = _g(p, "remote", "add", "origin", url)
        msg = f"origin added: {url}."
    _gh_forget("prs")
    return {"ok": code == 0, "message": msg if code == 0 else gh_redact(out), "url": url}


def gh_repo_name(name: str) -> str:
    """A GitHub repository name for a project name: letters, digits, - _ . only."""
    s = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or ""))).strip("-.")[:100].rstrip("-.")
    return s if s and s not in (".", "..") and _GH_REPO.fullmatch(s) else "project"


def gh_create_repo(p: "Project", login: str, private: bool = False, host: str = "github.com") -> dict:
    """Create <login>/<project name> on GitHub as the gh account `login`, make it origin (https) and push the base
    branch. The account's token only goes into the child processes' environment. Never raises: every failure is a
    message and the local project stays as it is (a created repository / origin is kept, so a push can be retried
    from the Git panel). {ok, created, pushed, repo, url, message, account}."""
    name = gh_repo_name(p.name)
    full = f"{login}/{name}"
    res = {"ok": False, "created": False, "pushed": False, "repo": full, "url": f"https://{host}/{full}", "account": login}
    if not valid_login(login):
        return {**res, "message": f"'{login}' is not a GitHub account name"}
    tok, err = gh_token(login, host)
    if err:
        return {**res, "message": err}
    code, out = _gh(["repo", "create", full, "--private" if private else "--public"], token=tok, host=host, timeout=90)
    if code != 0:
        return {**res, "message": f"Could not create the GitHub repository {full}: {gh_redact(out, tok or '') or 'gh failed'}"}
    res["created"] = True
    _gh_forget("repos")
    try:
        set_git_account(p, login)
    except (OSError, ValueError) as e:
        return {**res, "message": f"Created {res['url']}, but could not save the GitHub account in the project: {e}"}
    r = git_set_remote(p, full, "https")
    if not r.get("ok"):
        return {**res, "message": f"Created {res['url']}, but could not set it as origin: {r.get('message')}"}
    base = p.cfg["repo"]["base"]
    r = git_push(p, base)
    if not r.get("ok"):
        return {**res, "message": f"Created {res['url']} and set it as origin, but pushing '{base}' failed (push again "
                                  f"from the Git panel): {gh_redact(str(r.get('message') or ''), tok or '')}"}
    return {**res, "ok": True, "pushed": True, "message": f"Created {res['url']} ({'private' if private else 'public'}) "
                                                         f"and pushed '{base}'."}


def parse_repos(out: str) -> list[dict]:
    """`gh repo list --json nameWithOwner,url,isPrivate` -> [{name, url, private}]."""
    data = _json_or_none(out)
    return [{"name": str(x["nameWithOwner"]), "url": str(x.get("url") or ""), "private": x.get("isPrivate") is True}
            for x in (data if isinstance(data, list) else []) if isinstance(x, dict) and x.get("nameWithOwner")]


def gh_repo_list(p: "Project", owner: str = "", refresh: bool = False) -> dict:
    """Repositories of `owner` (default: the project's account, else gh's active one) visible to that account."""
    tok, who, err = _gh_identity(p)
    if err:
        return {"ok": False, "message": err, "repos": [], "account": who}
    owner = (owner or who or "").strip()
    if not valid_login(owner):
        return {"ok": False, "message": f"'{owner}' is not a GitHub user or organization name", "repos": [], "account": who}

    def load() -> dict:
        code, out = _gh(["repo", "list", owner, "--json", "nameWithOwner,url,isPrivate", "--limit", "100"], token=tok)
        if code != 0:
            return {"ok": False, "message": out, "repos": []}
        return {"ok": True, "message": "", "repos": parse_repos(out)}
    return {**_gh_cached(("repos", owner, who), load, refresh), "owner": owner, "account": who}


PR_FIELDS = "number,title,headRefName,baseRefName,author,url,state,isDraft"


def parse_prs(out: str) -> list[dict]:
    """`gh pr list --json PR_FIELDS` -> [{number, title, head, base, author, url, state, draft}]."""
    data = _json_or_none(out)
    rows = []
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict):
            continue
        a = x.get("author")
        rows.append({"number": int(x.get("number") or 0), "title": str(x.get("title") or ""),
                     "head": str(x.get("headRefName") or ""), "base": str(x.get("baseRefName") or ""),
                     "author": str(a.get("login") or "") if isinstance(a, dict) else str(a or ""),
                     "url": str(x.get("url") or ""), "state": str(x.get("state") or ""), "draft": x.get("isDraft") is True})
    return rows


def parse_checks(out: str) -> dict:
    """`gh pr view --json statusCheckRollup` -> {passed, failed, pending, skipped, total, summary, checks: [{name, outcome}]}."""
    data = _json_or_none(out)
    items = data.get("statusCheckRollup") if isinstance(data, dict) else None
    checks = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        if c.get("__typename") == "StatusContext" or ("context" in c and "conclusion" not in c):
            name, state = str(c.get("context") or "status"), str(c.get("state") or "").upper()
            outcome = "passed" if state == "SUCCESS" else "failed" if state in ("FAILURE", "ERROR") else "pending"
        else:
            name = str(c.get("name") or c.get("workflowName") or "check")
            status, concl = str(c.get("status") or "").upper(), str(c.get("conclusion") or "").upper()
            outcome = ("pending" if status != "COMPLETED" else "passed" if concl in _CHECK_OK
                       else "skipped" if concl in _CHECK_SKIP else "failed")
        checks.append({"name": name, "outcome": outcome})
    n = {k: sum(1 for c in checks if c["outcome"] == k) for k in ("passed", "failed", "pending", "skipped")}
    summary = ", ".join(f"{v} {k}" for k, v in n.items() if v) or "no checks"
    return {**n, "total": len(checks), "summary": summary, "checks": checks}


def _gh_repo(p: "Project") -> tuple[dict | None, str | None]:
    info = git_remote_info(p)
    if info["github"] is None:
        return None, ("local-only project: connect it to a GitHub repository first" if info["url"] is None
                      else f"the remote {info['url']} is not a GitHub repository")
    return info["github"], None


def git_prs(p: "Project", refresh: bool = False) -> dict:
    """Open pull requests of the connected repository."""
    gh, err = _gh_repo(p)
    if err:
        return {"ok": False, "message": err, "prs": []}
    tok, who, err = _gh_identity(p, gh["host"])
    if err:
        return {"ok": False, "message": err, "prs": [], "account": who}
    repo = f"{gh['host']}/{gh['repo']}"

    def load() -> dict:
        code, out = _gh(["pr", "list", "--repo", repo, "--state", "open", "--limit", "50", "--json", PR_FIELDS],
                        cwd=p.repo, token=tok, host=gh["host"])
        return {"ok": code == 0, "message": "" if code == 0 else out, "prs": parse_prs(out) if code == 0 else []}
    return {**_gh_cached(("prs", repo, who), load, refresh), "repo": gh["repo"], "account": who}


def git_pr_checks(p: "Project", number: int, refresh: bool = False) -> dict:
    gh, err = _gh_repo(p)
    if err:
        return {"ok": False, "message": err}
    tok, who, err = _gh_identity(p, gh["host"])
    if err:
        return {"ok": False, "message": err, "account": who}
    repo = f"{gh['host']}/{gh['repo']}"

    def load() -> dict:
        code, out = _gh(["pr", "view", str(int(number)), "--repo", repo, "--json", "statusCheckRollup"],
                        cwd=p.repo, token=tok, host=gh["host"])
        return {"ok": True, "message": "", **parse_checks(out)} if code == 0 else {"ok": False, "message": out}
    return {**_gh_cached(("checks", repo, int(number), who), load, refresh), "number": int(number), "account": who}


def _branch_name_ok(p: "Project", b: str) -> bool:
    return bool(b) and not b.startswith("-") and _g(p, "check-ref-format", "--branch", b)[0] == 0


def git_pr_create(p: "Project", title: str, body: str = "", head: str = "", base: str = "", draft: bool = False) -> dict:
    """Open a pull request from `head` (default: the current branch) into `base` (default: the project's base).
    The branch must be on the remote already (Push it first)."""
    gh, err = _gh_repo(p)
    if err:
        return {"ok": False, "message": err}
    title, body = (title or "").strip(), (body or "").strip()
    head = (head or "").strip() or _g(p, "rev-parse", "--abbrev-ref", "HEAD")[1]
    base = (base or "").strip() or p.cfg["repo"]["base"]
    if not title:
        return {"ok": False, "message": "a pull request needs a title"}
    if len(title) > 256 or len(body) > 60_000:
        return {"ok": False, "message": "the title or description is too long"}
    for b in (head, base):
        if not _branch_name_ok(p, b):
            return {"ok": False, "message": f"'{b}' is not a valid branch name"}
    if head == base:
        return {"ok": False, "message": f"the pull request needs a branch other than the base '{base}'"}
    remote = git_remote(p)
    if remote and _g(p, "rev-parse", "--verify", "-q", f"refs/remotes/{remote[0]}/{head}")[0] != 0:
        return {"ok": False, "message": f"'{head}' is not on {remote[0]} yet: push it first."}
    tok, who, err = _gh_identity(p, gh["host"])
    if err:
        return {"ok": False, "message": err, "account": who}
    args = ["pr", "create", "--repo", f"{gh['host']}/{gh['repo']}", "--head", head, "--base", base,
            f"--title={title}", f"--body={body}"] + (["--draft"] if draft else [])
    code, out = _gh(args, cwd=p.repo, token=tok, host=gh["host"], timeout=60)
    url = next((w for w in reversed(out.split()) if w.startswith("https://")), "") if code == 0 else ""
    _gh_forget("prs")
    return _as({"ok": code == 0, "message": out or (f"Opened {url}" if url else ""), "url": url}, who)


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


def branch_exists(p: Project, branch: str) -> bool:
    return git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], p.repo)[0] == 0


def dependency_branches(p: Project, task: Task, state: dict) -> list[str]:
    """The branches of the task's finished dependencies that still exist, in `depends` order."""
    out = []
    for dep in task.depends:
        e = state["tasks"].get(dep, {})
        if e.get("status") == "done":
            br = e.get("branch") or f"auto/{dep}"
            if br not in out and branch_exists(p, br):
                out.append(br)
    return out


def unmerged_files(wt: Path) -> list[str]:
    """Files with unresolved merge conflicts in a worktree."""
    code, out = git(["diff", "--name-only", "--diff-filter=U"], wt)
    return [l.strip() for l in out.splitlines() if l.strip()] if code == 0 else []


def merge_into(wt: Path, branch: str, message: str) -> list[str]:
    """Merge `branch` into the worktree's branch (a merge commit). [] on success; on a conflict the merge is left in
    progress (markers in the files) and the conflicted files are returned."""
    code, out = git(["merge", "--no-ff", "--no-edit", "-m", message, branch], wt)
    if code == 0:
        return []
    files = unmerged_files(wt)
    if not files:  # failed for another reason (not a conflict): nothing is left half way
        git(["merge", "--abort"], wt)
        raise RuntimeError(f"git merge {branch} failed: {out.strip()[:500]}")
    return files


def ensure_worktree(p: Project, store: Store, task: Task, state: dict | None = None) -> Path:
    """The task's worktree on `auto/<id>`. A new one starts from the first finished dependency's branch (else the base
    branch) and gets every other finished dependency's branch merged in, so the task sees all the work it builds on.
    A merge conflict is left for the worker (see worktree_conflicts); an existing worktree is reused as it is."""
    wt = Path(p.cfg["repo"]["worktrees"]) / task.id
    if wt.exists():
        return wt
    state = state if state is not None else store.load()
    why = worktree_cap_reason(p, task.id, state)
    if why:
        raise RuntimeError(why)
    branch, base = f"auto/{task.id}", p.cfg["repo"]["base"]
    deps = dependency_branches(p, task, state)
    if deps:
        base = deps[0]
    add_worktree(p, wt, branch, base)
    store.log(f"{task.id}: worktree {wt} on {branch} from {base}")
    for br in deps[1:]:
        files = merge_into(wt, br, f"{task.id}: merge {br} (dependency)")
        if files:
            store.log(f"{task.id}: merging {br} left conflicts in {', '.join(files[:10])}; the worker resolves them")
            break  # one merge at a time: the rest can't start while this one is in progress
        store.log(f"{task.id}: merged {br}")
    return wt


def worktree_conflicts(p: Project, task: Task, wt: Path, state: dict) -> str:
    """The brief's preamble when the worktree has an unfinished dependency merge (conflicts, or branches not merged
    yet because an earlier merge stopped), else ""."""
    files = unmerged_files(wt)
    missing = [br for br in dependency_branches(p, task, state)
               if git(["merge-base", "--is-ancestor", br, "HEAD"], wt)[0] != 0]
    if not files and not missing:
        return ""
    parts = ["## First: finish merging the dependencies\n"]
    if files:
        parts.append("Merging the branches this task depends on stopped with conflicts in: "
                     + ", ".join(f"`{f}`" for f in files[:30]) + ". Resolve them keeping the intent of both sides, then "
                     "`git add` the files (don't commit; the orchestrator does).")
    if missing:
        parts.append(("After that, merge" if files else "Merge") + " these dependency branches too, resolving any conflict the same way: "
                     + ", ".join(f"`git merge --no-edit {br}`" for br in missing)
                     + (" (run `git commit --no-edit` first to finish the merge in progress)." if files else "."))
    return "\n".join(parts) + "\n\n"


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
    """The project's `kind` checks in `wt`. Besides [gates.env] they get AGENTDESK_TASK_ID (the worktree's name) and
    AGENTDESK_WORKTREE, so checks of tasks running in parallel can keep their ports, databases and temp files apart
    (e.g. a port from 20000 + hash, or a database under the worktree)."""
    g = p.cfg.get("gates", {})
    sel = "--workspace" if not crates else " ".join(f"-p {c}" for c in crates)
    env = {"AGENTDESK_TASK_ID": wt.name, "AGENTDESK_WORKTREE": str(wt), **{k: str(v) for k, v in g.get("env", {}).items()}}
    for cmd in g.get(kind, []):
        cmd = cmd.replace("{crates}", sel)
        store.log(f"  gate: {cmd}")
        code, out = run(cmd, wt, env=env, timeout=int(g.get("timeout_s", 3600)), shell=True)
        if environment_failure(code, out):
            store.log(f"  gate couldn't run ({out.strip()[:160]}); trying once more")
            time.sleep(2)
            code, out = run(cmd, wt, env=env, timeout=int(g.get("timeout_s", 3600)), shell=True)
            if environment_failure(code, out):
                return False, f"$ {cmd}\n{ENV_FAILURE}: {out.strip()[:1500]}"
        if code != 0:
            return False, f"$ {cmd}\n{trim(out, int(g.get('max_feedback_chars', 6000)))}"
    return True, ""


# ----------------------------------------------------------------------------- auto-runner (pure logic; the loop is in server.py)

NL = chr(10)
# run_timeout_min: a run older than this is killed with all its children (0 = never); min_free_gb: no run starts below it.
AUTO_DEFAULTS = {"enabled": True, "interval_s": 30, "retry_crashed": 1, "run_timeout_min": 720, "min_free_gb": 1.0}


def autorunner_cfg(p: Project | None = None, global_cfg: dict | None = None, project_cfg: dict | None = None) -> dict:
    """Effective auto-runner settings: code defaults < settings.toml [autorunner] < project.toml [autorunner]."""
    g = settings().get("autorunner", {}) if global_cfg is None else global_cfg
    pc = (p.cfg.get("autorunner", {}) if p is not None else {}) if project_cfg is None else project_cfg
    out = {**AUTO_DEFAULTS, **g, **pc}
    out["enabled"] = bool(out["enabled"])
    out["interval_s"] = max(5, int(out["interval_s"]))
    out["retry_crashed"] = max(0, int(out["retry_crashed"]))
    out["run_timeout_min"] = max(0, int(out["run_timeout_min"]))
    out["min_free_gb"] = max(0.0, float(out["min_free_gb"]))
    return out


SERVER_POLICY = "server-policy.json"
_POLICY: dict = {"key": None, "data": {}}


def server_policy() -> dict:
    """`<data>/server-policy.json`, written by the AgentDesk Server gateway for this user's instance ({} on the
    desktop): the server's [hygiene] defaults, the disk gate (refuse new runs / clones / imports when the server is
    at its hard limit or the user over quota), environment for the shared package caches, clone options and the
    upload limit. Re-read when the file changes."""
    f = DATA / SERVER_POLICY
    try:
        st = f.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    if _POLICY["key"] != key:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _POLICY.update(key=key, data=data if isinstance(data, dict) else {})
    return _POLICY["data"]


def disk_gate() -> str | None:
    """Why new runs, clones, imports and uploads are refused right now (server disk budget / user quota), or None.
    Running work is never stopped by it. A gate the gateway stopped refreshing (`until` passed) no longer counts."""
    g = server_policy().get("gate") or {}
    if not isinstance(g, dict) or not g.get("refuse"):
        return None
    try:
        if float(g.get("until") or 0) and float(g["until"]) < time.time():
            return None
    except (TypeError, ValueError):
        pass
    return str(g.get("reason") or "the server's disk budget is used up")


def policy_env(p: "Project | None" = None) -> dict[str, str]:
    """Extra environment for runs on a server: the shared package caches, and with a shared target dir a per-user
    CARGO_TARGET_DIR per project (only when the project does not set its own in [gates.env])."""
    pol = server_policy()
    env = {str(k): str(v) for k, v in pol["env"].items()} if isinstance(pol.get("env"), dict) else {}
    st = pol.get("shared_target_dir")
    if p is not None and st and not ((p.cfg.get("gates") or {}).get("env") or {}).get("CARGO_TARGET_DIR"):
        env["CARGO_TARGET_DIR"] = str(Path(str(st)) / p.slug / "target")
    return env


def clone_options() -> list[str]:
    """`git clone` options from the server policy: --depth N (clone_depth) and --filter=blob:none (partial_clone)."""
    g = server_policy().get("git") or {}
    out: list[str] = []
    try:
        depth = int(g.get("clone_depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    if depth > 0:
        out += ["--depth", str(depth), "--no-single-branch"]
    if g.get("partial_clone"):
        out.append("--filter=blob:none")
    return out


def worktree_cap_reason(p: "Project", task_id: str, state: dict | None = None) -> str | None:
    """Why no new worktree may be made for `task_id` ([hygiene] max_worktrees reached), or None. Only worktrees of
    tasks that aren't done count: a finished task's worktree is disk hygiene's to remove, and counting it would stall a
    parallel run behind work that is merely waiting to be merged."""
    cap = int(hygiene_cfg(p)["max_worktrees"])
    raw = str((p.cfg.get("repo") or {}).get("worktrees") or "")
    if cap <= 0 or not raw:
        return None
    root = Path(raw)
    if (root / task_id).exists() or not root.is_dir():
        return None
    tasks = (state if state is not None else Store(p).load()).get("tasks", {})
    try:
        n = sum(1 for d in root.iterdir() if d.is_dir() and not d.is_symlink() and d.name not in (COWORK, INTEGRATION)
                and tasks.get(d.name, {}).get("status") != "done")
    except OSError:
        return None
    if n >= cap:
        return (f"the project has {n} unfinished task worktrees (limit [hygiene] max_worktrees = {cap}); the next task "
                "starts when one of them finishes")
    return None


def large_files(wt: Path, base: str, limit_mb: float) -> list[tuple[str, int]]:
    """Files added or changed on this branch (since `base`) that are larger than limit_mb: [(path, bytes)]."""
    if limit_mb <= 0:
        return []
    code, out = git(["diff", "--name-only", "--diff-filter=AM", "-z", f"{base}...HEAD"], wt)
    names = [n for n in out.split("\0") if n.strip()][:2000] if code == 0 else []
    if not names:
        return []
    code, out = git(["ls-tree", "-r", "-l", "-z", "HEAD", "--", *names], wt)
    rows = []
    for rec in out.split("\0") if code == 0 else []:
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[3].isdigit() and int(parts[3]) > limit_mb * 1024 * 1024:
            rows.append((path, int(parts[3])))
    return sorted(rows, key=lambda r: -r[1])


HYGIENE_DEFAULTS = {"enabled": True, "auto": False, "max_target_gb": 60.0, "max_total_gb": 150.0, "stale_days": 3.0,
                    "max_runs_mb": 200.0, "max_log_mb": 20.0, "interval_min": 30.0, "target_dirs": [],
                    "artifacts": False,          # also clean other build kinds (node_modules, venvs, dist ...): server on
                    "worktree_ttl_hours": 0.0,   # >0: worktrees of done tasks idle this long are removed (branch kept)
                    "max_worktrees": 0.0,        # >0: at most this many task worktrees per project (cowork not counted)
                    "git_gc_hours": 0.0,         # >0: `git gc --auto` at most this often, off-peak, never during a run
                    "offpeak_hours": "",         # "01-06": local hours in which git gc may run ("" = any time)
                    "large_file_mb": 0.0}        # >0: the task report warns about committed files above this size
_HYGIENE_RANGE = {"max_target_gb": (0.1, 100000.0), "max_total_gb": (0.1, 100000.0), "stale_days": (0.0, 3650.0),
                  "max_runs_mb": (1.0, 1e6), "max_log_mb": (1.0, 1e5), "interval_min": (5.0, 10080.0),
                  "worktree_ttl_hours": (0.0, 1e5), "max_worktrees": (0.0, 1000.0), "git_gc_hours": (0.0, 1e5),
                  "large_file_mb": (0.0, 1e6)}


def _hygiene_value(key: str, v) -> tuple[bool, object]:
    """(True, value) for a valid [hygiene] value, else (False, what was expected)."""
    d = HYGIENE_DEFAULTS[key]
    if isinstance(d, bool):
        return (True, v) if isinstance(v, bool) else (False, "expected true or false")
    if isinstance(d, list):
        if isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v):
            return True, [x.strip() for x in v]
        return False, "expected a list of folder paths"
    if isinstance(d, str):
        if isinstance(v, str) and (not v.strip() or re.fullmatch(r"\s*\d{1,2}\s*-\s*\d{1,2}\s*", v)):
            return True, v.strip()
        return False, 'expected hours like "01-06" (or "")'
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return False, "expected a number"
    lo, hi = _HYGIENE_RANGE[key]
    if not lo <= float(v) <= hi:
        return False, f"expected {lo:g} to {hi:g}"
    return True, float(v)


def hygiene_cfg(p: Project | None = None, global_cfg: dict | None = None, project_cfg: dict | None = None,
                server_cfg: dict | None = None) -> dict:
    """Effective disk-hygiene settings: code defaults < settings.toml [hygiene] < the server policy's [hygiene]
    (AgentDesk Server only: the admin's defaults, build cleanup on) < project.toml [hygiene]. Never raises: a bad or
    unknown value is ignored (the one below it stays) and described in `errors` (logged by the hygiene loop)."""
    errors: list[str] = []
    if server_cfg is None and global_cfg is None:
        server_cfg = server_policy().get("hygiene") or {}
    if global_cfg is None:
        try:
            global_cfg = settings().get("hygiene", {})
        except Exception as e:  # unreadable settings.toml: defaults
            global_cfg, errors = {}, [f"settings.toml can't be read ({type(e).__name__}: {e})"]
    if project_cfg is None:
        project_cfg = (p.cfg.get("hygiene", {}) if isinstance(p.cfg, dict) else {}) if p is not None else {}
    out = copy.deepcopy(HYGIENE_DEFAULTS)
    layers = [("settings.toml", global_cfg), ("project.toml", project_cfg)]
    if server_cfg:  # AgentDesk Server: the admin's policy (build cleanup on) beats the user's settings.toml
        layers.insert(1, ("server policy", server_cfg))
    for where, src in layers:
        if not isinstance(src, dict):
            errors.append(f"{where}: [hygiene] is not a table; ignored")
            continue
        for k, v in src.items():
            if k not in HYGIENE_DEFAULTS:
                errors.append(f"{where}: [hygiene] {k} is not a known setting; ignored")
                continue
            ok, val = _hygiene_value(k, v)
            if ok:
                out[k] = val
            else:
                errors.append(f"{where}: [hygiene] {k} = {v!r}: {val}; using {out[k]!r}")
    out["errors"] = errors
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

        if st != "done" and int(e.get("crashes", 0)) > retry_crashed:  # a finished task is no problem, whatever came before
            add("repeated", f"crashed {e['crashes']} times; auto-retry used up: {fail.splitlines()[0] if fail else ''}".strip())
        elif kind == "gate" and st != "done":
            add("gate", "checks failed: " + (fail.splitlines()[0] if fail else ""), "log")
        elif st == "blocked":
            add("blocked", "blocked after all tiers: " + (fail.splitlines()[0][:120] if fail else "see the log"))
        elif st == "paused":
            add("paused", "paused: the run budget was reached; Run all goes on (a spending decision)")
        elif st == "needs_decision":
            add("decision", "needs your decision: " + (str(e.get("question") or "").splitlines() or [""])[0][:160], "report")
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
        # Shared by the tasks of one run when they go in parallel: at most `gate_sem` checks at a time (None = no
        # limit), and one git operation on the repository's worktrees/branches at a time.
        self.gate_sem: asyncio.Semaphore | None = None
        self.repo_lock = threading.Lock()
        self.active = 0   # model calls going right now (several in a parallel run)
        self.starts = 0   # model calls started so far

    def begin(self) -> dict:
        """Mark the start of a model call: a token for end()/release()."""
        self.active += 1
        self.starts += 1
        return {"before": self.mark(), "shared": self.active > 1, "n": self.starts, "open": True}

    def end(self, tok: dict) -> dict:
        """usage fields for a call started with begin(). If another call overlapped it, the account's usage delta
        belongs to all of them: usage_used_pct is unknown and the delta is kept as usage_window_pct (share_usage
        splits the run's delta by cost at the end). cost_usd and tokens come from the SDK per call either way."""
        u = self.used(tok["before"])
        shared = tok["shared"] or self.starts != tok["n"] or self.active > 1
        self.release(tok)
        if shared:
            u["usage_window_pct"], u["usage_used_pct"], u["usage_shared"] = u["usage_used_pct"], None, True
        return u

    def release(self, tok: dict) -> None:
        if tok.get("open"):
            tok["open"] = False
            self.active = max(0, self.active - 1)

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

    def budget(self) -> float:
        """This run's budget in USD: `run --budget` (an approved plan's estimate), else [limits] run_budget_usd."""
        return float(getattr(self, "budget_override", None) or self.limits["run_budget_usd"])

    def stop_reason(self) -> str | None:
        if self.state.get("spent_usd", 0.0) >= self.budget() + self.state.get("budget_base", 0.0):
            return f"run budget {self.budget():g} USD spent"
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
    _load_sdk()
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
        # mcp__<server>__* tools fall through: allowed here; which servers exist is decided by project.toml [mcp.*]
        return PermissionResultAllow()

    return can_use_tool


ROUTER_SCHEMA = {"type": "object", "required": ["tier", "why", "brief"], "properties": {
    "tier": {"type": "string", "enum": TIER_ORDER}, "why": {"type": "string"}, "brief": {"type": "string"},
    "effort": {"type": "string", "enum": ["low", "medium", "high"]},
    "crates": {"type": "array", "items": {"type": "string"}}}}

ROUTER_PROMPT = """You route a coding task to the cheapest worker that will do it well, and write its brief.

Tiers: mechanical (clear, local edits: strings, renames, small fixes with an obvious cause, docs),
standard (normal feature work or debugging in a few files), hard (design across modules, subtle
algorithms, unclear root causes, UI architecture).
Effort, within the tier (how hard the model thinks): low for a clear, well-specified change; medium for normal
work; high when it needs careful reasoning (tricky logic, many interacting pieces) but not a stronger model. Prefer
a cheaper tier at higher effort over a stronger tier when the work is narrow but subtle.

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
Other tasks run in parallel and add their own code next to yours, so write tests that stay true as the
project grows: assert on what this task adds, never that a registry, folder, list or table is empty or holds
an exact total of things other tasks may add. If a check fails in a test this task didn't write, decide
whether that test encodes an assumption the project has outgrown (e.g. "nothing is registered yet"): then fix
that test minimally and say so in your summary; otherwise fix your code.
If the task can't be done without a decision only the user can make (a choice the task leaves open, a missing
precondition, two reasonable options with different consequences), don't guess and don't stop silently: finish with
a line `NEEDS_DECISION:` followed by the question, the options and what you recommend; the task then waits for the
user's answer, which is added to the task. If the task turns out to need no change at all, finish with a line
`NO_CHANGES_NEEDED:` and the reason. A task with a "## Decision" section has been answered: follow it.
Finish with a short summary of what you changed, then a line `NOTES:` followed by at most three
lines of durable facts later tasks should know (or `NOTES: none`).

Brief:
{brief}

Full task:
{body}
"""


def mcp_servers(p: Project, role: str | None, log=None) -> dict[str, dict]:
    """The project's extra MCP servers for `role` ("worker"/"router") as SDK stdio configs. `npx` and other
    shims resolve through shutil.which (npx.cmd on Windows). The SDK config has no cwd: the server inherits the
    CLI's cwd, which is the worktree for workers (options(cwd=wt))."""
    if not role:
        return {}
    out = {}
    for name, c in p.mcp(log).items():
        if role in c["roles"]:
            out[name] = {"type": "stdio", "command": shutil.which(c["command"]) or c["command"], "args": c["args"], "env": c["env"]}
    return out


def options(p: Project, role: str | None = None, log=None, **kw) -> ClaudeAgentOptions:
    """ClaudeAgentOptions for a project. With `role`, the project's `[mcp.*]` servers for that role are added to
    mcp_servers (merged with any passed in). allowed_tools is left alone: list `mcp__<name>__*` there if wanted."""
    _load_sdk()
    # `scratch=False` (the full-access Coworker, which also installs and starts AgentDesk): don't hand the agent the
    # workers' throw-away data home, or whatever it launches would use it instead of the real data.
    scratch = kw.pop("scratch", True)
    env = {**account_env(p.account), **({"AGENTDESK_HOME": scratch_home(), "TMPDIR": work_tmp()} if scratch else {}),
           **kw.pop("env", {})}
    try:
        extra = mcp_servers(p, role, log)
    except Exception as e:  # never break a run over optional config
        extra = {}
        if log:
            log(f"mcp: skipped ({type(e).__name__}: {e})")
    if extra:
        kw["mcp_servers"] = {**extra, **(kw.get("mcp_servers") or {})}
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


def new_tool_stats() -> dict:
    return {"total": 0, "by_tool": {}, "mcp": {}}


def count_tool(stats: dict, name: str) -> None:
    """Count one tool call; `mcp__<server>__<tool>` calls are also grouped per server. Never raises."""
    try:
        stats["total"] += 1
        stats["by_tool"][name] = stats["by_tool"].get(name, 0) + 1
        if name.startswith("mcp__"):
            server = name.split("__")[1] or "?"
            stats["mcp"][server] = stats["mcp"].get(server, 0) + 1
    except Exception:
        pass


def merge_tool_stats(entry: dict, run: dict) -> None:
    """Add one run's stats into the task entry's `tool_stats` and append it to `runs_stats`."""
    acc = entry.setdefault("tool_stats", new_tool_stats())
    acc["total"] = acc.get("total", 0) + run["total"]
    for key in ("by_tool", "mcp"):
        d = acc.setdefault(key, {})
        for k, v in run[key].items():
            d[k] = d.get(k, 0) + v
    entry.setdefault("runs_stats", []).append(run)


def tools_line(stats: dict) -> str:
    top = sorted(stats.get("by_tool", {}).items(), key=lambda kv: -kv[1])[:8]
    line = f"Tools: {stats.get('total', 0)} calls"
    if top:
        line += " (" + ", ".join(f"{k} {v}" for k, v in top) + ")"
    mcp = stats.get("mcp") or {}
    line += "; MCP: " + (", ".join(f"{k} {v}" for k, v in sorted(mcp.items())) if mcp else "none")
    return line


def activity_path(p: Project, task_id: str) -> Path:
    return p.path("runs") / f"{task_id}.jsonl"


class Activity:
    """The compact, append-only timeline of a task's runs: `runs/<id>.jsonl`, one JSON event per line (router and
    worker tool calls, files touched, text snippets, tool errors, results, checks, retries). Sizes are capped. A line
    is written with one call, so a reader sees whole lines (a torn last line is skipped by `read_activity`)."""

    def __init__(self, p: Project, task_id: str, role: str):
        self.path, self.role = activity_path(p, task_id), role
        self.calls: dict[str, str] = {}
        self.stats = new_tool_stats()   # tool calls seen since the last take_stats()

    def take_stats(self) -> dict:
        s, self.stats = self.stats, new_tool_stats()
        return s

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
                    count_tool(self.stats, b.name)
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
    if task.tier in TIER_ORDER:  # the task file decides: no model call
        Activity(p, task.id, "router").add("routed", tier=task.tier, text="set in the task file")
        return {"tier": task.tier, "why": "set in the task file", "brief": task.body, "effort": task.effort}
    _load_sdk()
    r = settings()["router"]
    opts = options(p, role="router", log=store.log, model=r["model"], max_turns=int(r["max_turns"]), max_budget_usd=float(r["budget_usd"]), cwd=str(p.repo),
                   allowed_tools=["Read", "Grep", "Glob"], disallowed_tools=["Edit", "Write", "Bash", "PowerShell"],
                   permission_mode="default", setting_sources=["project"], output_format={"type": "json_schema", "schema": ROUTER_SCHEMA})
    result = None
    tok = meter.begin()
    delays, tries = network_delays(), 0
    act = Activity(p, task.id, "router")
    act.add("start", tier="router", model=r["model"], effort=r.get("effort"))
    try:
        result = await _route_loop(p, store, task, meter, opts, r, act, tok, delays)
    finally:
        meter.release(tok)
    if not result or result.get("tier") not in TIER_ORDER:
        result = {"tier": "standard", "why": "router gave no usable answer", "brief": task.body}
    act.add("routed", tier=result.get("tier"), text=result.get("why", ""))
    return result


async def _route_loop(p: Project, store: Store, task: Task, meter: Meter, opts, r: dict, act, tok: dict, delays: list) -> dict | None:
    result, tries = None, 0
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
                                                         **meter.end(tok)})
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
    return result


ARCHITECT_SCHEMA = {"type": "object", "required": ["summary", "tasks"], "properties": {
    "summary": {"type": "string"},
    "tasks": {"type": "array", "items": {"type": "object", "required": ["title", "goal", "done_when", "files", "depends"], "properties": {
        "title": {"type": "string"}, "goal": {"type": "string"},
        "done_when": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
        "tier": {"type": "string", "enum": TIER_ORDER},
        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
        "depends": {"type": "array", "items": {"type": "string"}}}}}}}

ARCHITECT_PROMPT = """You are the architect of an automated coding team. Turn the goal below into a dependency graph of
tasks for worker agents. Workers run in parallel, each in its own git worktree on its own branch; a task starts when
everything it depends on is done, from a branch that already contains that work. Each worker sees only its own task.

First read the repository (Read, Grep, Glob): its layout, conventions, tests and instructions (CLAUDE.md, AGENTS.md,
README). Be thorough but don't read everything. Then design the graph:
- Each task is one coherent change a worker can finish in one session, with its tests. Prefer 3-12 tasks; never more
  than {max_tasks}. Don't split what must change together; don't bundle unrelated work.
- Maximize safe parallelism: tasks that touch disjoint files run side by side. Put shared foundations (data models,
  interfaces, schemas, config, helpers) first and make the tasks using them depend on them.
- Two tasks that may run at the same time must not edit the same file: make one depend on the other, or merge them.
  List in `files` every file a task will create or edit (folders allowed, e.g. "src/api/").
- goal: what to build or change and why, where (files, functions), the interfaces or contracts it must provide or
  follow (names, signatures, data formats) so parallel tasks fit together, and constraints.
- done_when: concrete, checkable acceptance criteria (tests that pass, commands that succeed, behaviour).
- tier: mechanical (clear local edits), standard (normal feature or debugging work), hard (cross-module design,
  subtle algorithms). Choose the cheapest tier that will do it well.
- effort (optional): low for a clear, well-specified change, medium for normal work, high when the work is narrow but
  needs careful reasoning. A cheaper tier at higher effort often beats a stronger tier; leave it out to use the
  tier's own.
- depends: the 1-based positions of earlier tasks in your list as "d1", "d2", ..., or ids of queued tasks below.
  Only real dependencies: an unneeded edge costs parallelism.
- Tests must stay valid as the graph fills in: a foundation task's tests must not assert that nothing is
  registered yet or that a collection has an exact size other tasks will change. Say so in its goal.
- A task that generates data (a simulator, fixtures, seed data, sample streams) must also prove the absence of false
  positives: its done_when includes that the normal/healthy data triggers nothing, not only that injected cases appear.

The project's checks after every task: {gates}
Queued tasks that aren't done yet (you may depend on them; don't redo them):
{queued}
Project notes:
{notes}

Goal:
{goal}

Answer with the plan as JSON: summary (one line) and tasks.
"""

ARCHITECT_FIX = """The plan checker found problems. Fix them and answer with the whole corrected plan (same JSON format).

{problems}
"""


def architect_review(p: Project, raw: dict) -> tuple[list[dict] | None, list[str], list[str]]:
    """(normalized plan tasks or None, problems that must be fixed, warnings) for the architect's JSON answer."""
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return None, ["the plan has no tasks"], []
    conv = []
    for t in tasks:
        if not isinstance(t, dict):
            return None, ["each task must be an object"], []
        crit = [str(c).strip() for c in t.get("done_when") or [] if str(c).strip()]
        conv.append({"title": t.get("title"), "goal": t.get("goal"), "files": t.get("files") or [], "tier": t.get("tier") or None,
                     "effort": t.get("effort") or None,
                     "depends": t.get("depends") or [], "body": ("## Done when\n" + "\n".join(f"- {c}" for c in crit)) if crit else ""})
    try:
        norm = check_plan_tasks(p, conv)
    except ValueError as e:
        return None, [str(e)], []
    problems = [f"task {t['key']} ({t['title']}) has no acceptance criteria (done_when)" for t in norm if "## Done when" not in t["body"]]
    return norm, problems, plan_warnings(norm)


async def architect(p: Project, goal: str) -> dict:
    """Design a task graph for `goal` and save it as the project's draft plan (replacing a draft that is there). The
    plan is checked (ids, cycles, files two parallel tasks both touch); problems go back to the architect up to
    [architect] rounds times. Returns the plan view. Nothing runs before the plan is approved."""
    _load_sdk()
    a = settings()["architect"]
    store = Store(p)
    state = store.load()
    meter = Meter(p, store, state)
    gates = "; ".join(p.cfg.get("gates", {}).get("quick", [])) or "(none)"
    open_tasks = [t for t in load_tasks(p) if state["tasks"].get(t.id, {}).get("status") != "done"]
    queued = "\n".join(f"- {t.id}: {t.title}" + (f" (after {', '.join(t.depends)})" if t.depends else "") for t in open_tasks[:40]) or "(none)"
    opts = options(p, role="router", log=store.log, model=a["model"], effort=a.get("effort"), max_turns=int(a["max_turns"]),
                   max_budget_usd=float(a["budget_usd"]), cwd=str(p.repo), allowed_tools=["Read", "Grep", "Glob"],
                   disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"], permission_mode="default",
                   setting_sources=["project"], output_format={"type": "json_schema", "schema": ARCHITECT_SCHEMA})
    message = ARCHITECT_PROMPT.format(max_tasks=MAX_PLAN_TASKS, gates=gates, queued=queued, notes=store.notes(), goal=goal.strip())
    act = Activity(p, "_architect", "architect")
    act.add("start", tier="architect", model=a["model"], effort=a.get("effort"), text=goal[:ACTIVITY_TEXT])
    store.log(f"architect: designing a plan ({a['model']}{', ' + a['effort'] if a.get('effort') else ''})")
    norm, problems, warnings, cost = None, ["no answer"], [], 0.0
    rounds = max(0, int(a.get("rounds", 3)))
    async with ClaudeSDKClient(options=opts) as client:
        for rnd in range(rounds + 1):
            await client.query(message)
            raw = None
            async for msg in client.receive_response():
                meter.see(msg)
                act.see(msg)
                if isinstance(msg, ResultMessage):
                    cost += msg.total_cost_usd or 0.0
                    if msg.subtype == "error_max_budget_usd":
                        problems = ["the architect's budget ran out"]
                    if isinstance(msg.structured_output, dict):
                        raw = msg.structured_output
                    elif msg.result and (m := re.search(r"\{.*\}", msg.result, re.S)):
                        try:
                            raw = json.loads(m.group(0))
                        except ValueError:
                            raw = None
            if raw is None:
                problems = ["the answer was not the plan JSON"]
            else:
                norm, problems, warnings = architect_review(p, raw)
            act.add("check", ok=not problems, text="; ".join(problems + warnings)[:ACTIVITY_TEXT] or "plan ok")
            if norm is not None and not problems and not warnings:
                break
            if rnd == rounds or "budget ran out" in " ".join(problems):
                break
            store.log(f"architect: round {rnd + 1}: " + "; ".join(problems + warnings)[:300])
            message = ARCHITECT_FIX.format(problems="\n".join(f"- {x}" for x in problems + warnings))
    if norm is None or problems:
        store.log(f"architect: no usable plan ({'; '.join(problems)[:300]}); spent {cost:.2f} USD")
        raise RuntimeError("the architect gave no usable plan: " + "; ".join(problems))
    summary = str((raw or {}).get("summary") or goal).strip()
    with PLAN_LOCK:
        old = load_plan(p)
        if plan_active(old):
            raise RuntimeError("an approved plan is still going; close it on the plan card (or with the API) first")
        plan = {"status": "draft", "summary": summary[:500], "tasks": norm, "version": int((old or {}).get("version", 0)) + 1,
                "created": _now(), "warnings": warnings, "by": "architect", "goal": goal.strip()[:4000], "cost_usd": round(cost, 4)}
        _write_drafts(p, norm)
        save_plan(p, plan)
    store.log(f"architect: draft plan with {len(norm)} task(s)" + (f", {len(warnings)} warning(s)" if warnings else "")
              + f"; spent {cost:.2f} USD")
    return plan_view(p, plan) or {}


REVIEW_SCHEMA = {"type": "object", "required": ["verdict", "problems"], "properties": {
    "verdict": {"type": "string", "enum": ["pass", "fix", "ask"]},
    "problems": {"type": "array", "items": {"type": "string"}},
    "instructions": {"type": "string"},
    "question": {"type": "string"}}}

REVIEW_PROMPT = """You review one automated worker's finished attempt at a task. The project's checks already passed; you
judge only whether the change actually does what the task asks.

Task {id}: {title}
{body}

The worker's summary:
{summary}

Changed files (git status) and the diff against the start of the task (cut at {cut} characters; read any file in
full with Read/Grep/Glob, your working directory is the task's worktree):
{status}

{diff}

Verdict:
- "pass" unless you find a concrete problem. Don't nitpick style, naming or structure.
- "fix" when the task isn't done: its main point is missing or only stubbed (TODO, placeholder, "not implemented"),
  an acceptance criterion ("Done when") is clearly not met, tests were deleted or weakened just to pass, or the change
  breaks something the task said to keep. List each problem in `problems` (one line each, concrete: file, what) and
  say in `instructions` what to do.
- "ask" only when the task cannot be finished without a decision that is the user's (put it in `question`).
Answer with the JSON.
"""


def review_cfg() -> dict:
    r = dict(settings().get("review", {}))
    return {"enabled": bool(r.get("enabled", True)), "model": r.get("model") or "sonnet", "effort": r.get("effort") or None,
            "max_turns": int(r.get("max_turns", 20)), "budget_usd": float(r.get("budget_usd", 0.6)),
            "rounds": max(0, int(r.get("rounds", 1)))}


def attempt_diff(wt: Path, limit: int = 40000) -> tuple[str, str]:
    """(git status, diff of everything the worker changed: uncommitted edits and new files) for the reviewer."""
    git(["add", "-A", "-N", "--", ".", ":(exclude).code-graph", *[f":(exclude,glob){g}" for g in JUNK_GLOBS]], wt)
    _, status = git(["status", "--short"], wt)
    _, diff = git(["diff", "HEAD", "--", ".", ":(exclude).code-graph"], wt)
    return status.strip()[:4000] or "(no changes)", (diff[:limit] + ("\n… (cut)" if len(diff) > limit else ""))


async def review_attempt(p: Project, store: Store, task: Task, wt: Path, summary: str, meter: Meter) -> dict:
    """The reviewer's verdict on an attempt that passed the checks: {verdict: pass|fix|ask, problems, instructions,
    question}. Any failure of the review itself counts as pass (a review must never block finished work)."""
    _load_sdk()
    rc = review_cfg()
    act = Activity(p, task.id, "reviewer")
    try:
        status, diff = await asyncio.to_thread(attempt_diff, wt)
        prompt = REVIEW_PROMPT.format(id=task.id, title=task.title, body=task.body[:8000], summary=(summary or "")[-4000:],
                                      cut=40000, status=status, diff=diff)
        opts = options(p, model=rc["model"], effort=rc["effort"], max_turns=rc["max_turns"], max_budget_usd=rc["budget_usd"],
                       cwd=str(wt), allowed_tools=["Read", "Grep", "Glob"], permission_mode="default", setting_sources=["project"],
                       disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"],
                       output_format={"type": "json_schema", "schema": REVIEW_SCHEMA})
        act.add("start", tier="review", model=rc["model"], effort=rc["effort"])
        tok = meter.begin()
        verdict = None
        try:
            async for msg in query(prompt=prompt, options=opts):
                meter.see(msg)
                act.see(msg)
                if isinstance(msg, ResultMessage):
                    meter.state["tasks"].setdefault(task.id, {}).setdefault("runs", []).append(
                        {"tier": "review", "model": rc["model"], "effort": rc["effort"], "cost_usd": msg.total_cost_usd,
                         "turns": msg.num_turns, "error": msg.is_error, "tokens": usage_tokens(msg.usage),
                         "at": dt.datetime.now().isoformat(timespec="seconds"), **meter.end(tok)})
                    store.save(meter.state)
                    if isinstance(msg.structured_output, dict):
                        verdict = msg.structured_output
                    elif msg.result and (m := re.search(r"\{.*\}", msg.result, re.S)):
                        try:
                            verdict = json.loads(m.group(0))
                        except ValueError:
                            verdict = None
        finally:
            meter.release(tok)
    except Exception as e:  # the review is a safety net, never a gate that can break a run
        store.log(f"{task.id}: review skipped ({type(e).__name__}: {str(e)[:160]})")
        return {"verdict": "pass", "problems": []}
    if not isinstance(verdict, dict) or verdict.get("verdict") not in ("pass", "fix", "ask"):
        return {"verdict": "pass", "problems": []}
    act.add("review", ok=verdict["verdict"] == "pass", text=(verdict["verdict"] + ": " + "; ".join(verdict.get("problems") or []))[:ACTIVITY_TEXT])
    return verdict


def effort_ladder(tier: str, effort: str | None = None) -> list[tuple[str, str | None]]:
    """The rungs a task climbs while its attempts fail: (tier, effort) pairs. Within a tier: the chosen effort (the
    router's or the task file's, else the tier's own), then once more at the tier's `max_effort` if that is higher;
    then the next tier at its own effort, and so on. E.g. standard/medium → standard/high → hard/high → hard/xhigh."""
    tiers, out, seen = settings()["tiers"], [], set()
    if tier not in tiers:  # a tier the settings don't know: one rung as given (the worker falls back as before)
        return [(tier, effort if effort in EFFORTS else None)]
    t, e = tier, effort
    while t and t in tiers and t not in seen:
        seen.add(t)
        cfg = tiers[t]
        base = cfg.get("effort") or None
        top = cfg.get("max_effort") or base
        rank = lambda x: EFFORTS.index(x) if x in EFFORTS else -1
        start = e if e in EFFORTS else base
        if start and top and rank(start) > rank(top):
            start = top
        out.append((t, start))
        if top and top in EFFORTS and rank(top) > rank(start):
            out.append((t, top))
        t, e = cfg.get("next"), None
    return out


def rung_label(tier: str, effort: str | None) -> str:
    return f"{tier}/{effort}" if effort else tier


_TIER_EFFORT = object()  # work(): use the tier's own effort


async def work(p: Project, store: Store, task: Task, wt: Path, tier: str, brief: str, meter: Meter,
               effort=_TIER_EFFORT) -> tuple[bool, str]:
    _load_sdk()
    s = settings()
    t = dict(s["tiers"].get(tier) or s["tiers"]["standard"])
    if effort is not _TIER_EFFORT:
        t["effort"] = effort
    entry = meter.state["tasks"][task.id]
    gate_env = {k: str(v) for k, v in p.cfg.get("gates", {}).get("env", {}).items()}
    opts = options(p, role="worker", log=store.log, model=t["model"], effort=t.get("effort"), max_turns=int(t["max_turns"]), max_budget_usd=float(t["budget_usd"]),
                   cwd=str(wt), permission_mode="acceptEdits", setting_sources=["project"], can_use_tool=guard(wt), env=gate_env)
    summary = ""
    message = WORKER_PROMPT.format(id=task.id, title=task.title, brief=brief, body=task.body)
    attempts = int(s["limits"]["retries_per_tier"])
    attempt, net_try, session_id, delays = 1, 0, None, network_delays()
    reviews = 0
    tok: dict = {}
    act = Activity(p, task.id, "worker")
    while True:
        # A network error (reset, refused, dropped ...) ends the connection, not the task: wait, reconnect to the same
        # session and go on, instead of crashing the run (which counted against the task's retries).
        try:
            async with ClaudeSDKClient(options=replace(opts, resume=session_id) if session_id else opts) as client:
                while attempt <= attempts:
                    if meter.stop_reason():
                        return False, summary
                    tok = meter.begin()
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
                                                                 "at": dt.datetime.now().isoformat(timespec="seconds"), **meter.end(tok)})
                            try:
                                run = act.take_stats()
                                run.update(turns=msg.num_turns, cost_usd=msg.total_cost_usd, duration_ms=msg.duration_ms, tier=tier, attempt=attempt)
                                merge_tool_stats(entry, run)
                            except Exception as e:  # stats must never break a run
                                store.log(f"{task.id}: tool stats failed ({e})")
                            store.save(meter.state)
                    meter.release(tok)  # (already released by end() unless the answer had no result)
                    ok, failure = await check_worktree(p, store, task, wt, meter)
                    if not ok and ENV_FAILURE in failure:
                        act.add("check", ok=False, text=failure[-ACTIVITY_TEXT:])
                        raise RuntimeError("the checks could not run (the environment, not the task): " + failure.splitlines()[-1][:300])
                    gates = p.cfg.get("gates", {}).get("quick", [])
                    act.add("check", ok=ok, gates=len(gates), text="; ".join(gates) if ok else failure[-ACTIVITY_TEXT:])
                    if ok and not _DECISION.search(summary or "") and review_cfg()["enabled"] and reviews < review_cfg()["rounds"]:
                        verdict = await review_attempt(p, store, task, wt, summary, meter)
                        if verdict.get("verdict") == "ask" and verdict.get("question"):
                            return True, f"{summary}\nNEEDS_DECISION: {verdict['question']}"
                        if verdict.get("verdict") == "fix":
                            reviews += 1
                            problems = "\n".join(f"- {x}" for x in verdict.get("problems") or []) or "- (see instructions)"
                            entry["last_review"] = (problems + "\n" + str(verdict.get("instructions") or ""))[:1500]
                            store.save(meter.state)
                            store.log(f"{task.id}: the reviewer sent it back: {problems.splitlines()[0][2:160]}")
                            attempt += 1
                            if attempt > attempts:
                                return False, summary
                            act.add("retry", attempt=attempt, text="the reviewer sent it back")
                            message = ("The checks passed, but a reviewer compared your change with the task and found it isn't "
                                       f"done yet:\n{problems}\n\nWhat to do: {verdict.get('instructions') or 'fix the points above'}\n\n"
                                       "Fix this (or, if the reviewer is wrong, explain why in your summary), then finish as before.")
                            continue
                    if ok:
                        return True, summary
                    store.log(f"{task.id}: checks failed")
                    entry["last_failure"] = failure[-1500:]
                    store.save(meter.state)
                    if meter.stop_reason():
                        return False, summary
                    message = f"The checks failed. Fix the cause (not the check), then finish as before.\n\n{failure}"
                    foreign = foreign_failures(wt, p.cfg["repo"]["base"], failure)
                    if foreign:
                        message += ("\n\nNote: the failing tests are in files this task didn't change: " + ", ".join(foreign)
                                    + ". If such a test encodes an assumption the project has outgrown, fix that test "
                                      "minimally (and say so); otherwise fix your code.")
                    attempt += 1
                    if attempt <= attempts:
                        act.add("retry", attempt=attempt, text="checks failed; the worker is asked to fix the cause")
            return False, summary
        except Exception as e:
            meter.release(tok)
            if not is_transient(e) or net_try >= len(delays):
                raise
            store.log(f"{task.id}: network error ({str(e)[:120] or type(e).__name__}); retrying in {delays[net_try]:g}s "
                      f"(try {net_try + 1}/{len(delays)})")
            act.add("network_retry", text=str(e)[:200] or type(e).__name__, delay_s=delays[net_try], resume=bool(session_id))
            if session_id:
                message = "A network error interrupted you. Continue the task exactly where you left off."
            await asyncio.sleep(delays[net_try])
            net_try += 1


_FAIL_FILE = re.compile(r"(?m)^(?:FAILED |ERROR )?([\w./\\-]+\.(?:py|rs|ts|tsx|js|go|java|kt))(?::\d+)?::")


def foreign_failures(wt: Path, base: str, failure: str) -> list[str]:
    """Test files named in a check failure (pytest / cargo / jest style `path::test`) that this worktree's branch hasn't
    changed since `base` (committed or not): failures the task likely didn't cause."""
    files = list(dict.fromkeys(m.group(1).replace("\\", "/") for m in _FAIL_FILE.finditer(failure or "")))
    if not files:
        return []
    changed = set()
    for args in (["diff", "--name-only", f"{base}...HEAD"], ["diff", "--name-only", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        code, out = git(args, wt)
        if code == 0:
            changed |= {l.strip() for l in out.splitlines() if l.strip()}
    return [f for f in files if f not in changed][:10]


async def check_worktree(p: Project, store: Store, task: Task, wt: Path, meter: Meter) -> tuple[bool, str]:
    """The quick checks of a worker's attempt, off the event loop (other tasks of a parallel run keep streaming), at
    most `meter.gate_sem` at a time. Unresolved merge conflicts fail before any check runs."""
    def go() -> tuple[bool, str]:
        left = unmerged_files(wt)
        if left:
            return False, ("The checks failed: unresolved merge conflicts remain in " + ", ".join(left[:30])
                           + ". Resolve them and `git add` the files.")
        return run_gates(p, store, wt, "quick", task.crates or changed_crates(wt, p.cfg["repo"]["base"]))
    if getattr(meter, "gate_sem", None) is None:
        return await asyncio.to_thread(go)
    async with meter.gate_sem:
        return await asyncio.to_thread(go)


async def in_repo(meter: Meter, fn, *args):
    """Run a blocking git step in a thread, one at a time per run (worktree add, merges, commits)."""
    lock = getattr(meter, "repo_lock", None) or threading.Lock()  # (a bare meter in tests has none)

    def go():
        with lock:
            return fn(*args)
    return await asyncio.to_thread(go)


# Build/test leftovers a worker's run leaves in the worktree that must never be committed even when the repository
# doesn't ignore them (in parallel branches they turn into binary merge conflicts). Tracked files are unaffected.
JUNK_GLOBS = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**")


def commit(p: Project, task: Task, wt: Path, tiers: str) -> str:
    # .code-graph/ is the code-graph MCP index: never commit it (info/exclude is per common git dir, so don't rely on it)
    git(["add", "-A", "--", ".", ":(exclude).code-graph", *[f":(exclude,glob){g}" for g in JUNK_GLOBS]], wt)
    git(["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", ".code-graph"], wt)
    merging = bool(git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt)[1].strip())
    if git(["diff", "--cached", "--quiet"], wt)[0] == 0 and not merging:  # nothing staged (any locale): keep HEAD
        return git(["rev-parse", "--short", "HEAD"], wt)[1].strip()
    code, out = git(["commit", "-q", "-m", f"{task.id}: {task.title}\n\nAutomated by AgentDesk ({tiers}).\n\n{p.cfg['repo']['co_author']}"], wt)
    if code != 0:
        raise RuntimeError(out)
    return git(["rev-parse", "--short", "HEAD"], wt)[1].strip()


_DECISION = re.compile(r"(?m)^[ \t>*_-]*NEEDS_DECISION:[ \t]*")
_NO_CHANGES = re.compile(r"(?m)^[ \t>*_-]*NO_CHANGES_NEEDED:")


def worktree_changed(wt: Path, start_sha: str) -> bool:
    """The worker left something to commit: new commits since `start_sha`, uncommitted changes, or a merge in progress."""
    if git(["rev-parse", "HEAD"], wt)[1].strip() != start_sha:
        return True
    code, out = git(["status", "--porcelain", "--", ".", ":(exclude).code-graph", *[f":(exclude,glob){g}" for g in JUNK_GLOBS]], wt)
    return code != 0 or bool(out.strip()) or (wt / ".git").is_file() and bool(git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt)[1].strip())


def decision_needed(summary: str, wt: Path, start_sha: str) -> str:
    """The question a finished attempt leaves for the user, or "" when the task is really done: the worker asked
    (`NEEDS_DECISION:`), or it changed nothing without saying why nothing was needed (`NO_CHANGES_NEEDED:`)."""
    m = _DECISION.search(summary or "")
    if m:
        q = summary[m.end():].split("\nNOTES:")[0].strip()
        return (q or "The worker stopped for a decision without stating it; see its summary.")[:3000]
    if not worktree_changed(wt, start_sha) and not _NO_CHANGES.search(summary or ""):
        tail = (summary or "").strip()[-1500:] or "(no summary)"
        return ("The worker finished without changing anything and didn't say why no change is needed. Decide how to go "
                "on (answer with what it should do, or say no change is needed). Its summary:\n" + tail)
    return ""


_notify_errors: set[str] = set()


def notify(p: Project, kind: str, text: str) -> bool:
    """Send a short notification (Telegram and/or webhook from [notify]) in the background; False when nothing is
    configured or the event is off. A failure is logged once per reason and never breaks the caller."""
    n = settings().get("notify", {}) or {}
    if kind not in (n.get("events") or []):
        return False
    token = os.environ.get(str(n.get("telegram_token_env") or "AGENTDESK_TELEGRAM_TOKEN"), "").strip()
    chat_id, hook = str(n.get("telegram_chat_id") or "").strip(), str(n.get("webhook_url") or "").strip()
    if not ((token and chat_id) or hook):
        return False
    msg = f"AgentDesk · {p.name}\n{text}"[:3500]

    def send():
        import urllib.request
        posts = []
        if token and chat_id:
            posts.append((f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": msg,
                                                                              "disable_web_page_preview": True}))
        if hook:
            posts.append((hook, {"project": p.slug, "kind": kind, "text": msg}))
        for url, body in posts:
            try:
                req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as e:  # noqa: BLE001 — a notification must never break a run
                why = f"{type(e).__name__}: {str(e)[:120]}".replace(token, "***") if token else f"{type(e).__name__}"
                if why not in _notify_errors:
                    _notify_errors.add(why)
                    try:
                        Store(p).log(f"notify failed ({'telegram' if 'telegram' in url else 'webhook'}): {why}")
                    except OSError:
                        pass

    threading.Thread(target=send, daemon=True, name=f"notify-{p.slug}").start()
    return True


def answer_task(p: Project, task_id: str, answer: str, who: str = "user") -> None:
    """Record the answer to a task waiting for a decision (or any open task) in its file and put it back in the queue;
    its worktree and branch are kept, so the next run goes on from there."""
    answer = str(answer or "").strip()
    if not answer:
        raise ValueError("the answer is empty")
    f = p.tasks_dir / f"{task_id}.md"
    if not f.is_file():
        raise KeyError(task_id)
    store = Store(p)
    state = store.load()
    e = state["tasks"].setdefault(task_id, {})
    if e.get("status") == "done":
        raise ValueError(f"{task_id} is done; queue a follow-up task instead")
    q = e.pop("question", "")
    text = f.read_text(encoding="utf-8").rstrip()
    block = f"\n\n## Decision ({_now()[:10]}, {who})\n\n" + (f"Question: {q.splitlines()[0][:300]}\n\n" if q else "") + answer + "\n"
    write_atomic(f, text + block)
    e.setdefault("decisions", []).append({"at": _now(), "question": q[:1000], "answer": answer[:3000], "by": who})
    e.update(status="pending", crashes=0, requeued=now_iso())
    e.pop("last_failure", None)
    store.save(state)
    store.log(f"{task_id}: decision recorded ({who}); back in the queue")


def keep_notes(p: Project, task: Task, summary: str) -> None:
    m = re.search(r"NOTES:\s*(.*)", summary, re.S)
    if not m or m.group(1).strip().lower().startswith("none"):
        return
    with p.path("notes.md").open("a", encoding="utf-8") as f:
        for l in [l.strip("-• ").strip() for l in m.group(1).strip().splitlines() if l.strip()][:3]:
            f.write(f"- ({task.id}) {l}\n")


def large_files_note(p: Project, store: Store, task: Task, wt: Path, entry: dict) -> str:
    """The report's warning about large files the agent committed ([hygiene] large_file_mb), "" if none."""
    try:
        big = large_files(wt, p.cfg["repo"]["base"], float(hygiene_cfg(p)["large_file_mb"]))
    except Exception:  # a warning must never fail a finished task
        return ""
    if not big:
        return ""
    entry["large_files"] = [{"path": n, "bytes": b} for n, b in big[:20]]
    store.log(f"{task.id}: WARNING: {len(big)} large file(s) committed: " + ", ".join(n for n, _ in big[:5]))
    return ("## Warning: large files committed\n\n" + "".join(f"- `{n}` ({b / 1048576:.1f} MB)\n" for n, b in big[:20])
            + "\nLarge files bloat the repository and every clone for good; remove them or use Git LFS.\n\n")


def ready(task: Task, state: dict) -> bool:
    if state["tasks"].get(task.id, {}).get("status") in ("done", "blocked", "needs_decision"):
        return False
    return all(state["tasks"].get(d, {}).get("status") == "done" for d in task.depends)


async def do_task(p: Project, store: Store, task: Task, meter: Meter) -> None:
    state = meter.state
    entry = state["tasks"].setdefault(task.id, {"title": task.title})
    entry.update(status="routing", started=dt.datetime.now().isoformat(timespec="seconds"))
    store.save(state)
    plan = await route(p, store, task, meter)
    effort = plan.get("effort") if plan.get("effort") in EFFORTS else (task.effort if task.effort in EFFORTS else None)
    entry.update(tier=plan["tier"], effort=effort, why=plan.get("why", ""), brief=plan.get("brief", ""), status="working")
    if plan.get("crates") and not task.crates:
        task.crates = [c for c in plan["crates"] if re.fullmatch(r"[A-Za-z0-9_-]+", c)]
    store.save(state)
    store.log(f"{task.id}: routed to {rung_label(plan['tier'], effort)} ({plan.get('why', '')})")
    wt = await in_repo(meter, ensure_worktree, p, store, task, state)
    start_sha = git(["rev-parse", "HEAD"], wt)[1].strip()
    preamble = await in_repo(meter, worktree_conflicts, p, task, wt, state)
    if preamble:
        Activity(p, task.id, "orchestrator").add("merge_conflict", text=preamble[:ACTIVITY_TEXT])
    ladder, tried = effort_ladder(plan["tier"], effort), []
    for step, (tier, eff) in enumerate(ladder):
        if step:
            store.log(f"{task.id}: escalating to {rung_label(tier, eff)}")
            Activity(p, task.id, "orchestrator").add("escalate", tier=tier, effort=eff)
            entry.update(tier=tier, effort=eff)
        tried.append(rung_label(tier, eff))
        ok, summary = await work(p, store, task, wt, tier, preamble + plan["brief"], meter, eff)
        if ok:
            question = decision_needed(summary, wt, start_sha)
            if question:
                entry.update(status="needs_decision", question=question, finished=dt.datetime.now().isoformat(timespec="seconds"),
                             branch=f"auto/{task.id}")
                p.path("reports").mkdir(exist_ok=True)
                p.path("reports").joinpath(f"{task.id}.md").write_text(
                    f"# {task.id}: {task.title}\n\nWaiting for a decision (tiers {' → '.join(tried)}); nothing was committed, the "
                    f"worktree keeps any work in progress.\n\n## Needs your decision\n\n{question}\n\n## Worker summary\n\n{summary}\n",
                    encoding="utf-8")
                store.save(state)
                store.log(f"{task.id}: needs a decision: {question.splitlines()[0][:200]}")
                notify(p, "needs_decision", f"❓ {task.id} {task.title}: needs your decision\n{question[:1200]}")
                Activity(p, task.id, "orchestrator").add("decision", text=question[:ACTIVITY_TEXT])
                return
            sha = await in_repo(meter, commit, p, task, wt, " → ".join(tried))
            keep_notes(p, task, summary)
            # crashes and last_failure describe the unfinished work; the history stays in runs[]
            entry.update(status="done", commit=sha, branch=f"auto/{task.id}", finished=dt.datetime.now().isoformat(timespec="seconds"),
                         crashes=0, last_failure="")
            warn = large_files_note(p, store, task, wt, entry)
            p.path("reports").mkdir(exist_ok=True)
            p.path("reports").joinpath(f"{task.id}.md").write_text(
                f"# {task.id}: {task.title}\n\nBranch `auto/{task.id}` at {sha}; tiers {' → '.join(tried)}.\n\n{warn}## Brief\n\n{plan['brief']}\n\n"
                f"## Worker summary\n\n{summary}\n", encoding="utf-8")
            store.save(state)
            store.log(f"{task.id}: done at {sha}")
            return
        if meter.stop_reason():
            entry["status"] = "paused"
            store.save(state)
            return
    entry["status"] = "blocked"
    store.save(state)
    store.log(f"{task.id}: blocked after {' → '.join(tried)}")
    fail = (entry.get("last_failure") or "").strip().splitlines()
    notify(p, "blocked", f"✗ {task.id} {task.title}: blocked after {' → '.join(tried)}" + (f"\n{fail[0][:300]}" if fail else ""))


def run_cfg(p: Project | None = None) -> dict:
    """[run] from settings.toml with the project's [run] on top: max_parallel (>= 1), gate_parallel (0 = all),
    integrate_after, recheck_seconds (> 0)."""
    cfg = dict(settings().get("run", {}))
    if p is not None:
        cfg.update(p.cfg.get("run", {}) or {})
    try:
        n = max(1, int(cfg.get("max_parallel", 1)))
    except (TypeError, ValueError):
        n = 1
    try:
        g = max(0, int(cfg.get("gate_parallel", 0)))
    except (TypeError, ValueError):
        g = 0
    try:
        r = float(cfg.get("recheck_seconds", 30))
    except (TypeError, ValueError):
        r = 30.0
    return {"max_parallel": n, "gate_parallel": g, "integrate_after": bool(cfg.get("integrate_after", False)),
            "recheck_seconds": r if r > 0 else 30.0}


class RunLock:
    """One run process per project: an exclusive OS lock on `run.lock` in the project folder, released by the OS when
    the process ends (also when it is killed), so a stale lock can't exist. `held_elsewhere(p)` asks without taking it."""

    def __init__(self, p: Project, name: str = "run"):
        self.path = p.path(f"{name}.lock")
        self.f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self) -> None:
        if self.f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.f.close()
        self.f = None

    def __enter__(self):
        if not self.acquire():
            raise RunBusy(f"another run of this project is going (lock {self.path})")
        return self

    def __exit__(self, *exc):
        self.release()

    @staticmethod
    def held_elsewhere(p: Project, name: str = "run") -> bool:
        probe = RunLock(p, name)
        if probe.acquire():
            probe.release()
            return False
        return True


class RunBusy(RuntimeError):
    """A run (or an integration) of the project is already going."""


def _flagged(e: dict, retry_crashed: int) -> bool:
    """A task the auto-runner leaves for a human: crashed too often, or its checks failed last time."""
    return int(e.get("crashes", 0)) > retry_crashed or classify_failure(e.get("last_failure"), "pending") == "gate"


async def _guarded_task(p: Project, store: Store, task: Task, meter: Meter) -> None:
    """do_task for a parallel run: an unexpected error ends this task (back to pending, counted as a crash), not the
    whole run with its other tasks."""
    try:
        await do_task(p, store, task, meter)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        entry = meter.state["tasks"].setdefault(task.id, {"title": task.title})
        entry.update(status="pending", last_failure=f"worker exited unexpectedly: {type(e).__name__}: {str(e)[:500]}",
                     crashes=int(entry.get("crashes", 0)) + 1)
        store.save(meter.state)
        store.log(f"{task.id}: failed with {type(e).__name__}: {str(e)[:300]}")
        Activity(p, task.id, "orchestrator").add("crash", text=f"{type(e).__name__}: {e}")


async def run_dag(p: Project, max_parallel: int | None = None, max_tasks: int | None = None, auto: bool = False,
                  budget_usd: float | None = None) -> dict:
    """The orchestrator: run the project's task graph with up to `max_parallel` tasks at once. A task starts as soon as
    everything it depends on is done; among ready tasks the one on the longest remaining chain goes first. A blocked
    task only holds back what depends on it. Tasks added while the run goes are picked up. `max_tasks` caps how many
    start; `auto` (the auto-runner) skips tasks flagged for a human (see attention_items). Returns a summary."""
    from . import dag
    store = Store(p)
    state = store.load()
    state["budget_base"] = state.get("spent_usd", 0.0)  # the run budget counts from now
    meter = Meter(p, store, state)
    meter.budget_override = budget_usd
    cfg = run_cfg(p)
    n = max(1, int(max_parallel or cfg["max_parallel"]))
    running: dict[str, asyncio.Task] = {}
    store.owned = set()  # filled as tasks start: see Store.save
    g = cfg["gate_parallel"]
    meter.gate_sem = asyncio.Semaphore(g) if 0 < g < n else None
    retry = autorunner_cfg(p)["retry_crashed"] if auto else 0
    started: list[str] = []
    started_at: dict[str, str] = {}
    said: set[str] = set()
    stop = None
    run_before = meter.mark()
    cap_n = int(hygiene_cfg(p)["max_worktrees"])
    if 0 < cap_n < n:
        store.log(f"note: [hygiene] max_worktrees = {cap_n} allows fewer unfinished tasks than max_parallel = {n}; "
                  f"at most {cap_n} run at once (raise max_worktrees in [hygiene] for more)")
    store.log(f"run started: up to {n} task(s) at once" + (f", checks {g} at a time" if meter.gate_sem else ""))
    try:
        while True:
            store.owned = set(running)
            store.save(state)  # takes in what others changed meanwhile (an answer, a retry) before choosing
            tasks = {t.id: t for t in load_tasks(p)}
            nodes = dag.nodes_from_tasks(tasks.values())
            errs = dag.validate(nodes, [tid for tid, e in state["tasks"].items() if e.get("status") == "done"])
            for e in errs:
                if f"err|{e}" not in said:
                    said.add(f"err|{e}")
                    store.log(f"task graph: {e} (the tasks involved can't start)")
            stop = stop or meter.stop_reason()
            if not stop and (max_tasks is None or len(started) < max_tasks):
                # Each task at most once per run (one that ends not done waits for the next run), unless someone
                # put it back in the queue (a retry, an answer) after this run started it.
                skip = {t for t in started if t in running or state["tasks"].get(t, {}).get("requeued", "") <= started_at[t]}
                if auto:
                    skip |= {tid for tid in tasks if _flagged(state["tasks"].get(tid, {}), retry)}
                for tid in dag.ready(nodes, state, skip):
                    if len(running) >= n or (max_tasks is not None and len(started) >= max_tasks):
                        break
                    cap = worktree_cap_reason(p, tid, state)
                    if cap:
                        if f"cap|{cap}" not in said:
                            said.add(f"cap|{cap}")
                            store.log(f"{tid} waits: {cap}")
                        break
                    store.owned.add(tid)
                    running[tid] = asyncio.create_task(_guarded_task(p, store, tasks[tid], meter), name=f"task-{tid}")
                    if tid in started:
                        store.log(f"{tid}: put back in the queue; starting it again")
                    else:
                        started.append(tid)
                    started_at[tid] = now_iso()
                    if len(running) > 1 or n > 1:
                        store.log(f"{tid}: started ({len(running)} of {n} slots busy)")
            if not running:
                break
            # Wake up when a task ends, or after recheck_seconds to pick up work queued meanwhile.
            finished, _ = await asyncio.wait(running.values(), timeout=cfg["recheck_seconds"],
                                             return_when=asyncio.FIRST_COMPLETED)
            for tid in [t for t, job in running.items() if job in finished]:
                running.pop(tid)
                store.owned.discard(tid)
    finally:
        for job in running.values():  # only on cancellation / a crash of the loop itself
            job.cancel()
    statuses = {tid: state["tasks"].get(tid, {}).get("status", "pending") for tid in started}
    nodes = dag.nodes_from_tasks(load_tasks(p))
    stuck = dag.stuck(nodes, state)
    left = [] if stop or max_tasks is not None else [t for t in dag.ready(nodes, state, set(started)) if not (
        auto and _flagged(state["tasks"].get(t, {}), retry))]
    summary = {"started": started, "done": [t for t, s_ in statuses.items() if s_ == "done"],
               "blocked": [t for t, s_ in statuses.items() if s_ == "blocked"],
               "needs_decision": [t for t, s_ in statuses.items() if s_ == "needs_decision"],
               "stuck": sorted(stuck), "stopped": stop, "not_started": left}
    if left:
        why = next((worktree_cap_reason(p, t, state) for t in left if worktree_cap_reason(p, t, state)), None)
        store.log(f"WARNING: {len(left)} ready task(s) were not started: {', '.join(left)}"
                  + (f" ({why})" if why else ""))
    share_usage(state, started, run_before, meter.mark())
    store.owned = {t for t in started if state["tasks"].get(t, {}).get("usage_share_pct") is not None}
    store.save(state)
    store.owned = None
    if stop:
        paused = [t for t, e in state["tasks"].items() if e.get("status") == "paused"]
        store.log(f"stopping: {stop}" + (f"; paused: {', '.join(paused)}" if paused else "")
                  + ". Run all goes on (with a new budget); nothing restarts by itself.")
        summary["paused"] = paused
    store.log(f"run finished: {len(started)} task(s) started, {len(summary['done'])} done, {len(summary['blocked'])} blocked"
              + (f", {len(summary['needs_decision'])} waiting for a decision" if summary["needs_decision"] else "")
              + (f", {len(stuck)} waiting on a blocked task" if stuck else "")
              + f"; project total {state.get('spent_usd', 0):.2f} USD")
    if started:
        spent = state.get("spent_usd", 0.0) - state.get("budget_base", 0.0)
        parts = [f"{len(summary['done'])} done"] + [f"{len(summary[k])} {w}" for k, w in (("blocked", "blocked"),
                 ("needs_decision", "waiting for your decision"), ("stuck", "waiting on those")) if summary.get(k)]
        if stop:
            notify(p, "paused", f"⏸ Run paused: {stop}. {', '.join(parts)}; paused: {', '.join(summary.get('paused', [])) or '-'}. "
                                "Run all goes on.")
        else:
            notify(p, "run_finished", f"■ Run finished: {', '.join(parts)} of {len(started)} started; ${spent:.2f} this run.")
    if summary["done"] and cfg["integrate_after"] and not stop:
        try:
            summary["integration"] = await asyncio.to_thread(integrate, p)
        except Exception as e:  # the run's work is committed either way
            store.log(f"integration failed: {type(e).__name__}: {e}")
    return summary


def share_usage(state: dict, task_ids: list[str], before: dict | None, after: dict | None) -> None:
    """A parallel run's account usage (percent points of the window, from the readings before and after the run) split
    over its tasks by what each spent in the run (cost_usd from the SDK): tasks[id].usage_share_pct, and the run's total
    in state.last_run_usage. Nothing when there's no newer reading."""
    if not before or not after or before.get("at") == after.get("at") or not task_ids:
        return
    total = max(0.0, round((after["utilization"] - before["utilization"]) * 100, 2))
    since = before.get("at") or ""
    costs = {}
    for tid in task_ids:
        runs = state["tasks"].get(tid, {}).get("runs", [])
        costs[tid] = sum((r.get("cost_usd") or 0.0) for r in runs if str(r.get("at", "")) >= since)
    whole = sum(costs.values())
    for tid, c in costs.items():
        if whole > 0:
            state["tasks"][tid]["usage_share_pct"] = round(total * c / whole, 2)
    state["last_run_usage"] = {"used_pct": total, "tasks": len(task_ids), "from": before.get("at"), "to": after.get("at")}


async def run_queue(p: Project, only: str | None = None, max_tasks: int | None = None, max_parallel: int | None = None,
                    auto: bool = False, budget_usd: float | None = None) -> None:
    """`run PROJECT [TASK]`: one task (whatever its dependencies say), or the task graph (run_dag)."""
    if not only:
        await run_dag(p, max_parallel, max_tasks, auto, budget_usd)
        return
    store = Store(p)
    state = store.load()
    state["budget_base"] = state.get("spent_usd", 0.0)
    meter = Meter(p, store, state)
    meter.budget_override = budget_usd
    task = next((t for t in load_tasks(p) if t.id == only), None)
    if task is None:
        store.log(f"run: no task {only}")
        return
    if (why := meter.stop_reason()):
        store.log(f"stopping before {task.id}: {why}")
        return
    store.owned = {task.id}
    await do_task(p, store, task, meter)
    store.log(f"run finished: 1 task(s); project total {state.get('spent_usd', 0):.2f} USD")


# ----------------------------------------------------------------------------- integration: one branch with all the work

INTEGRATION = "_integration"            # the integration worktree's folder (never a task id: those don't start with _)
INTEGRATION_BRANCH = "auto/integration"


def integrate(p: Project, branch: str = INTEGRATION_BRANCH, final: bool = True) -> dict:
    """Merge every finished task's branch that isn't in the base branch yet into `branch` (made fresh from the base,
    in its own worktree), in dependency order, then run the final checks there. A branch that conflicts is skipped
    with everything built on it. Never touches the base branch or the checkout. Writes reports/integration.md."""
    from . import dag
    store = Store(p)
    state = store.load()
    base = p.cfg["repo"]["base"]
    tasks = load_tasks(p)
    nodes = dag.nodes_from_tasks(tasks)
    order = dag.topo_order(nodes)
    titles = {t.id: t.title for t in tasks}
    wt = Path(p.cfg["repo"]["worktrees"]) / INTEGRATION
    if wt.exists():
        git(["merge", "--abort"], wt)
        code, out = git(["checkout", "-q", "-B", branch, base], wt)
        if code != 0:
            raise RuntimeError(f"can't reset the integration worktree: {out.strip()[:300]}")
        git(["reset", "-q", "--hard", base], wt)
        git(["clean", "-q", "-fd"], wt)
    else:
        wt.parent.mkdir(parents=True, exist_ok=True)
        code, out = git(["worktree", "add", "-q", "-B", branch, str(wt), base], p.repo)
        if code != 0:
            raise RuntimeError(f"git worktree add failed: {out.strip()[:300]}")
    merged, skipped, already, covered = [], [], [], {}
    bad: set[str] = set()
    done = [tid for tid in order if state["tasks"].get(tid, {}).get("status") == "done"
            and branch_exists(p, state["tasks"][tid].get("branch") or f"auto/{tid}")]
    br_of = {tid: state["tasks"][tid].get("branch") or f"auto/{tid}" for tid in done}
    for tid in done:  # a branch another finished branch already contains comes in with it (conflicts resolved there)
        for other in done:
            if other != tid and tid in dag.ancestors(nodes, other) and \
                    git(["merge-base", "--is-ancestor", br_of[tid], br_of[other]], p.repo)[0] == 0:
                covered[tid] = other
                break
    for tid in done:
        br = br_of[tid]
        if tid in covered:
            continue
        if tid in bad:
            skipped.append({"task": tid, "reason": "builds on a task that conflicted"})
            continue
        if git(["merge-base", "--is-ancestor", br, "HEAD"], wt)[0] == 0:
            already.append(tid)
            continue
        code, out = git(["merge", "--no-ff", "--no-edit", "-m", f"integrate {tid}: {titles.get(tid, '')}", br], wt)
        if code != 0:
            files = unmerged_files(wt)
            git(["merge", "--abort"], wt)
            skipped.append({"task": tid, "reason": "merge conflict" if files else out.strip()[:300], "files": files[:30]})
            bad |= dag.descendants(nodes, [tid]) | {tid}
            store.log(f"integration: {tid} conflicts ({', '.join(files[:5]) or out.strip()[:120]}); skipped with what builds on it")
            continue
        merged.append(tid)
    for tid in done:  # report what came in through a branch built on it
        if tid in covered:
            top = covered[tid]
            while top in covered:
                top = covered[top]
            (merged if top in merged else already if top in already else skipped).append(
                tid if top in merged or top in already else {"task": tid, "reason": f"comes with {top}, which was skipped"})
    merged.sort(key=order.index)
    already.sort(key=order.index)
    ok, failure = (True, "")
    if final and merged:
        ok, failure = run_gates(p, store, wt, "final", None)
    sha = git(["rev-parse", "--short", "HEAD"], wt)[1].strip()
    res = {"branch": branch, "commit": sha, "merged": merged, "already_in": already, "skipped": skipped,
           "via": {t: c for t, c in covered.items()},
           "checks": None if not (final and merged) else ok, "failure": failure[-3000:] if failure else "",
           "at": _now(), "worktree": str(wt)}
    state = store.load()
    state["integration"] = {k: v for k, v in res.items() if k != "failure"} | {"failure": failure[-1500:]}
    store.save(state)
    lines = [f"# Integration: `{branch}` at {sha}", "",
             f"Base `{base}`. Merged in dependency order: {', '.join(merged) or 'nothing new'}.", ""]
    if already:
        lines.append(f"Already contained: {', '.join(already)}.")
    for s_ in skipped:
        lines.append(f"- Skipped {s_['task']}: {s_['reason']}" + (f" ({', '.join(s_.get('files') or [])})" if s_.get("files") else ""))
    if res["checks"] is not None:
        lines += ["", "Final checks: " + ("passed." if ok else "FAILED."), *(["", "```", failure[-3000:], "```"] if failure else [])]
    p.path("reports").mkdir(exist_ok=True)
    p.path("reports").joinpath("integration.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    notify(p, "integration", f"⇢ Integration {branch} at {sha}: merged {len(merged)}, skipped {len(skipped)}"
           + ("" if res["checks"] is None else f"; final checks {'passed' if ok else 'FAILED'}"))
    store.log(f"integration: {branch} at {sha}; merged {len(merged)}, skipped {len(skipped)}"
              + ("" if res["checks"] is None else f"; final checks {'passed' if ok else 'FAILED'}"))
    return res


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


def run_async_result(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- export / import of a project

EXPORT_FORMAT = 1
EXPORT_DIR = ".agentdesk"
MAX_FILE_BYTES = 20 * 1024 * 1024      # one file in an import
MAX_TOTAL_BYTES = 100 * 1024 * 1024    # everything in an import
MAX_ENTRIES = 5000
_EXPORT_TOP = ("project.toml", "notes.md", "state.json", "agentdesk.log", "chat_memory.md", "chat_rules.md", "chat_archive.jsonl")
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
    res = _toml_edit(text, {("", "account"), ("repo", "path"), ("repo", "worktrees"), ("git", "account")}, blank_secret_env=True)
    tomllib.loads(res)
    return res


def _localize_toml(text: str, name: str, repo: str, worktrees: str, account: str | None) -> str:
    text = _toml_edit(text, {("", "account"), ("", "name"), ("repo", "path"), ("repo", "worktrees"), ("git", "account")}, blank_secret_env=True)
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


EXPORT_BRANCH_PREFIX = "agentdesk-export/"


def export_to_branch(p: Project, include_logs: bool = False, push: bool = False, branch: str | None = None) -> dict:
    """Commit the export onto its own branch `agentdesk-export/<slug>` of the project's repo, without touching the
    checkout: the files are staged with a temporary index/work tree and the commit is made with plumbing
    (`write-tree`, `commit-tree`, `update-ref`), so the current branch, the working tree and uncommitted changes
    stay as they are. Layout: the export sits at the BRANCH ROOT (manifest.json, project.toml, tasks/ ...), so
    importing needs only the clone URL + branch. Each export is a child of the branch tip (fast-forward, never
    forced); unchanged content makes no commit. `push=True` pushes only that branch to origin (-u, never forced)."""
    import tempfile
    repo = p.repo
    branch = (branch or EXPORT_BRANCH_PREFIX + p.slug).strip()
    ref = f"refs/heads/{branch}"
    genv = {"GIT_TERMINAL_PROMPT": "0"}

    def g(*a, env=None, timeout=120):
        code, out = run(["git", *a], repo, env={**genv, **(env or {})}, timeout=timeout)
        return code, out.strip()
    if not repo.is_dir() or g("rev-parse", "--git-dir")[0] != 0:
        return {"ok": False, "message": f"{repo} is not a git repository"}
    if g("check-ref-format", "--branch", branch)[0] != 0 or branch.startswith("-"):
        return {"ok": False, "message": f"'{branch}' is not a valid branch name"}
    if g("rev-parse", "--abbrev-ref", "HEAD")[1] == branch:
        return {"ok": False, "message": f"'{branch}' is checked out in the repo; switch to another branch first"}
    code, tip = g("rev-parse", "--verify", "-q", ref + "^{commit}")
    tip = tip if code == 0 else ""
    files = export_files(p, include_logs)

    with tempfile.TemporaryDirectory(prefix="agentdesk-export-") as tmp:
        work, index = Path(tmp) / "w", Path(tmp) / "index"
        work.mkdir()
        env = {"GIT_WORK_TREE": str(work), "GIT_INDEX_FILE": str(index)}

        def tree_for(manifest: bytes) -> str:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            index.unlink(missing_ok=True)
            (work / "manifest.json").write_bytes(manifest)
            for rel, data in files.items():
                t = work / rel
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_bytes(data)
            if g("add", "-A", "-f", env=env)[0] != 0:
                return ""
            return g("write-tree", env=env)[1]
        tree, old_tree = "", g("rev-parse", "--verify", "-q", tip + "^{tree}")[1] if tip else ""
        if tip:  # unchanged content keeps the old manifest (its timestamp) so there is nothing to commit
            code, old = run(["git", "show", f"{tip}:manifest.json"], repo)
            try:
                if code == 0 and json.loads(old).get("includes_logs") == include_logs:
                    tree = tree_for(old.encode("utf-8"))
            except ValueError:
                pass
        if not tree or tree != old_tree:
            tree = tree_for(_manifest(p, files, include_logs))
    if not tree:
        return {"ok": False, "message": "git add failed"}

    res = {"ok": True, "branch": branch, "commit": tip[:12], "committed": False, "pushed": False,
           "message": f"nothing changed since the last export on '{branch}'"}
    if tree != old_tree:
        ident = ["-c", "user.name=AgentDesk", "-c", "user.email=agentdesk@localhost"]
        code, new = g(*ident, "commit-tree", tree, *(["-p", tip] if tip else []), "-m", f"AgentDesk: export project {p.name}")
        if code != 0:
            return {"ok": False, "message": f"git commit failed: {new}"}
        # old value given: the update fails rather than overwrite a branch that moved meanwhile
        code, out = g("update-ref", "-m", "AgentDesk export", ref, new, tip or "0" * 40)
        if code != 0:
            return {"ok": False, "message": f"could not update '{branch}': {out}"}
        res.update(commit=new[:12], committed=True, message=f"committed to branch '{branch}'")
    if push:
        names = g("remote")[1].split()
        if not names:
            return {**res, "ok": False, "message": res["message"] + "; local-only: there is no remote to push to"}
        remote = "origin" if "origin" in names else names[0]
        code, out = g("push", "-u", remote, f"{ref}:{ref}", timeout=300)  # no --force: a diverged remote is refused
        res.update(ok=code == 0, pushed=code == 0, message=res["message"] + "; " + (out or f"pushed '{branch}' to {remote}"))
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


def clone_for_import(url: str, into: Path | None = None, branch: str | None = None) -> Path:
    """Clone a config repo (git URL or local path) into a new folder and return it. `branch`: e.g. an
    `agentdesk-export/<slug>` branch made by export_to_branch."""
    import tempfile
    if into is not None and into.exists() and any(into.iterdir()):
        raise BadExport(f"{into} already exists and is not empty")
    imports = DATA / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    dest = into or Path(tempfile.mkdtemp(prefix="agentdesk-import-", dir=imports))
    code, out = run(["git", "clone", *clone_options(), *(["--branch", branch] if branch else []), "--", url, str(dest)], imports,
                    env={"GIT_TERMINAL_PROMPT": "0"}, timeout=600)
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
BUILD_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){1,3}")  # what a build version may be (also names build/<version>)
BUILD_MAX_TAGS = 30
BUILD_MAX_TASKS = 500


# Versioned builds: an integration branch build/<version>, made from the base branch (or the release tag when an
# existing version is picked) plus the chosen finished task branches (auto/<id>), checked out in the build worktree.
# The base branch and the task branches are never changed; nothing is pushed.

def release_tags(p: "Project") -> list[dict]:
    """The repo's release tags vX.Y.Z, newest version first: {version, tag, date (unix, None if unknown)}."""
    code, out = _g(p, "for-each-ref", "--format=%(refname:short) %(creatordate:unix)", "refs/tags")
    rows = []
    for line in out.splitlines() if code == 0 else []:
        name, _, ts = line.strip().partition(" ")
        m = re.fullmatch(r"v(\d+\.\d+\.\d+)", name)
        if m:
            rows.append({"version": m.group(1), "tag": name, "date": int(ts) if ts.isdigit() else None})
    rows.sort(key=lambda r: parse_version(r["version"]), reverse=True)
    return rows


def source_versions(p: "Project", ref: str) -> list[tuple]:
    """Versions the sources at `ref` claim: the newest numbered heading of CHANGELOG.md and `__version__` of a
    top-level package's __init__.py."""
    out = []
    code, text = _g(p, "show", f"{ref}:CHANGELOG.md")
    m = re.search(r"^##\s*\[?v?(\d+(?:\.\d+){1,3})\b", text, re.M) if code == 0 else None
    if m:
        out.append(parse_version(m.group(1)))
    code, text = _g(p, "grep", "-h", "-e", "__version__", ref, "--", ":(glob)*/__init__.py")
    for m in re.finditer(r"^__version__\s*[:=][^\"'\n]*[\"'](\d+(?:\.\d+){1,3})[\"']", text if code == 0 else "", re.M):
        out.append(parse_version(m.group(1)))
    return [v for v in out if v]


def next_version(p: "Project", tags: list[dict] | None = None) -> str:
    """The next patch version after the newest of the release tags, CHANGELOG.md and __version__ (0.1.0 if none)."""
    tags = release_tags(p) if tags is None else tags
    known = [parse_version(t["version"]) for t in tags] + source_versions(p, p.cfg["repo"]["base"])
    if not known:
        return "0.1.0"
    top = list(max(known, key=lambda v: v + (0,) * (4 - len(v))))
    top = (top + [0, 0, 0])[:max(3, len(top))]
    top[-1] += 1
    return ".".join(str(x) for x in top)


def _selection_file(p: "Project") -> Path:
    return p.dir / "build" / "selection.json"


def build_selection(p: "Project") -> dict:
    """The last versioned build's choice: {version, tasks (checked ids), seen (ids that were offered)}."""
    try:
        d = json.loads(_selection_file(p).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_build_selection(p: "Project", version: str, tasks: list[str], seen: list[str]) -> None:
    f = _selection_file(p)
    f.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(f, json.dumps({"version": version, "tasks": tasks, "seen": seen}))


def _auto_merged(p: "Project", ref: str) -> set[str]:
    code, out = _g(p, "for-each-ref", f"--merged={ref}", "--format=%(refname:short)", "refs/heads/auto/")
    return set(out.split()) if code == 0 else set()


def _ts(iso) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def build_plan(p: "Project", version: str | None = None) -> dict:
    """What the Build panel offers: `versions` (the next one first, then the release tags), the chosen `version`
    and the `ref` it starts from (its tag, or the base branch for a new version), and `tasks`: the finished tasks
    whose branch auto/<id> still exists, newest first, each {id, title, merged (into the base branch), in_base (already
    in `ref`), default_checked (finished since the last tag), checked (the remembered choice, else the default)}."""
    base = p.cfg["repo"]["base"]
    tags = release_tags(p)
    proposed = next_version(p, tags)
    sel = build_selection(p)
    by_version = {t["version"]: t for t in tags}
    nxt = proposed
    saved = str(sel.get("version") or "")
    if BUILD_VERSION.fullmatch(saved) and saved not in by_version and (
            not tags or _vcmp(parse_version(saved), parse_version(tags[0]["version"])) > 0):
        nxt = saved  # an edited next version is kept until it is released
    versions = [{"version": nxt, "kind": "next", "ref": base, "proposed": proposed}] + \
        [{"version": t["version"], "kind": "tag", "ref": t["tag"]} for t in tags[:BUILD_MAX_TAGS]]
    chosen = (version or "").strip().lstrip("vV") or (saved if saved in by_version else nxt)
    if not BUILD_VERSION.fullmatch(chosen):
        chosen = nxt
    ref = by_version[chosen]["tag"] if chosen in by_version else base
    state = Store(p).load().get("tasks", {})
    titles = {t.id: t.title for t in load_tasks(p)}
    code, heads = _g(p, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/")
    heads_set = set(heads.split()) if code == 0 else set()
    merged = _auto_merged(p, base)
    in_ref = merged if ref == base else _auto_merged(p, ref)
    last_tag = tags[0]["date"] if tags else None
    seen = set(sel.get("seen") or [])
    checked = set(sel.get("tasks") or [])
    rows = []
    for tid, e in state.items():
        br = f"auto/{tid}"
        if not isinstance(e, dict) or e.get("status") != "done" or br not in heads_set:
            continue
        fin = _ts(e.get("finished"))
        in_base = br in in_ref
        dflt = not in_base and (last_tag is None or fin is None or fin > last_tag)
        rows.append({"id": tid, "title": titles.get(tid, ""), "branch": br, "merged": br in merged, "in_base": in_base,
                     "default_checked": dflt, "checked": in_base or ((tid in checked) if tid in seen else dflt),
                     "finished": e.get("finished")})
    rows.sort(key=lambda r: (len(r["id"]), r["id"]), reverse=True)
    return {"versions": versions, "version": chosen, "ref": ref, "base": base, "proposed": proposed, "tasks": rows}


def check_build_selection(p: "Project", version, tasks) -> tuple[dict | None, str | None]:
    """Validates a Build request: (selection, None) or (None, why). The selection: version, ref, branch, tasks (ids,
    oldest first), merge (those not already in ref), seen (every id offered)."""
    v = str(version if version is not None else "").strip().lstrip("vV")
    if not BUILD_VERSION.fullmatch(v):
        return None, "the version must look like 1.2.3"
    if not isinstance(tasks, list) or not all(isinstance(x, str) for x in tasks):
        return None, "tasks must be a list of task ids"
    if len(tasks) > BUILD_MAX_TASKS:
        return None, "too many tasks"
    plan = build_plan(p, v)
    known = {t["id"]: t for t in plan["tasks"]}
    bad = [x for x in tasks if x not in known]
    if bad:
        return None, "not a finished task with a branch auto/<id>: " + ", ".join(bad[:10])
    ids = sorted(set(tasks), key=lambda x: (len(x), x))
    try:  # dependencies before what builds on them (ids are usually in that order already)
        from . import dag
        order = {tid: i for i, tid in enumerate(dag.topo_order(dag.nodes_from_tasks(
            sorted(load_tasks(p), key=lambda t: (len(t.id), t.id)))))}
        ids.sort(key=lambda x: order.get(x, len(order)))
    except Exception:
        pass
    return {"version": v, "ref": plan["ref"], "branch": f"build/{v}", "tasks": ids,
            "merge": [x for x in ids if not known[x]["in_base"]], "seen": list(known)}, None


class BuildConflict(RuntimeError):
    pass


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
            write_atomic(self.statf, json.dumps(self.st))
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

    def start(self, kind: str, ref: str, blocked: str | None = None, sel: dict | None = None) -> str | None:
        """Returns an error message, or None when the build was started. `sel` (from check_build_selection): build
        the integration branch build/<version> = its ref + the chosen task branches, instead of `ref` as it is."""
        if kind not in BUILD_KINDS:
            return "unknown build kind"
        if sel:
            ref = sel["ref"]
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
            self._set(status="running", kind=kind, ref=ref, commit=sha[:12], started=time.time(), ended=None, exit=None,
                      version=sel["version"] if sel else None, branch=sel["branch"] if sel else None,
                      tasks=list(sel["tasks"]) if sel else [], included=[], conflict=None)
            self.thread = threading.Thread(target=self._work, args=(kind, sha, cmd, shell, sel), daemon=True)
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

    def _integrate(self, sel: dict, sha: str) -> None:
        """(Re)creates build/<version> at `sha` in the build worktree and merges the chosen task branches into it.
        Only that branch moves; a conflict is aborted and reported (BuildConflict)."""
        br = sel["branch"]
        code, out = git(["checkout", "--force", "-B", br, sha], self.wt)
        if code != 0:
            raise RuntimeError(f"could not create the branch {br}: {out.strip()}")
        self._say(f"== {br} reset to {self.st.get('ref')} ({sha[:12]})")
        ident = [] if git(["config", "user.email"], self.wt)[0] == 0 else ["-c", "user.name=AgentDesk", "-c", "user.email=agentdesk@localhost"]
        for tid in sel["tasks"]:
            if tid not in sel["merge"]:
                self._say(f"== task {tid}: already in {self.st.get('ref')}")
                continue
            if self.stopped:
                raise RuntimeError("stopped")
            tb = f"auto/{tid}"
            code, out = git([*ident, "merge", "--no-ff", "--no-edit", "-m", f"Build {sel['version']}: merge {tb}", tb], self.wt)
            if code != 0:
                _, files = git(["diff", "--name-only", "--diff-filter=U"], self.wt)
                git(["merge", "--abort"], self.wt)
                conflicts = files.split()
                self._set(conflict={"task": tid, "files": conflicts[:50]})
                self._say(out.strip())
                raise BuildConflict(f"{tb} does not merge cleanly into {br}" + (f" (conflicts in {', '.join(conflicts[:10])})" if conflicts else "")
                                    + ". The merge was aborted and nothing was built; the base branch and the task branches are unchanged.")
            self._say(f"== merged {tb}")
            self._set(included=[*self.st.get("included", []), tid])

    def _work(self, kind: str, sha: str, cmd, shell: bool, sel: dict | None = None) -> None:
        code = 1
        try:
            self._say(f"== {kind} of {self.st.get('ref')} ({sha[:12]}) in {self.wt}")
            self._prepare(kind, sha)
            if self.stopped:
                raise RuntimeError("stopped")
            if sel:
                self._integrate(sel, sha)
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
        except BuildConflict as e:
            self._say(f"== merge conflict: {e}")
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
            try:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
            except OSError:  # a headless server has no file manager
                return f"no file manager here; the output is in {folder}"
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
