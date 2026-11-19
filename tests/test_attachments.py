"""Chat attachments (agentdesk/attachments.py): safe names, content checks, limits, claim/prompt, removal."""

import io
import json
import os
import re
import time
from types import SimpleNamespace

import pytest

from agentdesk import attachments as att
from agentdesk import core

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@pytest.fixture
def world(tmp_path):
    """A fake project inside tmp_path/world, plus a sentinel tree next to it that must never change."""
    w = tmp_path / "world"
    pdir = w / "proj"
    pdir.mkdir(parents=True)
    (w / "other").mkdir()
    (w / "other" / "keep.txt").write_text("keep")
    p = SimpleNamespace(dir=pdir, cfg={})
    return SimpleNamespace(p=p, w=w, root=pdir / "attachments")


def tree(path):
    return sorted(str(q.relative_to(path)) for q in path.rglob("*"))


def outside(world):
    """Everything in the world tree that is not under <project>/attachments."""
    return [t for t in tree(world.w) if not t.startswith(os.path.join("proj", "attachments"))]


def put(world, name, data, length=None):
    return att.store(world.p, name, io.BytesIO(data), len(data) if length is None else length)


# ----------------------------------------------------------------------------- sanitize_name

@pytest.mark.parametrize("raw,want", [
    ("../../etc/passwd", "passwd"),
    ("/etc/shadow", "shadow"),
    ("C:\\Windows\\System32\\evil.txt", "evil.txt"),
    ("..\\..\\boot.ini", "boot.ini"),
    ("a/b\\c.txt", "c.txt"),
    ("..", "file"), ("", "file"), ("   ", "file"), (".hidden", "hidden"), ("...x.txt", "x.txt"),
    ("a\x00b\x1f.txt", "ab.txt"),
    ("we<ird>:name?.txt", "we_ird__name_.txt"),
    ("résumé café.md", "résumé café.md"),
    ("отчёт 日本語.txt", "отчёт 日本語.txt"),
    ("my file-1_2.v3.py", "my file-1_2.v3.py"),
])
def test_sanitize_cases(raw, want):
    assert att.sanitize_name(raw) == want


@pytest.mark.parametrize("raw", ["CON", "con.txt", "NUL.png", "Com1.log", "LPT9.md", "aux"])
def test_sanitize_reserved(raw):
    out = att.sanitize_name(raw)
    assert out.split(".")[0].upper() not in att.RESERVED
    assert out.endswith(raw[raw.find("."):] if "." in raw else raw)


def test_sanitize_overlong_keeps_extension():
    out = att.sanitize_name("x" * 500 + ".png")
    assert len(out) <= 100 and out.endswith(".png")
    out = att.sanitize_name("é" * 300)
    assert len(out) <= 100
    out = att.sanitize_name("a." + "b" * 300)
    assert len(out) <= 100


def test_sanitize_idempotent():
    for raw in ["../x y.TXT", "CON.txt", "日本" * 80 + ".md", "a<b>.py"]:
        once = att.sanitize_name(raw)
        assert att.sanitize_name(once) == once


# ----------------------------------------------------------------------------- limits

def test_limits_defaults_and_policy(monkeypatch):
    monkeypatch.setattr(core, "settings", lambda: {})
    monkeypatch.setattr(core, "server_policy", lambda: {})
    lim = att.limits()
    assert (lim["max_file_mb"], lim["max_files"], lim["max_project_mb"], lim["text_inline_kb"]) == (10, 5, 200, 20)
    assert {"png", "pdf", "txt", "py"} <= set(lim["extensions"]) and "svg" not in lim["extensions"]
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"attachments": {"max_file_mb": 50, "max_files": 3, "text_inline_kb": "bad"}}})
    lim = att.limits()
    assert lim["max_file_mb"] == 50 and lim["max_files"] == 3 and lim["text_inline_kb"] == 20
    monkeypatch.setattr(core, "server_policy", lambda: {"max_upload_mb": 4})
    assert att.limits()["max_file_mb"] == 4


