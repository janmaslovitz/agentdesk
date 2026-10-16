import os
import subprocess
import sys
import tomllib

import pytest

from agentdesk import __main__ as cli, core, server


@pytest.fixture(autouse=True)
def fresh_settings():
    """These tests rewrite settings.toml (some deliberately break it): start and end from the defaults."""
    (core.DATA / "settings.toml").unlink(missing_ok=True)
    yield
    (core.DATA / "settings.toml").unlink(missing_ok=True)


def _settings(text):
    (core.DATA / "settings.toml").write_text(text, encoding="utf-8", newline="")


def test_window_settings_keep_one_table_and_crlf():
    _settings('[guard]\r\ndeny = []\r\n\r\n[window]\r\nwidth = 900\r\n')
    for _ in range(3):
        w = server.window_settings({"x": 10, "y": 20})
    raw = (core.DATA / "settings.toml").read_bytes().decode()
    assert raw.count("[window]") == 1 and "\n" not in raw.replace("\r\n", "")
    assert w["width"] == 900 and w["height"] == 860 and w["x"] == 10
    tomllib.loads(raw)


def test_window_settings_refuses_to_write_invalid_toml():
    bad = '[window]\nwidth = 900\n\n[window]\nwidth = 5\n'  # already invalid (duplicate table)
    path = core.DATA / "settings.toml"
    path.write_text(bad, encoding="utf-8", newline="")
    try:
        server.window_settings({"x": 1})
    except tomllib.TOMLDecodeError:
        pass  # reading an already broken file fails loudly; it must not be rewritten
    assert path.read_text(encoding="utf-8") == bad


def test_kill_refuses_foreign_and_own_processes():
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not cli.kill(other.pid)            # not an agentdesk process
        assert other.poll() is None
        assert not cli.kill(os.getpid())          # ourselves
        assert not cli.kill(os.getppid(), tree=True)  # an ancestor
    finally:
        other.kill()


def test_stop_with_stale_pid_kills_nothing(capsys):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (core.DATA / "dashboard.pid").write_text(str(other.pid))
        cli.cmd_stop(False)
        assert other.poll() is None
        assert not (core.DATA / "dashboard.pid").exists()
        assert "no process was killed" in capsys.readouterr().out
    finally:
        other.kill()


def test_agents_and_checks_get_a_scratch_data_home(project):
    assert core.options(project).env["AGENTDESK_HOME"] == core.scratch_home() != str(core.DATA)
    code, out = core.run([sys.executable, "-c", "import os;print(os.environ['AGENTDESK_HOME'])"], project.dir)
    assert out.strip() == core.scratch_home()
