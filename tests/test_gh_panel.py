"""Git window, GitHub side: gh accounts, repositories, pull requests, remote URL checks. gh itself is faked: these
tests never run the real gh or touch the network."""
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agentdesk import core, server

TOKEN = "gho_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # fake, shaped like a gh token

AUTH_JSON = json.dumps({"hosts": {"github.com": [
    {"state": "success", "active": True, "host": "github.com", "login": "sintec-llc", "tokenSource": "keyring",
     "scopes": "gist, read:org, repo, workflow", "gitProtocol": "https"},
    {"state": "success", "active": False, "host": "github.com", "login": "janmaslovitz", "tokenSource": "keyring",
     "scopes": "gist, read:org, repo, workflow", "gitProtocol": "ssh"}]}})

AUTH_TEXT = """github.com
  ✓ Logged in to github.com account sintec-llc (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'

  ✓ Logged in to github.com account janmaslovitz (keyring)
  - Active account: false
  - Git operations protocol: ssh
  - Token: gho_************************************
"""


def g(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def ident(monkeypatch, tmp_path):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "nogc"), "GIT_CONFIG_SYSTEM": str(tmp_path / "nogc")}.items():
        monkeypatch.setenv(k, v)
    core._gh_forget()
    yield
    core._gh_forget()


class FakeGh:
    """Stands in for the gh executable: answers by argument prefix and records every call with its environment."""
    EXE = "FAKE-GH"

    def __init__(self):
        self.answers: list[tuple[tuple, int, str]] = []
        self.calls: list[tuple[list[str], dict]] = []
        self.git_calls: list[tuple[list[str], dict]] = []

    def on(self, *prefix, code=0, out=""):
        self.answers.insert(0, (prefix, code, out))

    def answer(self, args):
        for prefix, code, out in self.answers:
            if tuple(args[:len(prefix)]) == prefix:
                return code, out
        return 1, f"unexpected gh call: {args}"


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGh()
    real_run, real_which = subprocess.run, core.shutil.which

    def run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == FakeGh.EXE:
            fake.calls.append((cmd[1:], kw.get("env") or {}))
            code, out = fake.answer(cmd[1:])
            return subprocess.CompletedProcess(cmd, code, out, "")
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            fake.git_calls.append((cmd[1:], kw.get("env") or {}))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(core.subprocess, "run", run)
    monkeypatch.setattr(core.shutil, "which", lambda name, *a, **k: FakeGh.EXE if name == "gh" else real_which(name, *a, **k))
    fake.on("auth", "status", out=AUTH_JSON)
    fake.on("auth", "token", out=TOKEN + "\n")
    return fake


# ----------------------------------------------------------------------------- pure parts

def test_parse_accounts_json_and_text():
    for out in (AUTH_JSON, AUTH_TEXT):
        rows = core.parse_gh_accounts(out)
        assert [(r["login"], r["active"], r["protocol"], r["host"], r["ok"]) for r in rows] == [
            ("sintec-llc", True, "https", "github.com", True), ("janmaslovitz", False, "ssh", "github.com", True)]
    assert core.parse_gh_accounts('{"hosts": {}}') == [] and core.parse_gh_accounts("") == []
    bad = json.dumps({"hosts": {"github.com": [{"state": "error", "active": True, "login": "x", "error": "token " + TOKEN + " is invalid"}]}})
    (row,) = core.parse_gh_accounts(bad)
    assert not row["ok"] and TOKEN not in row["error"]
    old = "github.com\n  ✓ Logged in to github.com as olduser (oauth_token)\n"
    assert core.parse_gh_accounts(old)[0] | {} == {"host": "github.com", "login": "olduser", "active": True, "ok": True, "protocol": "https", "error": ""}


def test_switch_command():
    assert core.gh_switch_cmd("janmaslovitz") == ["auth", "switch", "--hostname", "github.com", "--user", "janmaslovitz"]
    assert core.gh_switch_cmd("a-b", "ghe.example.com")[-3:] == ["ghe.example.com", "--user", "a-b"]
    for bad in ("", "-x", "--help", "a b", "a;b", "a/b", "$(x)"):
        with pytest.raises(ValueError):
            core.gh_switch_cmd(bad)
    with pytest.raises(ValueError):
        core.gh_switch_cmd("ok", "-h")


