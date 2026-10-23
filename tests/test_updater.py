"""In-app updater: version compare, asset choice, URL allow-list, failures swallowed, install flow."""
import io
import json

import pytest

from agentdesk import core, server


def rel(tag="v9.0.0", assets=None):
    return {"tag_name": tag, "body": "notes", "assets": assets if assets is not None else [
        {"name": "AgentDesk-Setup-9.0.0.exe", "size": 3, "browser_download_url": "https://github.com/x/y/releases/download/v9/AgentDesk-Setup-9.0.0.exe"}]}


def test_version_compare_is_numeric():
    assert core.parse_version("v1.10.0") > core.parse_version("1.9.9")
    assert core.parse_version("junk") is None
    assert core.update_from_release(rel("v1.10.0"), "1.9.0")["available"]
    assert not core.update_from_release(rel("v1.2.0"), "1.2.0")["available"]
    assert not core.update_from_release(rel("v1.2"), "1.2.0")["available"]
    assert core.update_from_release(rel("nightly"), "1.0.0")["status"] == "unknown"


def test_asset_selection():
    a = core.pick_installer_asset([{"name": "src.zip", "browser_download_url": "https://github.com/a"},
                                   rel()["assets"][0]])
    assert a["name"] == "AgentDesk-Setup-9.0.0.exe"
    assert core.pick_installer_asset([{"name": "AgentDesk-Setup-1.exe", "browser_download_url": "https://evil.com/a.exe"}]) is None
    assert not core.update_from_release(rel(assets=[]), "1.0.0", installer=True)["available"]


def test_url_allow_list():
    ok = core.update_url_ok
    assert ok("https://github.com/a/b") and ok("https://objects.githubusercontent.com/x")
    assert not ok("http://github.com/a") and not ok("https://github.com.evil.com/a")
    assert not ok("https://evil.com/github.com") and not ok("file:///c:/x") and not ok("https://github.com:8443/a")


def test_check_failures_are_swallowed_and_cached(monkeypatch):
    import urllib.request
    calls = []
    def boom(*a, **k):
        calls.append(1)
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setitem(core._UPDATE, "info", None)
    r = core.check_update(force=True)
    assert r["status"] == "unknown" and not r["available"]
    core.check_update()
    assert len(calls) == 1  # cached


def test_download_refuses_bad_url_and_verifies_checksum(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError):
        core.download_update({"asset": {"name": "a.exe", "url": "https://evil.com/a.exe", "size": 1, "sha256": None}})
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    import urllib.request
    class Op:
        def open(self, req, timeout=0): return R(b"abc")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Op())
    url = "https://github.com/a/AgentDesk-Setup-1.exe"
    good = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    p = core.download_update({"asset": {"name": "AgentDesk-Setup-1.exe", "url": url, "size": 3, "sha256": good}})
    assert p.read_bytes() == b"abc"
    with pytest.raises(RuntimeError, match="checksum"):
        core.download_update({"asset": {"name": "AgentDesk-Setup-1.exe", "url": url, "size": 3, "sha256": "0" * 64}})
    with pytest.raises(RuntimeError, match="incomplete"):
        core.download_update({"asset": {"name": "AgentDesk-Setup-1.exe", "url": url, "size": 9, "sha256": None}})


def test_install_flow(monkeypatch, tmp_path):
    info = core.update_from_release(rel(), "1.0.0")
    monkeypatch.setattr(core, "check_update", lambda force=False: info)
    assert "installed" in server.update_install()["message"]  # dev mode: refuses
    monkeypatch.setattr(core, "FROZEN", True)
    monkeypatch.setattr(server.os, "name", "nt")
    launched = []
    monkeypatch.setattr(core, "download_update", lambda i, progress=None: tmp_path / "s.exe")
    monkeypatch.setattr(server, "launch_installer", lambda p: launched.append(p))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "any_run_active", lambda: True)
    assert server.update_install()["needs_confirm"]
    monkeypatch.setitem(server.UPDATE_JOB, "state", "idle")
    assert server.update_install(confirm=True)["ok"]
    import time
    for _ in range(50):
        if launched:
            break
        time.sleep(0.05)
    assert launched == [tmp_path / "s.exe"]
    server.QUIT.clear()


def test_mac_and_linux_get_a_link_to_the_release_page():
    """No in-app installer off Windows: a newer release is enough, and the badge links to its page."""
    r = {**rel(assets=[]), "html_url": "https://github.com/x/y/releases/tag/v9.0.0"}
    info = core.update_from_release(r, "1.0.0", installer=False)
    assert info["available"] and info["page"] == "https://github.com/x/y/releases/tag/v9.0.0"
    assert not core.update_from_release(r, "9.0.0", installer=False)["available"]
    assert core.update_from_release({**r, "html_url": "https://evil.com/x"}, "1.0.0", installer=False)["page"] == core.RELEASES_PAGE
    assert core.update_from_release(rel("junk"), "1.0.0", installer=False)["page"] == core.RELEASES_PAGE


def test_update_status_links_on_mac_linux_and_installs_on_windows(monkeypatch):
    info = {**core.update_from_release(rel(), "1.0.0", installer=False), "page": "https://github.com/x/y/releases/tag/v9.0.0"}
    monkeypatch.setattr(core, "check_update", lambda force=False: info)
    monkeypatch.setattr(server, "any_run_active", lambda: False)
    monkeypatch.setattr(core, "FROZEN", True)
    monkeypatch.setattr(server.os, "name", "posix")
    st = server.update_status()
    assert st["available"] and st["link_only"] and not st["installable"] and st["page"] == info["page"]
    assert "only works in the installed program" in server.update_install()["message"]  # never downloads off Windows
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url))
    assert server.update_open()["ok"] and opened == [info["page"]]
    monkeypatch.setattr(core, "check_update", lambda force=False: {**info, "page": "https://evil.com/x"})
    server.update_open()
    assert opened[-1] == core.RELEASES_PAGE
    monkeypatch.setattr(server.os, "name", "nt")
    st = server.update_status()
    assert st["installable"] and not st["link_only"]  # Windows: unchanged, the button installs


def test_the_page_offers_the_link_off_windows():
    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    assert "upd.link_only" in html and "/api/update/open" in html
