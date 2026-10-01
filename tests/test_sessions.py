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
    cfg.sessions = {"worker": "w", "adversary": "a"}   # recorded IDs win
    assert sessions.find_sessions(cfg) == {"worker": "w", "adversary": "a"}


def test_last_worker_message(tmp_path):
    (tmp_path / "transcript.md").write_text(
        "# t\n\n## [10:00:00] Worker\n\nfirst\n\n## [10:01:00] Adversary (worker_msg)\n\nfix\n\n"
        "## [10:02:00] Worker\n\nsecond\n\n## [10:03:00] approved #3\n")
    assert sessions.last_worker_message(str(tmp_path)) == "second"
