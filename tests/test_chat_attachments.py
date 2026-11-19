"""Chat attachments in the server: upload, serve, send with a message, and cleanup."""

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, PermissionResultAllow, PermissionResultDeny, ResultMessage, TextBlock

from agentdesk import attachments as att
from agentdesk import core, server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


# ----------------------------------------------------------------------------- helpers

@pytest.fixture
def http(project, monkeypatch):
    monkeypatch.setattr(server, "_WINDOW", None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(method, path, data=None, ctype="application/json"):
        headers = {"Host": f"127.0.0.1:{srv.server_port}"}
        body = None
        if method == "POST":
            headers["Origin"] = base
            headers["Content-Type"] = ctype
            body = data if isinstance(data, bytes) else json.dumps(data or {}).encode()
        req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
        try:
            r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r = e
        return r.status, dict(r.headers), r.read()

    def jcall(method, path, data=None, ctype="application/json"):
        code, _h, body = call(method, path, data, ctype)
        return code, json.loads(body)

    call.json = jcall
    call.base = f"/api/p/{project.slug}"
    yield call
    srv.shutdown()


def upload(http, name, data, ctype="application/octet-stream"):
    return http.json("POST", f"{http.base}/attachments?name={urllib.parse.quote(name)}", data, ctype)[1]


class FakeClient:
    made: list = []

    def __init__(self, options):
        self.options = options
        self.texts: list[str] = []
        FakeClient.made.append(self)

    async def connect(self):
        pass

    async def query(self, text):
        self.texts.append(text)

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="ok")], model="sonnet")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1",
                            total_cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 1})

    async def interrupt(self):
        pass

    async def disconnect(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeClient.made = []
    monkeypatch.setattr(server, "ClaudeSDKClient", FakeClient)
    return FakeClient


@pytest.fixture
def chat(project):
    c = server.Chat(server.LOOP, project)
    yield c
    c.approvals.deny_all()


def stored(p, name="notes.txt", data=b"hello there\n"):
    import io
    return att.store(p, name, io.BytesIO(data), len(data))


def wait_idle(chat, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not chat.busy:
            return
        time.sleep(0.01)
    raise AssertionError("the chat stayed busy")


# ----------------------------------------------------------------------------- upload and serve

def test_upload_then_get_roundtrip_with_security_headers(http, project):
    r = upload(http, "pic.png", PNG, "image/png")
    assert r["ok"] is True
    m = r["attachment"]
    assert m["name"] == "pic.png" and m["mime"] == "image/png" and m["size"] == len(PNG) and m["sent"] is False
    code, h, body = http("GET", f"{http.base}/attachments/{m['id']}")
    assert code == 200 and body == PNG
    h = {k.lower(): v for k, v in h.items()}
    assert h["content-type"] == "image/png"
    assert h["x-content-type-options"] == "nosniff"
    assert h["content-security-policy"] == "sandbox"
    assert h["content-disposition"].startswith("inline")
    assert (project.dir / "attachments" / m["id"] / "pic.png").read_bytes() == PNG


def test_non_images_are_served_as_attachments(http):
    m = upload(http, "doc.pdf", PDF, "application/pdf")["attachment"]
    _, h, body = http("GET", f"{http.base}/attachments/{m['id']}")
    h = {k.lower(): v for k, v in h.items()}
    assert body == PDF and h["content-type"] == "application/pdf"
    assert h["content-disposition"] == 'attachment; filename="doc.pdf"'
    assert h["x-content-type-options"] == "nosniff" and h["content-security-policy"] == "sandbox"
    m = upload(http, "page.html", b"<script>alert(1)</script>")["attachment"]
    _, h, _ = http("GET", f"{http.base}/attachments/{m['id']}")
    h = {k.lower(): v for k, v in h.items()}
    assert h["content-disposition"].startswith("attachment") and h["content-security-policy"] == "sandbox"


@pytest.mark.parametrize("aid", ["nope", "../state", "000000000000", "ZZZZZZZZZZZZ", "..%2F..%2Fchat.json"])
def test_get_bad_ids_are_404(http, aid):
    assert http("GET", f"{http.base}/attachments/{aid}")[0] == 404


def test_refused_uploads(http, project):
    r = upload(http, "evil.svg", b"<svg onload=alert(1)/>")
    assert r["ok"] is False and "SVG" in r["message"]
    r = upload(http, "run.exe", b"MZ\x90\x00")
    assert r["ok"] is False and r["message"]
    r = upload(http, "fake.png", b"not a png")
    assert r["ok"] is False and "png" in r["message"]
    r = upload(http, "empty.txt", b"")
    assert r["ok"] is False
    root = project.dir / "attachments"
    leftovers = [q for q in root.glob("*")] if root.exists() else []
    assert leftovers == []


def test_oversized_upload_is_refused(http, project, monkeypatch):
    monkeypatch.setattr(att, "limits", lambda p=None: {**att.DEFAULTS, "max_file_mb": 1, "extensions": []})
    r = upload(http, "big.txt", b"a" * (1024 * 1024 + 1))
    assert r["ok"] is False and "1 MB" in r["message"]
    assert not any((project.dir / "attachments").glob("*")) if (project.dir / "attachments").exists() else True


def test_upload_to_unknown_project_is_404(http):
    code, r = http.json("POST", "/api/p/no-such-project-x/attachments?name=a.txt", b"hi", "application/octet-stream")
    assert code == 404


def test_upload_is_not_read_as_json(http, project):
    """A text upload that happens to be JSON must be stored, not interpreted as a request body."""
    r = upload(http, "data.json", json.dumps({"text": "x", "attachments": []}).encode())
    assert r["ok"] is True and r["attachment"]["kind"] == "text"


def test_delete_unsent_upload(http, project):
    m = upload(http, "a.txt", b"hi")["attachment"]
    code, r = http.json("POST", f"{http.base}/attachments/{m['id']}/delete", {})
    assert code == 200 and r == {"ok": True}
    assert http("GET", f"{http.base}/attachments/{m['id']}")[0] == 404
    assert http.json("POST", f"{http.base}/attachments/{m['id']}/delete", {})[0] == 404
    assert http.json("POST", f"{http.base}/attachments/not-an-id/delete", {})[0] == 404


def test_delete_refuses_a_sent_attachment(project):
    m = stored(project)
    att.claim(project, [m["id"]])
    assert att.delete(project, m["id"]) is False
    assert att.file_path(project, m["id"]) is not None


def test_traversal_names_never_write_outside_the_attachments_folder(http, project):
    sentinel = project.dir / "chat_rules.md"
    sentinel.write_text("keep", encoding="utf-8")
    before = sorted(str(q.relative_to(project.dir)) for q in project.dir.rglob("*") if "attachments" not in q.parts)
    outside_dir = project.dir.parent
    outside_before = sorted(q.name for q in outside_dir.iterdir())
    for name in ("../../evil.txt", "..\\..\\evil.txt", "/etc/evil.txt", "../chat.json", "..", "a/../../b.txt"):
        r = upload(http, name, b"payload")
        if r["ok"]:
            f = att.file_path(project, r["attachment"]["id"])
            assert f is not None and Path(os.path.realpath(f)).is_relative_to(os.path.realpath(project.dir / "attachments"))
    after = sorted(str(q.relative_to(project.dir)) for q in project.dir.rglob("*") if "attachments" not in q.parts)
    assert after == before and sentinel.read_text(encoding="utf-8") == "keep"
    assert sorted(q.name for q in outside_dir.iterdir()) == outside_before
    assert not (project.dir / "evil.txt").exists() and not (outside_dir / "evil.txt").exists()


# ----------------------------------------------------------------------------- sending with a chat message

def test_chat_post_with_attachments_stores_them_and_tells_the_agent(http, project, fake, monkeypatch):
    m1 = upload(http, "pic.png", PNG, "image/png")["attachment"]
    m2 = upload(http, "note.txt", b"remember the milk\n")["attachment"]
    # the shared server chat for this project is the one the route uses
    c = server.chat(project)
    monkeypatch.setattr(c, "update_memory", lambda *a: None)
    code, r = http.json("POST", f"{http.base}/chat", {"text": "look at these", "attachments": [m1["id"], m2["id"]]})
    assert code == 200 and r.get("started") is True
    wait_idle(c)
    user = [m for m in c.history if m["role"] == "user"][-1]
    assert user["text"] == "look at these"
    assert user["attachments"] == [{k: m[k] for k in ("id", "name", "size", "mime", "kind")} for m in (m1, m2)]
    saved = json.loads(project.path("chat.json").read_text(encoding="utf-8"))
    assert [m for m in saved if m["role"] == "user"][-1]["attachments"] == user["attachments"]
    sent = fake.made[-1].texts[-1]
    assert sent.startswith("look at these")
    assert str(Path(os.path.abspath(att.file_path(project, m1["id"])))) in sent
    assert str(Path(os.path.abspath(att.file_path(project, m2["id"])))) in sent
    assert "remember the milk" in sent  # a small text file is inlined
    # the page gets them with the transcript
    info = http.json("GET", f"{http.base}/chat")[1]
    assert [m for m in info["history"] if m["role"] == "user"][-1]["attachments"][0]["id"] == m1["id"]


def test_chat_post_may_have_no_text_when_files_are_attached(http, project, fake, monkeypatch):
    m = upload(http, "note.txt", b"just a file\n")["attachment"]
    c = server.chat(project)
    monkeypatch.setattr(c, "update_memory", lambda *a: None)
    code, r = http.json("POST", f"{http.base}/chat", {"text": "", "attachments": [m["id"]]})
    assert code == 200 and r.get("started") is True
    wait_idle(c)
    assert "just a file" in fake.made[-1].texts[-1]
    code, r = http.json("POST", f"{http.base}/chat", {"text": "  ", "attachments": []})
    assert r == {"error": "empty"}


def test_reusing_a_sent_id_is_refused(http, project, fake, monkeypatch):
    m = upload(http, "note.txt", b"once\n")["attachment"]
    c = server.chat(project)
    monkeypatch.setattr(c, "update_memory", lambda *a: None)
    assert http.json("POST", f"{http.base}/chat", {"text": "one", "attachments": [m["id"]]})[1].get("started") is True
    wait_idle(c)
    n = len(c.history)
    code, r = http.json("POST", f"{http.base}/chat", {"text": "two", "attachments": [m["id"]]})
    assert code == 400 and "already sent" in r["error"]
    assert len(c.history) == n  # nothing was added or sent
    code, r = http.json("POST", f"{http.base}/chat", {"text": "three", "attachments": ["0123456789ab"]})
    assert code == 400 and "unknown" in r["error"]
    code, r = http.json("POST", f"{http.base}/chat", {"text": "four", "attachments": "x"})
    assert code == 400


def test_a_busy_chat_does_not_consume_the_attachment(project, fake):
    c = server.Chat(server.LOOP, project)
    m = stored(project)
    c.busy = True
    assert "error" in c.start("hi", [m["id"]])
    assert att.get(project, m["id"])["sent"] is False
    c.busy = False


def test_user_entry_without_attachments_is_unchanged(chat, fake, monkeypatch):
    monkeypatch.setattr(chat, "update_memory", lambda *a: None)
    chat.ask("plain")
    assert chat.history[0] == {"role": "user", "text": "plain"}
    assert fake.made[-1].texts[-1] == "plain"


def test_memory_gets_the_plain_text(chat, fake, monkeypatch):
    seen = []
    monkeypatch.setattr(chat, "update_memory", lambda user, reply: seen.append(user))
    m = stored(chat.p)
    metas = att.claim(chat.p, [m["id"]])
    chat.ask("what is this", metas)
    assert seen == ["what is this"]
    assert "notes.txt" in fake.made[-1].texts[-1]


def test_live_send_appends_the_attachment_block(chat, project, monkeypatch):
    """Coworker live mode: the same text + prompt_block reaches the live client, the memory prompt stays plain."""
    sent = []

    class Live:
        async def query(self, text):
            sent.append(text)

    async def client(mode):
        return Live()

    async def pump(*a):
        return None

    monkeypatch.setattr(chat, "_client", client)
    monkeypatch.setattr(chat, "_pump", pump)
    monkeypatch.setattr(chat, "is_live", lambda: True)
    m = stored(project, "pic.png", PNG)
    out = chat.start("see this", [m["id"]])
    assert out == {"started": True, "live": True}
    assert sent[0].startswith("see this") and str(Path(os.path.abspath(att.file_path(project, m["id"])))) in sent[0]
    assert chat.live_prompt == "see this"
    assert chat.history[-1]["attachments"][0]["id"] == m["id"] and chat.history[-1]["text"] == "see this"


# ----------------------------------------------------------------------------- cleanup

def test_reset_removes_attachments_but_keep_history_keeps_them(chat, project):
    m = stored(project)
    root = project.dir / "attachments"
    chat.reset(keep_history=True)
    assert (root / m["id"]).is_dir()
    chat.reset()
    assert not root.exists()


def test_sweep_on_chat_creation_keeps_referenced_files(project):
    kept = stored(project, "kept.txt", b"in chat.json\n")
    archived = stored(project, "arch.txt", b"in the archive\n")
    orphan = stored(project, "orphan.txt", b"sent but unreferenced\n")
    fresh = stored(project, "fresh.txt", b"uploaded, not sent yet\n")
    stale = stored(project, "stale.txt", b"uploaded long ago\n")
    att.claim(project, [kept["id"], archived["id"], orphan["id"]])
    entry = lambda m: {"id": m["id"], "name": m["name"], "size": m["size"], "mime": m["mime"], "kind": m["kind"]}
    project.path("chat.json").write_text(json.dumps([{"role": "user", "text": "x", "attachments": [entry(kept)]}]), encoding="utf-8")
    project.path("chat_archive.jsonl").write_text(json.dumps({"role": "user", "text": "old", "attachments": [entry(archived)]}) + "\n", encoding="utf-8")
    meta_path = project.dir / "attachments" / stale["id"] / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created"] = "2000-01-01T00:00:00"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    server.Chat(server.LOOP, project)
    assert att.file_path(project, kept["id"]) is not None
    assert att.file_path(project, archived["id"]) is not None
    assert att.file_path(project, fresh["id"]) is not None  # a recent unsent upload survives
    assert att.file_path(project, orphan["id"]) is None
    assert att.file_path(project, stale["id"]) is None


# ----------------------------------------------------------------------------- what the chat modes may do there

def target(project):
    return str(project.dir / "attachments" / "abcdef012345" / "pic.png")


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, server.LOOP).result(5)


def test_every_mode_can_read_attachments_and_nothing_more(chat, project, monkeypatch):
    t = target(project)
    # assistant: reads are allowed, writes only to tasks/*.md and notes.md
    assert isinstance(run(chat.permission("Read", {"file_path": t})), PermissionResultAllow)
    assert isinstance(run(chat.permission("Write", {"file_path": t})), PermissionResultDeny)
    # planner
    assert isinstance(run(chat.planner_permission("Read", {"file_path": t})), PermissionResultAllow)
    for tool in ("Write", "Edit", "Bash"):
        assert isinstance(run(chat.planner_permission(tool, {"file_path": t, "command": "rm x"})), PermissionResultDeny)
    # guarded coworker: Read allowed, edits there refused
    real = core.chat_settings
    monkeypatch.setattr(core, "chat_settings", lambda mode: {**real(mode), "full_access": False})
    assert isinstance(run(chat.coworker_permission("Read", {"file_path": t})), PermissionResultAllow)
    assert run(chat.coworker_hook({"tool_name": "Read", "tool_input": {"file_path": t}})) == {}
    for tool in ("Write", "Edit"):
        assert isinstance(run(chat.coworker_permission(tool, {"file_path": t})), PermissionResultDeny)
    denied = run(chat.coworker_hook({"tool_name": "Write", "tool_input": {"file_path": t}}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_chat_modes_can_reach_the_project_folder(chat, project):
    """cwd or add_dirs of each mode's options include the project folder, where attachments live."""
    for build in (chat._assistant_options, chat._coworker_options, chat._planner_options):
        o = build()
        dirs = [str(o.cwd), *map(str, o.add_dirs)]
        assert str(project.dir) in dirs