# ----------------------------------------------------------------------------- store: accepted

def test_store_valid_types(world):
    cases = [("a.png", PNG, "image", "image/png"), ("b.JPG", JPG, "image", "image/jpeg"), ("c.jpeg", JPG, "image", "image/jpeg"),
             ("d.gif", GIF, "image", "image/gif"), ("e.webp", WEBP, "image", "image/webp"),
             ("f.pdf", PDF, "pdf", "application/pdf"), ("g.txt", "héllo\n".encode(), "text", "text/plain"),
             ("h.py", b"print(1)\n", "text", "text/x-python"), ("i.json", b'{"a": 1}', "text", "application/json"),
             ("j.md", b"# hi", "text", "text/markdown")]
    for name, data, kind, mime in cases:
        m = put(world, name, data)
        assert re.fullmatch(r"[0-9a-f]{12}", m["id"])
        assert (m["kind"], m["mime"], m["size"], m["sent"]) == (kind, mime, len(data), False)
        assert set(m) == {"id", "name", "orig_name", "size", "mime", "kind", "created", "sent"}
        f = att.file_path(world.p, m["id"])
        assert f == world.root / m["id"] / m["name"] and f.read_bytes() == data
        assert json.loads((world.root / m["id"] / "meta.json").read_text(encoding="utf-8")) == m
        assert att.get(world.p, m["id"]) == m
    assert outside(world) == ["other", os.path.join("other", "keep.txt"), "proj"]
    # no leftovers of the upload temp files
    assert not [t for t in tree(world.root) if os.sep not in t and t.startswith(".tmp")]


def test_store_sanitizes_name(world):
    m = put(world, "../../x/..\\evil name.txt", b"hi")
    assert m["name"] == "evil name.txt" and m["orig_name"] == "evil name.txt"
    assert att.file_path(world.p, m["id"]).parent.parent == world.root
    assert outside(world) == ["other", os.path.join("other", "keep.txt"), "proj"]


# ----------------------------------------------------------------------------- store: refused

@pytest.mark.parametrize("name,data", [
    ("fake.png", b"this is not a png at all"),
    ("fake.jpg", PNG), ("fake.pdf", b"hello"), ("fake.webp", PNG),
    ("pic.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("run.exe", b"MZ\x90\x00" + b"\x00" * 60),
    ("tool.dll", b"MZ\x90\x00"), ("x.bat", b"echo hi"), ("noext", b"hello"),
    ("bad.txt", b"\xff\xfe\xfa not utf8 \x80"),
    ("bin.txt", b"abc\x00def"),
    ("elf.py", b"\x7fELF\x02\x01\x01"),
])
def test_store_refuses(world, name, data):
    with pytest.raises(att.AttachmentError):
        put(world, name, data)
    assert [t for t in outside(world) if t.startswith("other")] == ["other", os.path.join("other", "keep.txt")]
    # nothing is left in the attachments folder either: no id folders, no temp files
    assert not (world.root.exists() and tree(world.root))


def test_store_non_utf8_split_across_chunks(world, monkeypatch):
    monkeypatch.setattr(att, "CHUNK", 4)
    assert put(world, "ok.txt", "ééé日本".encode())["kind"] == "text"
    with pytest.raises(att.AttachmentError):
        put(world, "bad.txt", "ééé日".encode()[:-1])


def test_store_short_stream_cleans_up(world):
    before = tree(world.root) if world.root.exists() else []
    with pytest.raises(att.AttachmentError):
        put(world, "a.txt", b"short", length=100)
    assert (tree(world.root) if world.root.exists() else []) == before


def test_store_reads_only_length(world):
    s = io.BytesIO(b"abcdefghij")
    m = att.store(world.p, "a.txt", s, 4)
    assert att.file_path(world.p, m["id"]).read_bytes() == b"abcd"


class Exploding(io.BytesIO):
    def read(self, n=-1):
        if self.tell() >= 4:
            raise OSError("connection reset")
        return super().read(min(n, 4))


def test_store_stream_error_cleans_up(world):
    with pytest.raises(att.AttachmentError):
        att.store(world.p, "a.txt", Exploding(b"x" * 100), 100)
    assert not (world.root.exists() and tree(world.root))
    assert [t for t in outside(world) if t.startswith("other")] == ["other", os.path.join("other", "keep.txt")]


# ----------------------------------------------------------------------------- limits and gate

def test_size_limits(world, monkeypatch):
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"attachments": {"max_file_mb": 1}}})
    monkeypatch.setattr(core, "server_policy", lambda: {})
    with pytest.raises(att.AttachmentError, match="larger"):
        att.store(world.p, "big.txt", io.BytesIO(b""), 1024 * 1024 + 1)
    with pytest.raises(att.AttachmentError):
        put(world, "empty.txt", b"")
    with pytest.raises(att.AttachmentError):
        att.store(world.p, "neg.txt", io.BytesIO(b"abc"), -1)
    assert put(world, "edge.txt", b"a" * (1024 * 1024))["size"] == 1024 * 1024


