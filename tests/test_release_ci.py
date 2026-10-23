"""Release builds in CI (.github/workflows/release.yml, agentdesk.spec, build_unix.sh, tools/release.py notes): what can
be checked offline. The workflow itself only runs on GitHub."""

import importlib.util
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from agentdesk import __version__

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_tool", ROOT / "tools" / "release.py")
rel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rel)


def run_spec(monkeypatch, platform: str) -> dict:
    """Execute agentdesk.spec as PyInstaller would on `platform`, with recording stand-ins for its build objects."""
    calls: dict = {}

    def rec(name):
        def f(*args, **kw):
            calls[name] = {"args": args, **kw}
            return name
        return f

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda pkg: ([], [], [f"{pkg}.collected"])
    for name, mod in (("PyInstaller", types.ModuleType("PyInstaller")), ("PyInstaller.utils", types.ModuleType("PyInstaller.utils")),
                      ("PyInstaller.utils.hooks", hooks)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.chdir(ROOT)
    ns = {n: rec(n) for n in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")}
    ns["Analysis"] = lambda *a, **kw: calls.setdefault("Analysis", {"args": a, **kw}) and types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
    exec(compile((ROOT / "agentdesk.spec").read_text(encoding="utf-8"), "agentdesk.spec", "exec"), ns)
    return calls


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_spec_is_cross_platform(monkeypatch, platform):
    calls = run_spec(monkeypatch, platform)
    hidden = calls["Analysis"]["hiddenimports"]
    exe = calls["EXE"]
    assert exe["name"] == "AgentDesk" and exe["console"] is False
    assert ("agentdesk/web/index.html", "agentdesk/web") in calls["Analysis"]["datas"]
    if platform == "win32":
        assert exe["icon"].endswith(".ico") and "pystray._win32" in hidden and "webview.platforms.winforms" in hidden
        assert "BUNDLE" not in calls
    else:
        assert exe["icon"] is None or exe["icon"].endswith(".icns")  # never the Windows .ico
        assert exe["version"] is None  # version_info.txt is a Windows resource
        assert not any(h.endswith(("_win32", "winforms", "edgechromium")) for h in hidden)
    if platform == "darwin":
        b = calls["BUNDLE"]
        assert b["name"] == "AgentDesk.app" and b["version"] == __version__
        assert "webview.platforms.cocoa" in hidden
    if platform == "linux":
        assert "webview.platforms.gtk" in hidden and "BUNDLE" not in calls


def working_bash() -> str | None:
    """A bash that runs. On Windows `bash` on PATH may be System32's WSL launcher, which only prints an error when no
    Linux distribution is installed: prefer Git's bash there, and try each candidate."""
    candidates = []
    if sys.platform == "win32":
        try:
            exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            exec_path = ""
        if exec_path:  # <Git>/mingw64/libexec/git-core -> <Git>/bin/bash.exe, <Git>/usr/bin/bash.exe
            git_root = Path(exec_path).parents[2]
            candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    candidates.append(shutil.which("bash"))
    for c in candidates:
        if not c or not Path(c).is_file():
            continue
        try:
            r = subprocess.run([str(c), "-c", "echo ok"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip() == b"ok":
            return str(c)
    return None


def test_build_unix_script():
    text = (ROOT / "build_unix.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert "tools/release.py current" in text  # the version comes from agentdesk/__init__.py
    assert 'AgentDesk-$version-linux-$arch.tar.gz' in text and 'AgentDesk-$version-macos-$arch.zip' in text
    bash = working_bash()
    if bash is None:
        pytest.skip("no working bash to syntax-check build_unix.sh")
    r = subprocess.run([bash, "-n", "-"], input=text.replace("\r\n", "\n"), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_version_extraction_matches_the_package():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "current"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == __version__


CHANGELOG = """# Changelog

## Unreleased

- next

## 1.10.0 - 2026-02-02

### Added
- ten

## 1.1.0 - 2026-01-02
- one

## [1.0.0] - 2026-01-01
- first
"""


def test_release_notes_come_from_the_changelog_section():
    assert rel.changelog_notes(CHANGELOG, "1.10.0") == "### Added\n- ten"
    assert rel.changelog_notes(CHANGELOG, "1.1.0") == "- one"        # not confused with 1.10.0
    assert rel.changelog_notes(CHANGELOG, "1.0.0") == "- first"      # the last section, [bracketed]
    assert rel.changelog_notes(CHANGELOG, "1.1") is None
    assert rel.changelog_notes(CHANGELOG, "2.0.0") is None


def test_notes_command_prints_the_section_or_a_fallback():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## (\d+\.\d+\.\d+) - ", text, re.M)
    assert m, "CHANGELOG.md has a released version"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "notes", "v" + m.group(1)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and r.stdout.strip() == rel.changelog_notes(text, m.group(1))
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "notes", "0.0.1"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "AgentDesk 0.0.1" and "no notes" in r.stderr


def load_workflow() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))


def test_release_workflow():
    wf = load_workflow()
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads the key `on` as true
    assert on["push"]["tags"] == ["v*"] and "tag" in on["workflow_dispatch"]["inputs"]
    jobs = wf["jobs"]
    assert set(jobs["build"]["strategy"]["matrix"]["os"]) == {"ubuntu-22.04", "macos-latest", "windows-latest"}
    assert jobs["build"]["needs"] == "release" and jobs["release"]["needs"] == "test" and jobs["publish"]["needs"] == "build"
    tests = [s["run"] for j in jobs.values() for s in j["steps"] if "pytest" in s.get("run", "") and "pip install" not in s["run"]]
    assert len(tests) == 1 and "pytest" in tests[0] and jobs["test"]["runs-on"] == "ubuntu-22.04"  # the tests run once
    for j in jobs.values():
        for s in j["steps"]:
            if "uses" in s:
                assert re.fullmatch(r"actions/[a-z-]+@v\d+", s["uses"]), s["uses"]  # official actions, major version
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.(\w+)", text)) <= {"GITHUB_TOKEN"}
    build = "\n".join(s.get("run", "") for s in jobs["build"]["steps"])
    assert "AgentDesk-Setup-" in build and "choco install innosetup" in build and "build_unix.sh" in build
    assert "libwebkit2gtk" in build


def test_installer_name_is_what_the_updater_picks():
    iss = (ROOT / "installer" / "AgentDesk.iss").read_text(encoding="utf-8")
    assert "OutputBaseFilename=AgentDesk-Setup-{#AppVersion}" in iss
    from agentdesk import core
    a = {"name": f"AgentDesk-Setup-{__version__}.exe", "browser_download_url": "https://github.com/x/y/releases/download/v1/a.exe"}
    others = [{"name": f"AgentDesk-{__version__}-macos-arm64.zip", "browser_download_url": "https://github.com/x/z.zip"},
              {"name": f"AgentDesk-{__version__}-linux-x86_64.tar.gz", "browser_download_url": "https://github.com/x/z.tgz"}]
    assert core.pick_installer_asset(others + [a]) is a
