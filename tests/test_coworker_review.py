"""The Coworker as reviewer and delegator: it reads what workers produce before it reports, hands big goals to the
architect, runs the graph in parallel, and the shared verify rule never invites "I haven't read it"."""

import asyncio
import subprocess

import pytest

from agentdesk import core, server

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, server.LOOP).result(5)


@pytest.fixture
def proj(project):
    (project.repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project.repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=project.repo, check=True)
    core.set_chat_mode(project, "coworker")
    return project


@pytest.fixture
def chat(proj):
    c = server.Chat(server.LOOP, proj)
    yield c
    c.approvals.deny_all()


def tool(chat, name, approval=False):
    return next(t for t in chat.tools(approval=approval) if t.name == name)


def finished_task(p, tid, title="Upstream inventory", report="Found 5143 upstream commits; 157 conflicts expected."):
    core.add_task(p, tid, title, "analyse")
    wt = p.repo.parent / f"wt-{tid}"
    subprocess.run(["git", "worktree", "add", "-q", "-b", f"auto/{tid}", str(wt), "main"], cwd=p.repo, check=True)
    (wt / "REPORT.md").write_text("findings\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", f"{tid}: {title}"], cwd=wt, check=True)
    p.path("reports").mkdir(exist_ok=True)
    p.path("reports").joinpath(f"{tid}.md").write_text(f"# {tid}\n\n{report}\n", encoding="utf-8")
    st = core.Store(p).load()
    st["tasks"][tid] = {"status": "done", "branch": f"auto/{tid}", "commit": "abc1234", "title": title,
                        "started": "2026-11-12T10:00:00", "finished": "2026-11-12T10:05:00"}
    core.Store(p).save(st)


def test_verify_rule_never_invites_unread_confessions():
    assert "say so plainly" not in server.VERIFY_RULE
    assert "is never a statement to make" in server.VERIFY_RULE
    for prompt in (server.COWORKER_PROMPT, server.COWORKER_FULL_PROMPT, server.PLANNER_PROMPT, server.CHAT_PROMPT):
        assert "Check before you report" in prompt


def test_role_makes_the_coworker_a_reviewer_and_delegator():
    role = server.COWORKER_ROLE
    assert "review(id)" in role and "Unread work is not finished work" in role
    assert "architect(goal)" in role and "in parallel" in role
    assert "never as a fact about the assistant" in server.MEMORY_PROMPT


def test_run_all_is_described_as_parallel(chat):
    assert "in parallel" in tool(chat, "run_all").description
    assert "one after another" not in tool(chat, "run_all").description


def test_coworker_and_assistant_tool_sets(chat):
    cw = {t.name for t in chat.tools(approval=False)}
    assert {"architect", "plan", "approve_plan", "cancel_plan", "graph", "review", "integrate"} <= cw
    asst = {t.name for t in chat.tools(approval=True)}
    assert {"architect", "plan", "graph", "review"} <= asst
    assert not {"approve_plan", "integrate"} & asst  # the user approves plans on the card
    assert "architect" in server.NEEDS_APPROVAL


def test_review_shows_report_and_branch_changes(chat, proj):
    finished_task(proj, "037")
    out = run(tool(chat, "review").handler({"id": "037"}))["content"][0]["text"]
    assert "5143 upstream commits" in out and "auto/037" in out and "REPORT.md" in out and "037: Upstream inventory" in out
    bad = run(tool(chat, "review").handler({"id": "999"}))
    assert bad.get("is_error")


def test_run_finished_wakeup_lists_tasks_and_demands_review(proj):
    finished_task(proj, "037")
    finished_task(proj, "038", "Local features")
    text = server.run_finished_prompt(proj, "2026-11-12T09:59:00")
    assert "037 done" in text and "038 done" in text and "reports/037.md" in text
    assert "review(id)" in text and "before you write to the user" in text
    assert "037" not in server.run_finished_prompt(proj, "2026-11-12T10:30:00").split("\n", 1)[1].split("Now,")[0]


def test_plan_notes_in_coworker_mode_do_not_ask_the_user(chat, proj, monkeypatch):
    monkeypatch.setattr(chat, "is_live", lambda: True)
    assert chat.coworking()
    core.propose_plan(proj, "s", [{"title": "A", "goal": "a"}])
    ids = core.approve_plan(proj)
    st = core.Store(proj).load()
    st["tasks"][ids["d1"]] = {"status": "blocked", "last_failure": "$ cargo fmt --all --check"}
    core.Store(proj).save(st)
    notes = []
    monkeypatch.setattr(chat, "note", notes.append)
    chat.plan_tick()
    assert notes and "The Coworker looks into it" in notes[0] and "tell me how to go on" not in notes[0]
    monkeypatch.setattr(chat, "is_live", lambda: False)
    assert not chat.coworking()


def test_plan_text_and_graph_text(proj):
    core.add_task(proj, "1", "Core", "x")
    core.add_task(proj, "2", "Next", "y", None, ["1"])
    g = server.graph_text(proj)
    assert "L0 1 ready" in g and "L1 2 waiting" in g and "after 1" in g
    assert server.plan_text(proj) == "no plan"
    core.propose_plan(proj, "Do it", [{"title": "A", "goal": "build a", "files": ["a.py"]}])
    t = server.plan_text(proj)
    assert "draft plan" in t and "files: a.py" in t and "goal: build a" in t


def test_default_chat_models():
    import tomllib
    chat = tomllib.loads(core.DEFAULT_SETTINGS)["chat"]
    assert chat["coworker"]["model"] == "sonnet" and chat["assistant"]["model"] == "haiku"
    assert (chat["planner"]["model"], chat["planner"]["effort"]) == ("sonnet", "medium")


def test_planner_can_hand_planning_to_the_architect(chat):
    names = {t.name for t in chat.planner_tools()}
    assert {"propose_plan", "update_plan", "cancel_plan", "architect"} <= names
    assert "architect" in server.PLANNER_TOOLS
    assert "together with the user" in server.PLANNER_PROMPT and "architect(goal)" in server.PLANNER_PROMPT


def test_confirm_patterns_cover_only_important_actions():
    must = ["git push origin main", "gh release create v1", "git remote set-url origin x", "git reset --hard HEAD",
            "git branch -D x", "cargo publish", "curl -X POST https://e.x", "systemctl restart x"]
    free = ["git merge auto/1", "git branch -d x", "gh pr list", "gh api repos/x", "cargo test", "curl -s https://e.x",
            "git checkout main", "rm -rf /tmp/x"]
    assert all(core.coworker_confirm_reason(c) for c in must)
    assert not any(core.coworker_confirm_reason(c) for c in free)
