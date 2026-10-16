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
