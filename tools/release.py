"""Release tooling for AgentDesk (standard library only; run it with any Python 3.11+).

    python tools/release.py current                     print the version
    python tools/release.py release --bump patch|minor|major
    python tools/release.py release --version 1.4.0     tests are run by release.ps1, not here
    python tools/release.py buildinfo                   write agentdesk/_buildinfo.py and version_info.txt (build_exe.ps1 does this)

`release` bumps `__version__` in agentdesk/__init__.py, turns the `## Unreleased` section of CHANGELOG.md into
`## <version> - <date>` (a fresh empty Unreleased goes on top), commits "Release <version>" and tags `v<version>`.
It needs a clean tracked tree, never pushes, and never touches anything else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
VERSION_LINE = re.compile(r'^(__version__\s*=\s*)"([^"]+)"([ 	]*#.*)?$', re.M)


def read_version(root: Path = ROOT) -> str:
    m = VERSION_LINE.search((root / "agentdesk" / "__init__.py").read_text(encoding="utf-8"))
    if not m or not SEMVER.match(m.group(2)):
        raise SystemExit("agentdesk/__init__.py has no `__version__ = \"x.y.z\"` line")
    return m.group(2)


def write_version(version: str, root: Path = ROOT) -> None:
    p = root / "agentdesk" / "__init__.py"
    text = p.read_text(encoding="utf-8")
    if not VERSION_LINE.search(text):
        raise SystemExit("no __version__ line to update")
    p.write_text(VERSION_LINE.sub(lambda m: f'{m.group(1)}"{version}"{m.group(3) or ""}', text, count=1), encoding="utf-8", newline="")


def bump(version: str, part: str) -> str:
    m = SEMVER.match(version)
    if not m:
        raise ValueError(f"not a version: {version!r}")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("bump is major, minor or patch")


def newer(a: str, b: str) -> bool:
    return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))


UNRELEASED = re.compile(r"^## \[?Unreleased\]?[^\n]*\n", re.M | re.I)


def changelog_release(text: str, version: str, date: str) -> str:
    """`## Unreleased` -> `## <version> - <date>`, with a new empty Unreleased above. Refuses an empty section."""
    m = UNRELEASED.search(text)
    if not m:
        raise SystemExit("CHANGELOG.md has no `## Unreleased` section")
    nxt = re.search(r"^## ", text[m.end():], re.M)
    body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
    if not body.strip():
        raise SystemExit("the `## Unreleased` section of CHANGELOG.md is empty: write what changed first")
    return text[:m.start()] + "## Unreleased\n\n\n" + f"## {version} - {date}\n" + text[m.end():]


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def release(root: Path = ROOT, part: str | None = None, version: str | None = None, today: str | None = None) -> str:
    cur = read_version(root)
    new = version or bump(cur, part or "patch")
    if not SEMVER.match(new):
        raise SystemExit(f"not a version: {new}")
    if not newer(new, cur):
        raise SystemExit(f"{new} is not newer than the current {cur}")
    if git(root, "status", "--porcelain", "--untracked-files=no", "--", ".", ":!data"):
        raise SystemExit("the tracked files have uncommitted changes: commit or stash them before releasing")
    if git(root, "tag", "--list", f"v{new}"):
        raise SystemExit(f"tag v{new} already exists")
    cl = root / "CHANGELOG.md"
    text = changelog_release(cl.read_text(encoding="utf-8"), new, today or dt.date.today().isoformat())
    write_version(new, root)
    cl.write_text(text, encoding="utf-8", newline="")
    git(root, "add", "agentdesk/__init__.py", "CHANGELOG.md")
    git(root, "commit", "-q", "-m", f"Release {new}")
    git(root, "tag", "-a", f"v{new}", "-m", f"AgentDesk {new}")
    return new


def buildinfo(root: Path = ROOT, now: dt.datetime | None = None) -> dict:
    """Stamp the build: agentdesk/_buildinfo.py (read by the program) and version_info.txt (the exe's file properties)."""
    version = read_version(root)
    try:
        commit = git(root, "rev-parse", "--short", "HEAD")
        if git(root, "status", "--porcelain", "--untracked-files=no", "--", ".", ":!data"):
            commit += "+dirty"
    except (SystemExit, OSError):
        commit = ""
    built = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    (root / "agentdesk" / "_buildinfo.py").write_text(f'# Generated by tools/release.py buildinfo; not in git.\nCOMMIT = "{commit}"\nBUILT = "{built}"\n',
                                                      encoding="utf-8", newline="")
    nums = tuple(int(x) for x in version.split(".")) + (0,)
    shown = f"{version} ({commit})" if commit else version
    (root / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'AgentDesk'), StringStruct('FileDescription', 'AgentDesk'),
      StringStruct('FileVersion', '{version}'), StringStruct('InternalName', 'AgentDesk'),
      StringStruct('OriginalFilename', 'AgentDesk.exe'), StringStruct('ProductName', 'AgentDesk'),
      StringStruct('ProductVersion', '{shown}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8", newline="")
    return {"version": version, "commit": commit, "built": built}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("current")
    sub.add_parser("buildinfo")
    r = sub.add_parser("release")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--bump", choices=("major", "minor", "patch"))
    g.add_argument("--version")
    a = ap.parse_args(argv)
    if a.cmd == "current":
        print(read_version())
    elif a.cmd == "buildinfo":
        print(buildinfo())
    else:
        print(release(part=a.bump, version=a.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