def test_server_policy_caps_file_size(world, monkeypatch):
    monkeypatch.setattr(core, "server_policy", lambda: {"max_upload_mb": 1})
    with pytest.raises(att.AttachmentError):
        att.store(world.p, "big.txt", io.BytesIO(b""), 2 * 1024 * 1024)


def test_project_total_limit(world, monkeypatch):
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"attachments": {"max_file_mb": 1, "max_project_mb": 2}}})
    monkeypatch.setattr(core, "server_policy", lambda: {})
    mb = 1024 * 1024
    put(world, "a.txt", b"a" * (mb - 2000))  # (meta.json files count towards the total too)
    put(world, "b.txt", b"b" * (mb - 2000))
    with pytest.raises(att.AttachmentError, match="exceed"):
        put(world, "c.txt", b"c" * 5000)
    assert sorted(att.get(world.p, i)["name"] for i in os.listdir(world.root)) == ["a.txt", "b.txt"]


def test_disk_gate_refuses(world, monkeypatch):
    monkeypatch.setattr(core, "disk_gate", lambda: "the server's disk is full")
    with pytest.raises(att.AttachmentError, match="disk is full"):
        put(world, "a.txt", b"hi")
    assert not world.root.exists()
    assert [t for t in outside(world) if t.startswith("other")] == ["other", os.path.join("other", "keep.txt")]


# ----------------------------------------------------------------------------- get / file_path

@pytest.mark.parametrize("bad", ["", "../etc", "..", "ABCDEF012345", "0123456789a", "0123456789abc", "0123456789ag", None, 5, "/abs/path00000"])
def test_get_rejects_bad_ids(world, bad):
    put(world, "a.txt", b"hi")
    assert att.get(world.p, bad) is None and att.file_path(world.p, bad) is None


def test_get_unknown_and_tampered(world):
    m = put(world, "a.txt", b"hi")
    assert att.get(world.p, "0" * 12) is None
    meta_f = world.root / m["id"] / "meta.json"
    meta_f.write_text(json.dumps({**m, "name": "../../../other/keep.txt"}), encoding="utf-8")
    assert att.file_path(world.p, m["id"]) is None
    meta_f.write_text("not json")
    assert att.get(world.p, m["id"]) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_entries_are_not_followed(world):
    m = put(world, "a.txt", b"hi")
    link = world.root / ("f" * 12)
    link.symlink_to(world.w / "other", target_is_directory=True)
    assert att.get(world.p, "f" * 12) is None and att.file_path(world.p, "f" * 12) is None
    f = world.root / m["id"] / "a.txt"
    f.unlink()
    f.symlink_to(world.w / "other" / "keep.txt")
    assert att.file_path(world.p, m["id"]) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_attachments_dir_refused(world):
    target = world.w / "other"
    world.root.symlink_to(target, target_is_directory=True)
    before = tree(world.w)
    with pytest.raises(att.AttachmentError):
        put(world, "a.txt", b"hi")
    assert tree(world.w) == before and (target / "keep.txt").read_text() == "keep"
    assert att.sweep(world.p, set()) == 0
    assert att.remove_all(world.p) == 0
    assert target.is_dir() and (target / "keep.txt").read_text() == "keep"
    assert att.get(world.p, "0" * 12) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_project_dir_reached_through_link_refused(world):
    link = world.w / "proj-link"
    link.symlink_to(world.p.dir, target_is_directory=True)
    p = SimpleNamespace(dir=link, cfg={})
    before = tree(world.w)
    with pytest.raises(att.AttachmentError):
        att.store(p, "a.txt", io.BytesIO(b"hi"), 2)
    assert tree(world.w) == before


