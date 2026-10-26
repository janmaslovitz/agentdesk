import pytest

from agentdesk import core


@pytest.mark.parametrize("url,want", [
    ("https://github.com/you/My_Repo.git", "My_Repo"),
    ("https://github.com/you/My_Repo/", "My_Repo"),
    ("git@github.com:you/repo.git", "repo"),
    ("ssh://git@host/a/b/c.git", "c"),
    ("https://host/x/y.git?ref=1", "y"),
])
def test_repo_name(url, want):
    assert core.import_folder_name(url=url) == want


@pytest.mark.parametrize("fn,want", [
    ("my-project-1.2.zip", "my-project"),
    ("my-project-1.2.3.ZIP", "my-project"),
    ("my project (1).zip", "my-project"),
    ("app_20240101-120000.zip", "app"),
    ("plain.zip", "plain"),
    ("1.2.zip", "1.2"),
])
def test_zip_name(fn, want):
    assert core.import_folder_name(filename=fn) == want


def test_priority():
    assert core.import_folder_name("Typed", "https://h/a/r.git", "f.zip", "Inner") == "Typed"
    assert core.import_folder_name("", "https://h/a/r.git", "f.zip", "Inner") == "r"
    assert core.import_folder_name("", "", "f-1.0.zip", "Inner Name") == "Inner-Name"


@pytest.mark.parametrize("raw,want", [
    ("../x", "x"), ("..", "project"), ("a/b\\c", "a-b-c"), ("  .hidden  ", "hidden"), ("Проект 1", "Проект-1"),
    ("a  b---c", "a-b-c"), ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("com1", "_com1"), ("", "project"), ("a..b", "a.b"),
])
def test_slug(raw, want):
    assert core.folder_slug(raw) == want


def test_slug_length_and_chars():
    s = core.folder_slug("x" * 200)
    assert len(s) <= 64
    assert "/" not in core.folder_slug("a/../../b") and ".." not in core.folder_slug("a/../../b")


def test_collisions_and_root(tmp_path):
    root = tmp_path / "repos"
    a = core.make_import_dir("My_Repo", root)
    b = core.make_import_dir("My_Repo", root)
    c = core.make_import_dir("My_Repo", root)
    assert (a.name, b.name, c.name) == ("My_Repo", "My_Repo-2", "My_Repo-3")
    assert core.preview_import_dir("My_Repo", root).name == "My_Repo-4"
    for d in (a, b, c, core.make_import_dir("../../evil", root)):
        assert d.resolve().parent == root.resolve()


def test_existing_nonempty_folder_not_reused(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "f").write_text("1")
    assert core.make_import_dir("x", tmp_path).name == "x-2"
