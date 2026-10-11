import json
import re
from pathlib import Path

from adversary import sessions
from adversary.config import RunConfig


def make_run(state, ts, repo, task="Build it.", origin=None):
    d = state / ts
    d.mkdir(parents=True)
    cfg = RunConfig(task=task, repo=str(repo), run_dir=str(d), sock="s", base_sha=None, origin=origin)
    cfg.save()
    return cfg


def write_session(folder: Path, sid: str, when: str, prompt: str):
    folder.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "summary"}, {"type": "user", "timestamp": when, "message": {"content": prompt}}]
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def write_stub(folder: Path, sid: str):
    """A session log a failed resume leaves behind: metadata only, no conversation."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "ai-title", "sessionId": sid}, {"type": "agent-name", "sessionId": sid}]
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_latest_run_for_matches_repo_or_origin(tmp_path):
    state, repo, other = tmp_path / "runs", tmp_path / "repo", tmp_path / "other"
    repo.mkdir(), other.mkdir()
    make_run(state, "20261001-100000", repo)
    newest = make_run(state, "20261001-110000", repo)
    make_run(state, "20261001-120000", other)
    wt = make_run(state, "20261001-130000", tmp_path / "repo-wt", origin=str(other))
    assert sessions.latest_run_for(repo, state).run_dir == newest.run_dir
    assert sessions.latest_run_for(other, state).run_dir == wt.run_dir
    assert sessions.latest_run_for(tmp_path, state) is None


def test_find_sessions_from_claude_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", tmp_path / "projects")
    repo = tmp_path / "my.repo"
    repo.mkdir()
    cfg = make_run(tmp_path / "runs", "20261001-143802", repo, task="Read PLAN.md and build it.")
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    start = sessions.run_started(cfg.run_dir).astimezone().isoformat()
    write_session(folder, "worker-1", start, "Here is your task (pasted below). Treat it as my request: Read PLAN.md and build it.")
    write_session(folder, "adv-1", start, "Your supervision brief (pasted below). Follow it: <task>\nRead PLAN.md and build it.\n</task>")
    write_session(folder, "other", start, "Here is your task (pasted below). Treat it as my request: something else")
    write_session(folder, "old", "2020-01-01T00:00:00Z", "Here is your task (pasted below). Read PLAN.md and build it.")
    assert sessions.find_sessions(cfg) == {"worker": "worker-1", "adversary": "adv-1"}
    # Recorded IDs win when their logs are real sessions (no folder scan needed).
    write_session(folder, "w", start, "whatever")
    write_session(folder, "a", start, "whatever")
    cfg.sessions = {"worker": "w", "adversary": "a"}
    assert sessions.find_sessions(cfg) == {"worker": "w", "adversary": "a"}


def test_find_sessions_drops_a_stub_recorded_id(tmp_path, monkeypatch):
    """A failed resume can record a stub session; resuming it would open an empty pane, so the
    real session must be found again from the folder."""
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", tmp_path / "projects")
    repo = tmp_path / "my.repo"
    repo.mkdir()
    cfg = make_run(tmp_path / "runs", "20261001-143802", repo, task="Read PLAN.md and build it.")
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    start = sessions.run_started(cfg.run_dir).astimezone().isoformat()
    write_session(folder, "worker-real", start, "Here is your task (pasted below). Treat it as my request: Read PLAN.md and build it.")
    write_stub(folder, "worker-stub")
    cfg.sessions = {"worker": "worker-stub", "adversary": "adv-missing"}   # stub + a file that doesn't exist
    write_session(folder, "adv-real", start, "Your supervision brief (pasted below). Follow it: Read PLAN.md and build it.")
    assert sessions.find_sessions(cfg) == {"worker": "worker-real", "adversary": "adv-real"}


def test_find_sessions_finds_a_plan_header_worker_without_the_task_text(tmp_path, monkeypatch):
    """New runs hand the worker the adversary's plan (in its own words), so its first prompt no
    longer quotes the task. The folder scan must still recognise it by the plan header."""
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", tmp_path / "projects")
    repo = tmp_path / "my.repo"
    repo.mkdir()
    cfg = make_run(tmp_path / "runs", "20261001-143802", repo, task="Read PLAN.md and build it.")
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    start = sessions.run_started(cfg.run_dir).astimezone().isoformat()
    write_session(folder, "worker-plan", start,
                  "Your supervisor's plan for this task (pasted below). Follow it as if I wrote it: "
                  "First write add(), then run the suite until green.")   # no task text in the plan
    write_session(folder, "adv-real", start,
                  "Your supervision brief (pasted below). Follow it: Read PLAN.md and build it.")
    write_session(folder, "other", start,
                  "Here is your task (pasted below). Treat it as my request: a different task entirely")
    cfg.sessions = {"worker": "worker-stub", "adversary": "adv-missing"}   # stub + missing: force the scan
    write_stub(folder, "worker-stub")
    found = sessions.find_sessions(cfg)
    assert found == {"worker": "worker-plan", "adversary": "adv-real"}


def test_last_worker_message(tmp_path):
    (tmp_path / "transcript.md").write_text(
        "# t\n\n## [10:00:00] Worker\n\nfirst\n\n## [10:01:00] Adversary (worker_msg)\n\nfix\n\n"
        "## [10:02:00] Worker\n\nsecond\n\n## [10:03:00] approved #3\n")
    assert sessions.last_worker_message(str(tmp_path)) == "second"


def test_resume_note_without_instructions_or_worker_message():
    from adversary.cli import resume_note
    note = resume_note(False, "No response requested.", None)
    assert "<worker_message>" not in note and "ended without a message" in note
    assert "no new instructions" in note and "call hold" in note and "NOTE: the human" not in note
    assert "ended without a message" in resume_note(False, "\u200b", None)


def test_resume_note_with_instructions_and_worker_message():
    from adversary.cli import resume_note
    note = resume_note(True, "Tests pass.", "Add a --json flag.")
    assert "<worker_message>\nTests pass.\n</worker_message>" in note
    assert "> Add a --json flag." in note and "human's new instructions" in note
    assert "no new instructions" not in note and "open again" in note