@pytest.mark.parametrize("text,url", [
    ("sintec-llc/AgentDesk", "https://github.com/sintec-llc/AgentDesk.git"),
    ("sintec-llc/AgentDesk.git", "https://github.com/sintec-llc/AgentDesk.git"),
    ("  owner/re.po_x-1 ", "https://github.com/owner/re.po_x-1.git"),
    ("https://github.com/owner/name", "https://github.com/owner/name"),
    ("https://github.com/owner/name.git", "https://github.com/owner/name.git"),
    ("https://ghe.example.com:8443/team/sub/name.git", "https://ghe.example.com:8443/team/sub/name.git"),
    ("git@github.com:owner/name.git", "git@github.com:owner/name.git"),
    ("ssh://git@github.com/owner/name.git", "ssh://git@github.com/owner/name.git"),
    ("ssh://git@github.com:22/owner/name", "ssh://git@github.com:22/owner/name"),
])
def test_remote_accepted(text, url):
    assert core.normalize_remote(text) == (url, None)


@pytest.mark.parametrize("text", [
    "", "   ", "-upload-pack=evil", "--mirror", "owner/name; rm -rf /", "owner/name && x", "https://github.com/a/b|x",
    "https://github.com/a/$(whoami)", "`x`/y", "https://github.com/a b/c", "a/b\nc", "https://user:pw@github.com/a/b",
    f"https://x-access-token:{TOKEN}@github.com/a/b", "ext::sh -c x", "ext::cmd", "file:///c/repo", "C:\\repos\\x",
    "/home/me/repo.git", "owner", "a/b/c", "http://github.com/a/b", "-/x", "owner/..", "owner/-x", "o'x/y",
])
def test_remote_rejected(text):
    url, err = core.normalize_remote(text)
    assert url is None and err


def test_remote_ssh_protocol_for_owner_name():
    assert core.normalize_remote("o/n", protocol="ssh") == ("git@github.com:o/n.git", None)


def test_parse_github_remote():
    want = {"host": "github.com", "owner": "o", "name": "n", "repo": "o/n"}
    for u in ("https://github.com/o/n.git", "https://github.com/o/n", "git@github.com:o/n.git", "ssh://git@github.com/o/n",
              "https://user@github.com/o/n.git"):
        assert core.parse_github_remote(u) == want, u
    assert core.parse_github_remote("C:/x/remote.git") is None and core.parse_github_remote("") is None