def test_missing_project_dir_is_not_created(world):
    p = SimpleNamespace(dir=world.w / "gone", cfg={})
    with pytest.raises(att.AttachmentError):
        att.store(p, "a.txt", io.BytesIO(b"hi"), 2)
    assert not (world.w / "gone").exists()


# ----------------------------------------------------------------------------- claim

def test_claim_marks_sent_and_refuses_reuse(world):
    a, b = put(world, "a.txt", b"A"), put(world, "b.png", PNG)
    got = att.claim(world.p, [a["id"], b["id"]])
    assert [m["id"] for m in got] == [a["id"], b["id"]] and all(m["sent"] for m in got)
    assert att.get(world.p, a["id"])["sent"] is True
    with pytest.raises(att.AttachmentError, match="already"):
        att.claim(world.p, [a["id"]])


def test_claim_refuses_unknown_duplicate_and_too_many(world, monkeypatch):
    a = put(world, "a.txt", b"A")
    with pytest.raises(att.AttachmentError):
        att.claim(world.p, ["0" * 12])
    with pytest.raises(att.AttachmentError):
        att.claim(world.p, ["../" + a["id"]])
    with pytest.raises(att.AttachmentError, match="twice"):
        att.claim(world.p, [a["id"], a["id"]])
    with pytest.raises(att.AttachmentError):
        att.claim(world.p, "not a list")
    assert att.get(world.p, a["id"])["sent"] is False  # nothing was marked by the refusals
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"attachments": {"max_files": 2}}})
    ids = [put(world, f"f{i}.txt", b"x")["id"] for i in range(3)]
    with pytest.raises(att.AttachmentError, match="at most 2"):
        att.claim(world.p, ids)
    assert all(att.get(world.p, i)["sent"] is False for i in ids)
    # one bad id among good ones marks none
    with pytest.raises(att.AttachmentError):
        att.claim(world.p, [ids[0], "1" * 12])
    assert att.get(world.p, ids[0])["sent"] is False


# ----------------------------------------------------------------------------- prompt_block

def test_prompt_block(world, monkeypatch):
    monkeypatch.setattr(core, "settings", lambda: {"chat": {"attachments": {"text_inline_kb": 1}}})
    img, pdf = put(world, "shot.png", PNG), put(world, "doc.pdf", PDF)
    small = put(world, "notes.md", b"hello `code` ```fence``` world\n")
    big = put(world, "big.txt", b"Z" * 2000)
    metas = att.claim(world.p, [img["id"], pdf["id"], small["id"], big["id"]])
    text = att.prompt_block(world.p, metas)
    for m in metas:
        path = str(att.file_path(world.p, m["id"]).absolute())
        assert os.path.isabs(path) and path in text and m["name"] in text and m["mime"] in text
    assert "Read tool" in text
    assert "hello `code` ```fence``` world" in text
    assert "ZZZZ" not in text  # over the inline limit: path only
    assert "````" in text  # the fence is longer than the content's longest backtick run
    assert att.prompt_block(world.p, []) == ""


def test_prompt_block_text_only_has_no_read_hint(world):
    m = att.claim(world.p, [put(world, "a.txt", b"inline me")["id"]])
    t = att.prompt_block(world.p, m)
    assert "inline me" in t and "Read tool" not in t


