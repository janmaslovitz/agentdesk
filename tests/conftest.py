"""Tests run against a temporary AGENTDESK_HOME (set before agentdesk is imported) and a scratch git repo."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="agentdesk-test-"))
os.environ["AGENTDESK_HOME"] = str(HOME)
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
