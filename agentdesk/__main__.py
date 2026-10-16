"""AgentDesk command line.

    python -m agentdesk [dashboard] [--browser]  open the dashboard in a window (--browser: in a browser tab)
    python -m agentdesk stop [--runs]            end the dashboard (--runs: also stop runs it started)
    python -m agentdesk projects                 list projects
    python -m agentdesk add-project NAME REPO BASE [--preset rust|node|python|go|none] [--account A]
    python -m agentdesk status PROJECT
    python -m agentdesk run PROJECT [TASK] [--max N]
    python -m agentdesk plan PROJECT [TASK]
    python -m agentdesk final PROJECT TASK
    python -m agentdesk export PROJECT FILE.zip [--logs]   save a project (config, tasks, notes, state, reports, chat) to a file
    python -m agentdesk import-file FILE.zip --repo PATH [--name N] [--account A]   make a project from such a file
    python -m agentdesk import PROJECT_NAME OLD_ORCHESTRATOR_DIR   bring in a zz_orchestrator queue
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import core


def cmd_status(p: core.Project) -> None:
    state = core.Store(p).load()
    for t in core.load_tasks(p):
        e = state["tasks"].get(t.id, {})
        cost = sum((r.get("cost_usd") or 0) for r in e.get("runs", []))
        print(f"{t.id:<8} {e.get('status', 'pending'):<9} {e.get('tier', t.tier or '-'):<11} {cost:7.3f}$  {t.title}")
    print(f"project total {state.get('spent_usd', 0):.3f} USD")


def _ps(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def process_info(pid: int) -> tuple[int, str] | None:
    """(parent pid, command line) of a live process, or None."""
    if sys.platform == "win32":
        out = _ps(["powershell", "-NoProfile", "-Command",
                   f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; if($p){{\"$($p.ParentProcessId)|$($p.CommandLine)\"}}"])
    else:
        parts = _ps(["ps", "-p", str(int(pid)), "-o", "ppid=,args="]).split(None, 1)
        out = "|".join(parts) if len(parts) == 2 else ""
    ppid, _, cmd = out.partition("|")
    return (int(ppid), cmd) if ppid.strip().isdigit() else None


def is_agentdesk(pid: int) -> bool:
    """A pid file can be stale and its pid reused: only treat the process as ours if it runs agentdesk."""
    info = process_info(pid)
    if not info:
        return False
    if core.FROZEN:  # the program itself: AgentDesk.exe [command]
        return re.search(r"agentdesk\.exe", info[1], re.I) is not None
    return re.search(r"-m\s+agentdesk(\s|$)", info[1]) is not None


def own_lineage() -> set[int]:
    """This process and its ancestors: killing any of them (or their tree) would kill the caller."""
    seen, pid = set(), os.getpid()
    while pid and pid not in seen and len(seen) < 32:
        seen.add(pid)
        info = process_info(pid)
        pid = info[0] if info else 0
    return seen


def kill(pid: int, tree: bool = False) -> bool:
    if pid <= 0 or pid in own_lineage() or not is_agentdesk(pid):
        return False
    if sys.platform == "win32":
        cmd = ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else [])
        return subprocess.run(cmd, capture_output=True).returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def cmd_stop(runs: bool) -> None:
    pf = core.DATA / "dashboard.pid"
    if pf.exists():
        try:
            ok = kill(int(pf.read_text().strip()))
        except ValueError:
            ok = False
        pf.unlink(missing_ok=True)
        print("dashboard stopped" if ok else "dashboard was not running (stale pid file removed; no process was killed)")
    else:
        print("dashboard is not running")
    if runs:
        for p in core.projects():
            rf = p.path("runner.pid")
            if rf.exists():
                try:
                    if kill(int(rf.read_text().strip()), tree=True):
                        print(f"stopped run of {p.slug}")
                except ValueError:
                    pass
                rf.unlink(missing_ok=True)


def cmd_import(name: str, old: Path) -> None:
    """Make a project from an old single-project orchestrator folder (config, tasks, notes, state)."""
    with (old / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    repo = cfg["repo"]
    p = core.add_project(name, repo["path"], repo["base"], "none", "default",
                         quick=cfg["gates"].get("quick", []), final=cfg["gates"].get("final", []), env=cfg["gates"].get("env", {}))
    text = (p.dir / "project.toml").read_text(encoding="utf-8")
    copy = repo.get("copy_into_worktree", [])
    text = text.replace("copy_into_worktree = []", "copy_into_worktree = [" + ", ".join(core.toml_str(c) for c in copy) + "]")
    # Keep the old worktrees folder: tasks already started have their work (and branch) there.
    if repo.get("worktrees"):
        text = re.sub(r'(?m)^worktrees = .*$', lambda _m: f"worktrees = {core.toml_str(Path(repo['worktrees']).as_posix())}", text, count=1)
    (p.dir / "project.toml").write_text(text, encoding="utf-8")
    for f in (old / "tasks").glob("*.md"):
        shutil.copy(f, p.tasks_dir / f.name)
    for n in ("notes.md", "state.json"):
        if (old / n).exists():
            shutil.copy(old / n, p.dir / n)
    if (old / "reports").exists():
        shutil.copytree(old / "reports", p.dir / "reports", dirs_exist_ok=True)
    print(f"imported into {p.dir}")


def main() -> None:
    if sys.stdout is not None:  # None under pythonw
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="agentdesk", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("dashboard"); d.add_argument("--port", type=int, default=8765); d.add_argument("--browser", action="store_true", help="open a browser tab instead of the native window")
    d.add_argument("--no-browser", action="store_true", help="just serve: no window, no browser")
    st = sub.add_parser("stop"); st.add_argument("--runs", action="store_true", help="also stop runs started from the dashboard")
    sub.add_parser("projects")
    a = sub.add_parser("add-project"); a.add_argument("name"); a.add_argument("repo"); a.add_argument("base")
    a.add_argument("--preset", default="none", choices=list(core.PRESETS)); a.add_argument("--account", default="default")
    a.add_argument("--local-git", action="store_true",
                   help="if the folder isn't a git repo (or has no commits), set up local version tracking (stays on this computer)")
    s = sub.add_parser("status"); s.add_argument("project")
    r = sub.add_parser("run"); r.add_argument("project"); r.add_argument("task", nargs="?"); r.add_argument("--max", type=int)
    pl = sub.add_parser("plan"); pl.add_argument("project"); pl.add_argument("task", nargs="?")
    f = sub.add_parser("final"); f.add_argument("project"); f.add_argument("task")
    i = sub.add_parser("import"); i.add_argument("name"); i.add_argument("old", type=Path)
    ex = sub.add_parser("export"); ex.add_argument("project"); ex.add_argument("file", type=Path); ex.add_argument("--logs", action="store_true")
    imf = sub.add_parser("import-file"); imf.add_argument("file", type=Path); imf.add_argument("--repo", required=True)
    imf.add_argument("--name"); imf.add_argument("--account")
    args = ap.parse_args()
    if args.cmd in (None, "dashboard"):
        from . import server
        server.main(port=getattr(args, "port", 8765), browser=getattr(args, "browser", False),
                    window=not getattr(args, "no_browser", False))
    elif args.cmd == "stop":
        cmd_stop(args.runs)
    elif args.cmd == "projects":
        for p in core.projects():
            print(f"{p.slug:<20} {p.cfg['repo']['path']}  (base {p.cfg['repo']['base']}, account {p.account.get('name')})")
    elif args.cmd == "add-project":
        try:
            print(core.add_project(args.name, args.repo, args.base, args.preset, args.account, local_git=args.local_git).dir)
        except core.NeedsLocalGit as e:
            sys.exit(f"{e}.\nAgentDesk can set up local version tracking (stays on this computer; nothing is pushed, no remote is added).\n"
                     "Run again with --local-git to accept.")
    elif args.cmd == "status":
        cmd_status(core.project(args.project))
    elif args.cmd == "run":
        core.run_async(core.run_queue(core.project(args.project), args.task, args.max))
    elif args.cmd == "plan":
        core.run_async(core.plan_tasks(core.project(args.project), args.task))
    elif args.cmd == "final":
        core.final_gates(core.project(args.project), args.task)
    elif args.cmd == "import":
        cmd_import(args.name, args.old)
    elif args.cmd == "export":
        print(core.export_project(core.project(args.project), args.file, args.logs))
    elif args.cmd == "import-file":
        try:
            print(core.import_project(args.file, args.repo, args.name, args.account).dir)
        except core.NameClash as e:
            sys.exit(f"{e}\nNothing was imported (existing projects are never overwritten). Run again with --name NEW_NAME.")
        except ValueError as e:
            sys.exit(f"import refused: {e}")


if __name__ == "__main__":
    main()
