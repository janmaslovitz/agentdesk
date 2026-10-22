"""The installed app must never take the workers' scratch folder as its data folder (it showed no projects)."""

import tempfile
from pathlib import Path


from agentdesk import core, server


def scratch():
    return str(Path(tempfile.gettempdir()) / "agentdesk-worker-home")


def test_installed_app_ignores_the_scratch_home(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "FROZEN", True)
    monkeypatch.setattr(core, "APP", tmp_path / "app")  # no data folder next to the exe
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("AGENTDESK_HOME", scratch())
    assert core._data_home() == tmp_path / "local" / "AgentDesk"
    monkeypatch.setenv("AGENTDESK_HOME", str(Path(scratch()) / ".." / "agentdesk-worker-home"))
    assert core._data_home() == tmp_path / "local" / "AgentDesk"  # also when written another way


def test_a_real_custom_home_still_works_for_the_installed_app(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "FROZEN", True)
    monkeypatch.setenv("AGENTDESK_HOME", str(tmp_path / "mine"))
    assert core._data_home() == tmp_path / "mine"


def test_from_source_the_home_is_used_as_given(monkeypatch):
    monkeypatch.setattr(core, "FROZEN", False)
    monkeypatch.setenv("AGENTDESK_HOME", scratch())  # workers run the source version against the scratch home on purpose
    assert core._data_home() == Path(scratch())


def test_workers_get_the_scratch_home_but_the_full_access_coworker_does_not(project):
    assert core.options(project).env["AGENTDESK_HOME"] == core.scratch_home()
    assert "AGENTDESK_HOME" not in core.options(project, scratch=False).env
    assert core.options(project, scratch=False, env={"AGENTDESK_HOME": "x"}).env["AGENTDESK_HOME"] == "x"  # an explicit value still wins


def test_the_coworker_sessions_do_not_carry_the_scratch_home(project):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], cwd=project.repo, check=True)
    core.set_chat_mode(project, "coworker")
    c = server.Chat(server.LOOP, project)
    try:
        assert "AGENTDESK_HOME" not in c._live_options().env
        assert "AGENTDESK_HOME" not in c._coworker_options().env      # full-access, non-live
    finally:
        c.reset()
