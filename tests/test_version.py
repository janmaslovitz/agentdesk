"""Versions: one number, shown everywhere, bumped and tagged by tools/release.py."""

import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentdesk import __main__ as cli, __version__, core, server

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_tool", ROOT / "tools" / "release.py")
rel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rel)


def test_the_version_is_semver_and_matches_the_tool():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert rel.read_version(ROOT) == __version__


def test_changelog_documents_the_current_version_or_has_unreleased_notes():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(__version__)} - ", text, re.M) or re.search(r"^## Unreleased", text, re.M)


def test_version_info_and_text():
    v = core.version_info()
    assert v["version"] == __version__ and set(v) == {"version", "commit", "built", "installed"}
    assert core.version_text().startswith(f"AgentDesk {__version__}")


def test_cli_prints_the_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agentdesk", "version"])
    cli.main()
    assert capsys.readouterr().out.strip() == core.version_text()
    monkeypatch.setattr(sys, "argv", ["agentdesk", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert core.version_text() in capsys.readouterr().out


def test_overview_and_api_carry_the_version():
    assert server._overview()["version"]["version"] == __version__


def test_upgrade_is_recorded_once():
    f = core.DATA / "app_version.json"
    f.unlink(missing_ok=True)
    assert core.record_version() == (None, __version__)          # first run: nothing to say about a previous version
    assert json.loads(f.read_text(encoding="utf-8"))["version"] == __version__
    f.write_text(json.dumps({"version": "0.9.0"}), encoding="utf-8")
    assert core.record_version() == ("0.9.0", __version__)       # upgraded from 0.9.0
    assert core.record_version() == (None, __version__)          # unchanged now


@pytest.mark.parametrize("v,part,want", [("1.2.3", "patch", "1.2.4"), ("1.2.3", "minor", "1.3.0"), ("1.2.3", "major", "2.0.0"),
                                         ("1.9.9", "minor", "1.10.0")])
def test_bump(v, part, want):
    assert rel.bump(v, part) == want
    with pytest.raises(ValueError):
        rel.bump("1.2", "patch")


def test_newer_compares_numbers_not_text():
    assert rel.newer("1.10.0", "1.9.9") and not rel.newer("1.2.3", "1.2.3") and not rel.newer("1.2.0", "1.10.0")


def test_changelog_release_moves_unreleased_notes_under_the_version():
    text = "# Changelog\n\nintro\n\n## Unreleased\n\n### Added\n- a thing\n\n## 1.0.0 - 2026-01-01\n- old\n"
    out = rel.changelog_release(text, "1.1.0", "2026-02-02")
    assert out.index("## Unreleased") < out.index("## 1.1.0 - 2026-02-02") < out.index("- a thing") < out.index("## 1.0.0 - 2026-01-01")
    assert out.count("## Unreleased") == 1
    with pytest.raises(SystemExit, match="empty"):
        rel.changelog_release("## Unreleased\n\n## 1.0.0 - x\n- old\n", "1.1.0", "d")
    with pytest.raises(SystemExit, match="no `## Unreleased`"):
        rel.changelog_release("# Changelog\n", "1.1.0", "d")


def make_repo(tmp_path, notes="- something changed\n"):
    root = tmp_path / "repo"
    (root / "agentdesk").mkdir(parents=True)
    (root / "agentdesk" / "__init__.py").write_text('"""x"""\n\n__version__ = "1.0.0"  # bumped by the tool\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## Unreleased\n\n{notes}\n## 1.0.0 - 2026-01-01\n- first\n", encoding="utf-8")
    for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start"]):
        subprocess.run(["git", *c], cwd=root, check=True, capture_output=True)
    return root


def tags(root):
    return subprocess.run(["git", "tag", "--list"], cwd=root, capture_output=True, text=True).stdout.split()


def test_release_bumps_commits_and_tags(tmp_path):
    root = make_repo(tmp_path)
    assert rel.release(root, part="minor", today="2026-03-03") == "1.1.0"
    init = (root / "agentdesk" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "1.1.0"  # bumped by the tool' in init  # the comment survives
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 1.1.0 - 2026-03-03\n\n- something changed" in log and log.count("## Unreleased") == 1
    assert tags(root) == ["v1.1.0"]
    last = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True).stdout.strip()
    assert last == "Release 1.1.0"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout.strip() == ""


def test_release_refuses_unsafe_situations(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(SystemExit, match="not newer"):
        rel.release(root, version="1.0.0")
    (root / "agentdesk" / "__init__.py").write_text((root / "agentdesk" / "__init__.py").read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="uncommitted"):
        rel.release(root, part="patch")
    subprocess.run(["git", "checkout", "--", "."], cwd=root, check=True)
    subprocess.run(["git", "tag", "v1.0.1"], cwd=root, check=True)
    with pytest.raises(SystemExit, match="already exists"):
        rel.release(root, part="patch")
    empty = make_repo(tmp_path / "e", notes="")
    with pytest.raises(SystemExit, match="empty"):
        rel.release(empty, part="patch")
    assert tags(empty) == [] and rel.read_version(empty) == "1.0.0"  # nothing was changed


def test_buildinfo_stamps_the_program_and_the_exe_properties(tmp_path):
    root = make_repo(tmp_path)
    info = rel.buildinfo(root, now=dt.datetime(2026, 5, 6, 7, 8))
    assert info["version"] == "1.0.0" and info["built"] == "2026-05-06 07:08" and re.fullmatch(r"[0-9a-f]+", info["commit"])
    b = (root / "agentdesk" / "_buildinfo.py").read_text(encoding="utf-8")
    assert f'COMMIT = "{info["commit"]}"' in b and 'BUILT = "2026-05-06 07:08"' in b
    vi = (root / "version_info.txt").read_text(encoding="utf-8")
    assert "filevers=(1, 0, 0, 0)" in vi and "StringStruct('FileVersion', '1.0.0')" in vi
    compile(vi.replace("VSVersionInfo(", "dict(ffi=", 1).replace("\n", " "), "v", "eval") if False else None  # (parsed by PyInstaller)


def test_a_modified_data_folder_does_not_block_a_release(tmp_path):
    root = make_repo(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "settings.toml").write_text("a = 1\n", encoding="utf-8")
    for c in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "data"]):
        subprocess.run(["git", *c], cwd=root, check=True, capture_output=True)
    (root / "data" / "settings.toml").write_text("a = 2\n", encoding="utf-8")  # the app's own data changes all the time
    assert rel.release(root, part="patch", today="2026-03-03") == "1.0.1"
