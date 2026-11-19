from pathlib import Path

from agentdesk import server

HTML = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")


def test_section_inside_settings_dialog():
    dlg = HTML[HTML.index('id="dProject"'):]
    assert dlg.index('id="pSetSections"') < dlg.index('id="psChatAgents"')
    assert "Chat agents" in HTML and 'id="caBody"' in HTML and 'onclick="caSave()"' in HTML


def test_loads_and_posts_to_chat_agents():
    assert "/chat/agents`" in HTML
    assert "caLoad();" in HTML and "async function caSave" in HTML
    assert "caChanges()" in HTML


def test_fields_resets_and_errors():
    for k in ("budget_usd", "daily_budget_usd", "max_turns", "tools_off", "prompt_extra", "enabled", "effort"):
        assert k in HTML
    assert "reset to default" in HTML and "v = null" in HTML
    assert "project override" in HTML and "Effective:" in HTML
    assert "custom id" in HTML
    assert "caE_" in HTML and "r.field" in HTML


def test_refreshes_chat_model():
    body = HTML[HTML.index("async function caSave"):]
    assert "refresh()" in body[:900]
    assert '$("chatModel").textContent' in HTML
