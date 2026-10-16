"""Where the data lives: AGENTDESK_HOME, else data/ next to the app; the packaged program falls back to %LOCALAPPDATA%."""
from agentdesk import core


def test_data_home_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(core, "APP", tmp_path / "app")
    monkeypatch.delenv("AGENTDESK_HOME", raising=False)
    monkeypatch.setattr(core, "FROZEN", False)
    assert core._data_home() == tmp_path / "app" / "data"  # source: unchanged
    monkeypatch.setattr(core, "FROZEN", True)
    assert core._data_home() == tmp_path / "local" / "AgentDesk"  # installed: no data next to the exe
    (tmp_path / "app" / "data").mkdir(parents=True)
    assert core._data_home() == tmp_path / "app" / "data"  # portable: a data folder next to the exe wins
    monkeypatch.setenv("AGENTDESK_HOME", str(tmp_path / "h"))
    assert core._data_home() == tmp_path / "h"
