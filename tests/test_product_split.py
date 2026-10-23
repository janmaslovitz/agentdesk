"""Two products, one core: headless entry points import without any GUI module; packaging stays separate."""
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUI = ["webview", "pystray", "PIL", "tkinter", "gi", "_tkinter"]

PROBE = """
import sys
for name in {gui!r}:
    sys.modules[name] = None  # any `import name` raises ImportError
import importlib
for mod in ("agentdesk.serve", "agentdesk.server", "agentdesk.gateway.cli", "agentdesk.gateway.admin_cli",
            "agentdesk.gateway.app", "agentdesk.gateway.config"):
    importlib.import_module(mod)
bad = [n for n in {gui!r} if sys.modules.get(n) is not None]
assert not bad, bad
print("ok")
"""


def test_headless_entry_points_import_without_gui_modules():
    r = subprocess.run([sys.executable, "-c", PROBE.format(gui=GUI)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


def _names(lines):
    out = set()
    for ln in lines:
        ln = ln.split("#")[0].strip()
        if ln and not ln.startswith("-"):
            out.add(ln)
    return out


def test_pyproject_core_has_no_gui_and_extras_match_requirements():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = (ROOT / "requirements-core.txt").read_text(encoding="utf-8").splitlines()
    assert set(proj["dependencies"]) == _names(core)
    assert not any(d.lower().startswith(("pywebview", "pystray", "pillow")) for d in proj["dependencies"])
    app = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() + \
        (ROOT / "requirements-tray.txt").read_text(encoding="utf-8").splitlines()
    assert set(proj["optional-dependencies"]["app"]) == _names(app) - set(proj["dependencies"])
    assert "server" in proj["optional-dependencies"]


def test_spec_excludes_server_only_code():
    spec = (ROOT / "agentdesk.spec").read_text(encoding="utf-8")
    assert '"agentdesk.gateway"' in spec and '"agentdesk.serve"' in spec and "SERVER_ONLY" in spec


def test_deb_has_no_gui_code_or_deps(tmp_path):
    sys.path.insert(0, str(ROOT / "packaging" / "debian"))
    try:
        import build_deb
    finally:
        sys.path.pop(0)
    d = build_deb.stage(tmp_path / "pkg", "9.9.9")
    reqs = (d / "opt/agentdesk-server/requirements-server.txt").read_text().lower()
    assert "claude-agent-sdk" in reqs and not any(g in reqs for g in ("pywebview", "pystray", "pillow"))
    assert not list((d / "opt/agentdesk-server/app").rglob("*.ico"))
    assert "Version: 9.9.9" in (d / "DEBIAN/control").read_text()
