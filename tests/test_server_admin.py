"""Console admin tool (agentdesk server ...) and the Debian package layout (no dpkg needed)."""
import importlib.util
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentdesk.gateway import admin_cli, config
from agentdesk.gateway.users import UserStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTDESK_SERVER_CONFIG", raising=False)
    monkeypatch.delenv("AGENTDESK_SERVER_HOME", raising=False)
    return tmp_path / "srv"


def run(home, *a):
    admin_cli.main(["--home", str(home), *a])


def pwfile(tmp_path, text="correct-horse-battery"):
    f = tmp_path / "pw"
    f.write_text(text + "\n")
    return str(f)


def test_init_creates_home_config_and_admin(home, tmp_path, capsys):
    run(home, "init", "--admin-password-file", pwfile(tmp_path), "--port", "9090")
    assert (home / "server.toml").is_file()
    assert config.load(home)["port"] == 9090
    users = UserStore(home).list()
    assert [(u["username"], u["role"]) for u in users] == [("admin", "admin")]
    assert UserStore(home).authenticate("admin", "correct-horse-battery")
    run(home, "init")  # second time: nothing is asked, nothing replaced
    assert "exist already" in capsys.readouterr().out


def test_user_add_list_passwd_disable_delete(home, tmp_path, capsys):
    run(home, "user", "add", "bob", "--password-file", pwfile(tmp_path))
    run(home, "user", "add", "amy", "--admin", "--password-file", pwfile(tmp_path))
    capsys.readouterr()
    run(home, "user", "list")
    out = capsys.readouterr().out
    assert "amy" in out and "admin" in out and "bob" in out and "disabled" not in out
    run(home, "user", "passwd", "bob", "--password-file", pwfile(tmp_path, "another-long-secret"))
    st = UserStore(home)
    assert st.authenticate("bob", "another-long-secret") and not st.authenticate("bob", "correct-horse-battery")
    run(home, "user", "disable", "bob")
    assert not st.authenticate("bob", "another-long-secret")
    capsys.readouterr()
    run(home, "user", "list")
    assert "disabled" in capsys.readouterr().out
    run(home, "user", "delete", "bob")
    assert st.get("bob") is None and (home / "users" / "bob").is_dir()  # data kept
    run(home, "user", "delete", "amy", "--purge")
    assert not (home / "users" / "amy").exists()


def test_user_errors_exit_cleanly(home, tmp_path):
    run(home, "user", "add", "bob", "--password-file", pwfile(tmp_path))
    with pytest.raises(SystemExit, match="already exists"):
        run(home, "user", "add", "bob", "--password-file", pwfile(tmp_path))
    with pytest.raises(SystemExit, match="no user"):
        run(home, "user", "passwd", "nobody", "--password-file", pwfile(tmp_path))


def test_config_get_set(home, capsys):
    run(home, "config", "set", "port", "9191")
    run(home, "config", "set", "idle_timeout", "10")
    run(home, "config", "set", "run_as_users", "true")
    run(home, "config", "set", "host", "127.0.0.1")
    cfg = config.load(home)
    assert (cfg["port"], cfg["idle_stop_minutes"], cfg["run_as_users"], cfg["host"]) == (9191, 10.0, True, "127.0.0.1")
    capsys.readouterr()
    run(home, "config", "get", "port")
    assert capsys.readouterr().out.strip() == "9191"
    with pytest.raises(SystemExit):
        run(home, "config", "set", "port", "abc")
    with pytest.raises(SystemExit):
        run(home, "config", "set", "nonsense", "1")
    cert = home / "c.pem"
    cert.write_text("x")
    run(home, "config", "set", "tls", str(cert), str(cert))
    assert config.load(home)["tls_cert"] == str(cert)
    run(home, "config", "set", "tls", "off")
    assert config.load(home)["tls_cert"] == ""
    assert (home / "server.toml").read_text().count("port =") == 1