def test_prompt_block_missing_file(world):
    m = att.claim(world.p, [put(world, "a.txt", b"x")["id"]])
    att.remove_all(world.p)
    assert "no longer available" in att.prompt_block(world.p, m)


# ----------------------------------------------------------------------------- delete / remove_all / sweep

def test_delete_only_unsent(world):
    a, b = put(world, "a.txt", b"A"), put(world, "b.txt", b"B")
    att.claim(world.p, [b["id"]])
    assert att.delete(world.p, b["id"]) is False and att.get(world.p, b["id"])
    assert att.delete(world.p, a["id"]) is True and att.get(world.p, a["id"]) is None
    assert att.delete(world.p, a["id"]) is False
    assert att.delete(world.p, "../other") is False
    assert (world.w / "other" / "keep.txt").exists()


def test_remove_all_stays_inside(world):
    put(world, "a.txt", b"A" * 100)
    put(world, "b.png", PNG)
    (world.p.dir / "project.toml").write_text("x")
    freed = att.remove_all(world.p)
    assert freed >= 100 + len(PNG)
    assert not world.root.exists()
    assert (world.p.dir / "project.toml").read_text() == "x"
    assert [t for t in outside(world) if t.startswith("other")] == ["other", os.path.join("other", "keep.txt")]
    assert att.remove_all(world.p) == 0


def _age(world, aid, hours):
    f = world.root / aid / "meta.json"
    m = json.loads(f.read_text(encoding="utf-8"))
    m["created"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - hours * 3600))
    f.write_text(json.dumps(m), encoding="utf-8")


def test_sweep(world):
    fresh = put(world, "fresh.txt", b"F")
    old = put(world, "old.txt", b"O" * 50)
    _age(world, old["id"], 30)
    sent_kept, sent_gone = put(world, "k.txt", b"K"), put(world, "g.txt", b"G" * 70)
    att.claim(world.p, [sent_kept["id"], sent_gone["id"]])
    _age(world, sent_kept["id"], 99)
    _age(world, sent_gone["id"], 99)
    stale = world.root / ".tmp-stale"
    stale.write_bytes(b"x" * 10)
    os.utime(stale, (time.time() - 48 * 3600,) * 2)
    young = world.root / ".tmp-young"
    young.write_bytes(b"y")
    stranger = world.root / "notes-by-user.txt"   # not ours: left alone
    stranger.write_text("hands off")
    freed = att.sweep(world.p, {sent_kept["id"]})
    assert freed >= 50 + 70 + 10
    assert att.get(world.p, fresh["id"]) and att.get(world.p, sent_kept["id"])
    assert att.get(world.p, old["id"]) is None and att.get(world.p, sent_gone["id"]) is None
    assert not stale.exists() and young.exists() and stranger.read_text() == "hands off"
    assert [t for t in outside(world) if t.startswith("other")] == ["other", os.path.join("other", "keep.txt")]
    assert att.sweep(world.p, {sent_kept["id"]}) == 0


def test_sweep_respects_cutoff_argument(world):
    m = put(world, "a.txt", b"A")
    _age(world, m["id"], 2)
    assert att.sweep(world.p, set(), unsent_older_than_h=24) == 0
    assert att.sweep(world.p, set(), unsent_older_than_h=1) > 0


def test_sweep_does_not_follow_links(world):
    if os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    put(world, "a.txt", b"A")
    (world.root / ("e" * 12)).symlink_to(world.w / "other", target_is_directory=True)
    att.sweep(world.p, set(), unsent_older_than_h=0)
    assert (world.w / "other" / "keep.txt").read_text() == "keep"


# ----------------------------------------------------------------------------- with a real project

def test_real_project_confined(project):
    before = {q for q in project.dir.rglob("*")}
    m = att.store(project, "a.txt", io.BytesIO(b"hi"), 2)
    new = {q for q in project.dir.rglob("*")} - before
    root = project.dir / "attachments"
    assert new and all(q == root or root in q.parents for q in new)
    assert att.remove_all(project) > 0 and att.get(project, m["id"]) is None
