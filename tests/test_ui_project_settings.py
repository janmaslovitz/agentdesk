from pathlib import Path

from agentdesk import server

HTML = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")


def test_settings_dialog_and_fields():
    assert HTML.count('id="dProject"') == 1
    assert "openDlg('dProject')" in HTML and ">Settings</button>" in HTML
    for i in ("pSetSections", "psName", "psBase", "psAccount", "psQuick", "psFinal", "psTimeout", "psToml"):
        assert f'id="{i}"' in HTML
    assert "Advanced: project.toml" in HTML
    assert "/settings`" in HTML and "{toml:" in HTML


def test_new_project_dialog_kept_separate():
    assert 'id="dNewProject"' in HTML and "openDlg('dNewProject')" in HTML


def test_delete_requires_typed_slug():
    assert "Delete project…" in HTML
    assert "Also delete the code repository" in HTML
    assert "/delete`" in HTML
    assert "confirm: true" in HTML and "delete_repo:" in HTML
    assert '.includes("/repos/")' in HTML
    assert "psData.slug" in HTML and 'id="psDelGo"' in HTML and "disabled" in HTML