def test_config_file_override_and_bad_value_rolls_back(home, tmp_path, monkeypatch):
    cfgfile = tmp_path / "etc" / "server.toml"
    monkeypatch.setenv("AGENTDESK_SERVER_CONFIG", str(cfgfile))
    run(home, "config", "set", "port", "1234")
    assert cfgfile.is_file() and not (home / "server.toml").exists()
    before = cfgfile.read_text()
    with pytest.raises(SystemExit):
        run(home, "config", "set", "run_as", "bogus")
    assert cfgfile.read_text() == before


def test_status_and_doctor_run(home, tmp_path, capsys):
    run(home, "init", "--admin-password-file", pwfile(tmp_path))
    run(home, "status")
    assert "users:     1" in capsys.readouterr().out
    try:  # exits 1 on a machine without git: either way it reports
        run(home, "doctor")
    except SystemExit as e:
        assert e.code == 1
    out = capsys.readouterr().out
    assert "git:" in out and "disk free" in out


def test_python_m_agentdesk_server(home):
    r = subprocess.run([sys.executable, "-m", "agentdesk", "server", "--home", str(home), "user", "list"],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 0 and "no users" in r.stdout


# ----------------------------------------------------------------------------- Debian package layout

def load_builder():
    spec = importlib.util.spec_from_file_location("build_deb", ROOT / "packaging" / "debian" / "build_deb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPTS = ("usr/bin/agentdesk-server", "DEBIAN/postinst", "DEBIAN/prerm", "DEBIAN/postrm")


def test_deb_layout(tmp_path):
    b = load_builder()
    d = b.stage(tmp_path / "pkg", "9.9.9")
    for rel in ("opt/agentdesk-server/app/agentdesk/__main__.py", "opt/agentdesk-server/app/agentdesk/gateway/admin_cli.py",
                "opt/agentdesk-server/requirements-server.txt", "usr/bin/agentdesk-server", "etc/agentdesk-server/server.toml",
                "etc/default/agentdesk-server", "lib/systemd/system/agentdesk-server.service",
                "usr/share/doc/agentdesk-server/nginx-agentdesk.conf.example", "usr/share/doc/agentdesk-server/Caddyfile.example",
                "DEBIAN/control", "DEBIAN/conffiles", "DEBIAN/postinst", "DEBIAN/prerm", "DEBIAN/postrm"):
        assert (d / rel).is_file(), rel
    assert not list(d.rglob("__pycache__"))
    ctl = (d / "DEBIAN/control").read_text()
    assert "Package: agentdesk-server" in ctl and "Version: 9.9.9" in ctl and "Architecture: all" in ctl
    assert "python3 (>= 3.11)" in ctl and "git" in ctl
    reqs = (d / "opt/agentdesk-server/requirements-server.txt").read_text().lower()
    assert "claude-agent-sdk" in reqs and "pywebview" not in reqs and "pystray" not in reqs
    unit = (d / "lib/systemd/system/agentdesk-server.service").read_text()
    for needle in ("Restart=on-failure", "NoNewPrivileges=true", "ProtectSystem=", "PrivateTmp=true", "User=agentdesk",
                   "gateway --home"):
        assert needle in unit, needle
    for script in SCRIPTS:
        data = (d / script).read_bytes()
        assert data.startswith(b"#!/bin/sh\n") and b"\r" not in data
        assert (d / script).stat().st_mode & stat.S_IXUSR or sys.platform == "win32"
    assert "/etc/agentdesk-server/server.toml" in (d / "DEBIAN/conffiles").read_text()
    with pytest.raises(SystemExit):
        b.stage(d)  # refuses to reuse a folder


def test_deb_scripts_parse_with_sh(tmp_path):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no sh")
    d = load_builder().stage(tmp_path / "pkg", "9.9.9")
    for script in SCRIPTS:
        r = subprocess.run([sh, "-n", str(d / script)], capture_output=True, text=True)
        assert r.returncode == 0, (script, r.stderr)


def test_release_workflow_attaches_deb():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "build_deb.py" in wf and "agentdesk-server_" in wf and "needs: [build, deb]" in wf
    assert "--clobber" in wf
