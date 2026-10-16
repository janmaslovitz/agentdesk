import socket

import pytest

from agentdesk import core


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in core.PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    core._PROXY_UP.clear()
    (core.DATA / "settings.toml").unlink(missing_ok=True)
    yield
    core._PROXY_UP.clear()
    (core.DATA / "settings.toml").unlink(missing_ok=True)


@pytest.fixture
def listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    yield f"127.0.0.1:{s.getsockname()[1]}"
    s.close()


def dead():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"127.0.0.1:{port}"


def registry(monkeypatch, enabled, server, override="<local>"):
    monkeypatch.setattr(core, "_registry_proxy", lambda: (enabled, server, override))


def test_enabled_reachable_windows_proxy_is_passed_on(monkeypatch, listening):
    registry(monkeypatch, True, listening, "<local>;*.corp.example")
    env = core.account_env({"kind": "subscription", "config_dir": ""})
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://" + listening
    assert "localhost" in env["NO_PROXY"] and "*.corp.example" in env["NO_PROXY"] and "<local>" not in env["NO_PROXY"]


def test_switched_off_proxy_is_ignored(monkeypatch, listening):
    registry(monkeypatch, False, listening)
    assert core.proxy_env() == {}


def test_dead_proxy_is_ignored_instead_of_causing_connection_refused(monkeypatch):
    registry(monkeypatch, True, dead())
    assert core.proxy_env() == {}


def test_per_protocol_server_string_prefers_https():
    assert core._proxy_url("http=a:1;https=b:2;ftp=c:3") == "http://b:2"
    assert core._proxy_url("http=a:1") == "http://a:1"
    assert core._proxy_url("socks=a:1") is None
    assert core._proxy_url("127.0.0.1:12334") == "http://127.0.0.1:12334"
    assert core._proxy_url("https://p:8") == "https://p:8"
    assert core._proxy_url("") is None


def test_environment_variables_already_set_win(monkeypatch, listening):
    registry(monkeypatch, True, listening)
    monkeypatch.setenv("HTTPS_PROXY", "http://elsewhere:1")
    assert core.proxy_env() == {}


def test_settings_off_and_explicit_url(monkeypatch, listening):
    registry(monkeypatch, True, listening)
    monkeypatch.setattr(core, "settings", lambda: {"network": {"proxy": "off"}})
    assert core.proxy_env() == {}
    monkeypatch.setattr(core, "settings", lambda: {"network": {"proxy": "http://" + listening}})
    registry(monkeypatch, False, "")  # the explicit URL doesn't need the Windows setting
    assert core.proxy_env()["HTTPS_PROXY"] == "http://" + listening


def test_agents_get_the_proxy(monkeypatch, listening, project):
    registry(monkeypatch, True, listening)
    assert core.options(project).env["HTTPS_PROXY"] == "http://" + listening
