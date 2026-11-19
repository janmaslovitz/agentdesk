from pathlib import Path

from agentdesk import server

HTML = (Path(server.WEB) / "index.html").read_text(encoding="utf-8")


def test_composer_controls():
    assert 'id="attFile"' in HTML and "multiple" in HTML and 'id="attChips"' in HTML
    assert "attPaste(event)" in HTML and "attDrop(" in HTML and '"drop"' in HTML


def test_upload_and_delete_endpoints():
    assert "/attachments?name=${encodeURIComponent(" in HTML
    assert "/attachments/${a.id}/delete" in HTML


def test_send_posts_ids_and_allows_empty_text():
    assert "{text, attachments: ids}" in HTML
    assert "(!text && !atts.length)" in HTML
    assert "attPending = []" in HTML


def test_feed_renders_attachments_escaped():
    assert "meta?.attachments?.length" in HTML
    assert 'loading="lazy"' in HTML and 'target="_blank"' in HTML
    assert "${esc(a.name)}" in HTML and "attUrl(a)" in HTML
