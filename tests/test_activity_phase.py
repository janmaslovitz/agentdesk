"""The activity digest says where a task is in its pipeline, so a chat agent can state it instead of guessing."""

from agentdesk import core, server




def test_phase_after_the_worker_finished_is_checks():
    ev = [{"role": "worker", "kind": "tool", "name": "Bash"}, {"role": "worker", "kind": "result", "turns": 10}]
    assert "running the project's checks" in server.activity_phase({"status": "working"}, ev)


def test_phase_reviewer_and_done():
    rev = [{"role": "worker", "kind": "check", "ok": True}, {"role": "reviewer", "kind": "start", "tier": "review"}]
    assert server.activity_phase({"status": "working"}, rev) == "the reviewer is reading the diff"
    assert "checks passed" in server.activity_phase({"status": "working"}, rev[:1])
    assert server.activity_phase({"status": "done", "branch": "auto/7"}, rev).startswith("done")
    assert "worker is on it" in server.activity_phase({"status": "working"}, [{"role": "worker", "kind": "tool"}])


def test_event_lines_name_the_reviewer():
    assert " reviewer tool Grep" in server._event_line({"at": "2026-11-13T16:44:36", "role": "reviewer", "kind": "tool", "name": "Grep"})
    assert server._event_line({"at": "2026-11-13T16:44:36", "kind": "tool", "name": "Bash"}).startswith("16:44 tool Bash")


def test_verify_rule_forbids_confessing():
    assert "never what you didn't do" in server.VERIFY_RULE and server.VERIFY_RULE in server.COWORKER_PROMPT


def test_no_acknowledgement_openers_and_coworker_acts_on_problems():
    assert "Понял фронт" not in server.ACK_RULE and "never open with" in server.ACK_RULE
    assert "Don't stop at a diagnosis" in server.COWORKER_ROLE
    assert "never ask for permission first" in server.COWORKER_FULL_PROMPT
    assert "awaiting the" in server.MEMORY_PROMPT and "explicit yes" in server.MEMORY_PROMPT