def test_parse_repos_prs_checks():
    repos = core.parse_repos(json.dumps([{"nameWithOwner": "o/a", "url": "https://github.com/o/a", "isPrivate": True},
                                         {"nameWithOwner": "o/b", "url": "https://github.com/o/b", "isPrivate": False}, {"x": 1}]))
    assert repos == [{"name": "o/a", "url": "https://github.com/o/a", "private": True},
                     {"name": "o/b", "url": "https://github.com/o/b", "private": False}]
    assert core.parse_repos("not json") == [] and core.parse_repos("{}") == []
    prs = core.parse_prs(json.dumps([{"number": 7, "title": "Fix", "headRefName": "auto/001", "baseRefName": "main",
                                      "author": {"login": "bob", "is_bot": False}, "url": "https://github.com/o/n/pull/7",
                                      "state": "OPEN", "isDraft": True}]))
    assert prs == [{"number": 7, "title": "Fix", "head": "auto/001", "base": "main", "author": "bob",
                    "url": "https://github.com/o/n/pull/7", "state": "OPEN", "draft": True}]
    checks = core.parse_checks(json.dumps({"statusCheckRollup": [
        {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "conclusion": ""},
        {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING"},
        {"__typename": "StatusContext", "context": "ci/ok", "state": "SUCCESS"}]}))
    assert (checks["passed"], checks["failed"], checks["pending"], checks["skipped"], checks["total"]) == (2, 1, 2, 1, 6)
    assert checks["summary"] == "2 passed, 1 failed, 2 pending, 1 skipped"
    assert core.parse_checks('{"statusCheckRollup": []}')["summary"] == "no checks"


def test_redact():
    s = core.gh_redact(f"push https://x:{TOKEN}@github.com/a/b failed, token {TOKEN} and secret-xyz", "secret-xyz")
    assert TOKEN not in s and "secret-xyz" not in s and "github.com/a/b" in s


# ----------------------------------------------------------------------------- with gh faked

def test_gh_missing_and_logged_out(monkeypatch, gh):
    monkeypatch.setattr(core.shutil, "which", lambda *a, **k: None)
    r = core.gh_accounts(refresh=True)
    assert not r["ok"] and not r["gh"] and "gh auth login" in r["message"] and "cli.github.com" in r["message"]
    monkeypatch.setattr(core.shutil, "which", lambda name, *a, **k: FakeGh.EXE)
    gh.on("auth", "status", code=1, out='{"hosts":{}}')
    r = core.gh_accounts(refresh=True)
    assert not r["ok"] and r["gh"] and "gh auth login" in r["message"]


def test_accounts_cached_briefly(gh):
    assert core.gh_accounts()["ok"] and core.gh_accounts()["ok"]
    assert sum(1 for a, _ in gh.calls if a[:2] == ["auth", "status"]) == 1
    core.gh_accounts(refresh=True)
    assert sum(1 for a, _ in gh.calls if a[:2] == ["auth", "status"]) == 2


def test_account_saved_in_project_toml_not_exported(project):
    core.set_git_account(project, "janmaslovitz")
    assert core.git_account(core.project(project.slug)) == "janmaslovitz"
    assert core.git_status(project)["account"] == "janmaslovitz"
    text = project.path("project.toml").read_text(encoding="utf-8")
    assert "[git]" in text and 'account = "janmaslovitz"' in text
    assert "janmaslovitz" not in core._portable_toml(text)
    core.set_git_account(project, "")
    assert core.git_account(core.project(project.slug)) == ""
    with pytest.raises(ValueError):
        core.set_git_account(project, "-bad")


def test_push_as_account_token_only_in_env(project, gh, tmp_path):
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    g(project.repo, "remote", "add", "origin", "https://github.com/o/n.git")
    core.set_git_account(project, "janmaslovitz")
    cfg, env, who, err = core._git_auth(project, "https://github.com/o/n.git")
    assert err is None and who == "janmaslovitz" and env == {"AGENTDESK_GIT_TOKEN": TOKEN}
    assert all(TOKEN not in a for a in cfg) and cfg[:2] == ["-c", "credential.helper="]
    token_call = next(a for a, _ in gh.calls if a[:2] == ["auth", "token"])
    assert token_call == ["auth", "token", "--hostname", "github.com", "--user", "janmaslovitz"]
    # a local remote keeps git's own credentials: no token is asked for
    assert core._git_auth(project, str(bare)) == ([], {}, None, None)
    # the push itself (to a host that doesn't exist, so it fails fast) gets the token in its env only
    g(project.repo, "remote", "set-url", "origin", "https://127.0.0.1:9/o/n.git")
    r = core.git_push(project)
    push = next((a, e) for a, e in gh.git_calls if "push" in a)
    assert all(TOKEN not in x for x in push[0]) and push[1]["AGENTDESK_GIT_TOKEN"] == TOKEN
    assert not r["ok"] and r["account"] == "janmaslovitz" and "as GitHub account janmaslovitz" in r["message"]
    assert TOKEN not in json.dumps(r)


def test_no_account_no_gh_call(project, gh, tmp_path):
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    g(project.repo, "remote", "add", "origin", str(bare))
    assert core.git_push(project)["ok"] and core.git_fetch(project)["ok"]
    assert gh.calls == []


def test_token_failure_is_a_message(project, gh):
    g(project.repo, "remote", "add", "origin", "https://github.com/o/n.git")
    core.set_git_account(project, "ghost")
    gh.on("auth", "token", code=1, out="no oauth token found for github.com account ghost")
    r = core.git_pull(project)
    assert not r["ok"] and "gh auth login" in r["message"] and r["account"] == "ghost"


def test_set_remote(project, gh):
    r = core.git_set_remote(project, "o/n")
    assert r["ok"] and g(project.repo, "remote", "get-url", "origin") == "https://github.com/o/n.git"
    r = core.git_set_remote(project, "git@github.com:o/other.git")
    assert r["ok"] and "changed" in r["message"] and g(project.repo, "remote", "get-url", "origin") == "git@github.com:o/other.git"
    assert core.git_set_remote(project, "git@github.com:o/other.git")["message"].startswith("origin is already")
    r = core.git_set_remote(project, "--upload-pack=x")
    assert not r["ok"] and g(project.repo, "remote", "get-url", "origin") == "git@github.com:o/other.git"
    core.set_git_account(project, "janmaslovitz")  # its gh protocol is ssh
    assert core.git_set_remote(project, "o/third")["url"] == "git@github.com:o/third.git"


def test_repo_list_and_prs(project, gh):
    gh.on("repo", "list", out=json.dumps([{"nameWithOwner": "sintec-llc/a", "url": "https://github.com/sintec-llc/a", "isPrivate": True}]))
    r = core.gh_repo_list(project)
    assert r["ok"] and r["owner"] == "sintec-llc" and r["account"] == "sintec-llc" and r["repos"][0]["name"] == "sintec-llc/a"
    call = next(a for a, _ in gh.calls if a[:2] == ["repo", "list"])
    assert call == ["repo", "list", "sintec-llc", "--json", "nameWithOwner,url,isPrivate", "--limit", "100"]
    assert not core.gh_repo_list(project, "-x")["ok"]
    assert not core.git_prs(project)["ok"]  # no remote yet
    g(project.repo, "remote", "add", "origin", "https://github.com/o/n.git")
    core.set_git_account(project, "janmaslovitz")
    gh.on("pr", "list", out=json.dumps([{"number": 3, "title": "T", "headRefName": "feat", "baseRefName": "main",
                                         "author": {"login": "a"}, "url": "https://github.com/o/n/pull/3", "state": "OPEN", "isDraft": False}]))
    r = core.git_prs(project)
    assert r["ok"] and r["repo"] == "o/n" and r["account"] == "janmaslovitz" and r["prs"][0]["number"] == 3
    args, env = next((a, e) for a, e in gh.calls if a[:2] == ["pr", "list"])
    assert args[args.index("--repo") + 1] == "github.com/o/n" and env["GH_TOKEN"] == TOKEN
    assert TOKEN not in json.dumps(r)
    gh.on("pr", "view", out=json.dumps({"statusCheckRollup": [{"__typename": "CheckRun", "name": "t", "status": "COMPLETED", "conclusion": "SUCCESS"}]}))
    assert core.git_pr_checks(project, 3)["summary"] == "1 passed"


def test_pr_create(project, gh, tmp_path):
    bare = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    g(project.repo, "remote", "add", "origin", str(bare))
    g(project.repo, "checkout", "-q", "-b", "feat")
    g(project.repo, "commit", "-q", "--allow-empty", "-m", "f")
    assert "not a GitHub repository" in core.git_pr_create(project, "T")["message"]
    g(project.repo, "remote", "set-url", "origin", "https://github.com/o/n.git")
    assert "title" in core.git_pr_create(project, "  ")["message"]
    assert "push it first" in core.git_pr_create(project, "T")["message"]
    g(project.repo, "update-ref", "refs/remotes/origin/feat", "HEAD")  # as if pushed
    assert "valid branch" in core.git_pr_create(project, "T", head="-x")["message"]
    assert "other than the base" in core.git_pr_create(project, "T", head="main")["message"]
    gh.on("pr", "create", out="https://github.com/o/n/pull/9\n")
    r = core.git_pr_create(project, "-dash title", "body", draft=True)
    assert r["ok"] and r["url"] == "https://github.com/o/n/pull/9" and r["account"] == "sintec-llc"
    args = next(a for a, _ in gh.calls if a[:2] == ["pr", "create"])
    assert args == ["pr", "create", "--repo", "github.com/o/n", "--head", "feat", "--base", "main",
                    "--title=-dash title", "--body=body", "--draft"]


def test_http_routes(project, gh, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/api/p/{project.slug}/git"
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda u: opened.append(u))

    def call(path="", body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        r = call("/gh")
        assert r["ok"] and [a["login"] for a in r["accounts"]] == ["sintec-llc", "janmaslovitz"] and r["remote"]["url"] is None
        assert not call("/remote", {"url": "o/n"})["ok"]  # needs confirm
        r = call("/remote", {"url": "o/n", "confirm": True})
        assert r["ok"] and r["status"]["remote_url"] == "https://github.com/o/n.git"
        assert call("/gh")["remote"]["github"]["repo"] == "o/n"
        assert not call("/account", {"account": "janmaslovitz"})["ok"]
        assert call("/account", {"account": "janmaslovitz", "confirm": True})["account"] == "janmaslovitz"
        assert call()["account"] == "janmaslovitz"
        gh.on("auth", "switch", out="✓ Switched active account for github.com to janmaslovitz")
        assert call("/switch", {"account": "janmaslovitz", "confirm": True})["ok"]
        assert next(a for a, _ in gh.calls if a[:2] == ["auth", "switch"]) == core.gh_switch_cmd("janmaslovitz")
        assert not call("/open", {"url": "file:///c:/x"})["ok"] and opened == []
        assert call("/open", {"url": "https://github.com/o/n/pull/1"})["ok"] and opened == ["https://github.com/o/n/pull/1"]
        assert "which pull request" in call("/checks?number=x")["message"]
        for path in ("/gh", "/prs", "/repos"):
            assert TOKEN not in json.dumps(call(path))
        monkeypatch.setattr(server.Runner, "running", lambda self: True)
        assert "in progress" in call("/remote", {"url": "o/m", "confirm": True})["message"]
    finally:
        srv.shutdown()
