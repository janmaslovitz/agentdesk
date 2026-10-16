"""Tests run against a temporary AGENTDESK_HOME (set before agentdesk is imported) and a scratch git repo."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="agentdesk-test-"))
os.environ["AGENTDESK_HOME"] = str(HOME)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"  # a system proxy must not intercept the tests' local servers
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from agentdesk import core  # noqa: E402

assert core.DATA == HOME, "agentdesk was imported before AGENTDESK_HOME was set"


@pytest.fixture
def project(tmp_path):
    """A fresh project on a scratch git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start"], cwd=repo, check=True)
    core.settings()
    name = f"scratch-{tmp_path.name}"
    p = core.add_project(name, str(repo), "main")
    yield p


@pytest.fixture(autouse=True)
def no_real_memory_call(monkeypatch):
    """The chat's memory update is a real model call: tests never make it unless they patch it themselves."""
    from agentdesk import server

    async def none(p, model, prompt):
        return ""

    monkeypatch.setattr(server, "memory_call", none)
