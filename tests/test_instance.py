import socket

from agentdesk import server


def test_answers_false_on_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert not server.answers(port)


def test_answers_true_when_listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert server.answers(s.getsockname()[1])
    finally:
        s.close()


def test_main_second_start_opens_browser_and_returns(monkeypatch):
    opened = []
    monkeypatch.setattr(server, "answers", lambda port: True)
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    server.main(port=8765, browser=True)
    assert opened == ["http://127.0.0.1:8765"]


def test_stop_without_pid_file(monkeypatch, tmp_path, capsys):
    from agentdesk import __main__ as cli
    monkeypatch.setattr(cli.core, "DATA", tmp_path)
    cli.cmd_stop(False)
    assert "not running" in capsys.readouterr().out


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_main(monkeypatch, **kw):
    class Fake:  # a server whose loop ends at once, so main() returns after the window/browser step
        def __init__(self, *a, **k):
            pass

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    port = _free_port()
    monkeypatch.setattr(server, "ThreadingHTTPServer", Fake)
    server.main(port=port, **kw)
    return port


def test_browser_flag_opens_browser_not_window(monkeypatch):
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    monkeypatch.setattr(server, "open_window", lambda url: opened.append("WINDOW") or True)
    port = _run_main(monkeypatch, browser=True)
    assert opened == [f"http://127.0.0.1:{port}"]


def test_missing_webview_falls_back_to_browser_and_logs(monkeypatch, capsys):
    import sys
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview -> ImportError
    port = _run_main(monkeypatch)
    assert opened == [f"http://127.0.0.1:{port}"]
    assert "cannot open the native window" in capsys.readouterr().err
    assert "cannot open the native window" in (server.core.DATA / "agentdesk.log").read_text(encoding="utf-8")


def test_window_settings_adds_missing_keeps_existing():
    path = server.core.DATA / "settings.toml"
    server.core.settings()
    path.write_text(path.read_text(encoding="utf-8") + "\n[window]\nwidth = 900\n", encoding="utf-8")
    w = server.window_settings()
    assert w["width"] == 900 and w["height"] == server.WINDOW_DEFAULTS["height"]
    assert server.window_settings({"x": 10, "y": 20})["x"] == 10
