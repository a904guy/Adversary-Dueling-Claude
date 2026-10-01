"""Finding a run's Claude Code sessions, and the run to resume for a folder."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adversary.config import RunConfig

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# The typed header that opens each agent's first prompt (see Bridge.header).
FIRST_PROMPT = {
    "worker": "Here is your task (pasted below).",
    "adversary": "Your supervision brief (pasted below).",
}


def run_started(run_dir: str) -> datetime:
    """Run dirs are named after their local start time, e.g. 20261001-143802."""
    return datetime.strptime(Path(run_dir).name, "%Y%m%d-%H%M%S").astimezone()


def _first_prompt(path: Path) -> tuple[datetime, str] | None:
    try:
        with path.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "user":
                    continue
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                when = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                return when, content
    except (OSError, KeyError, ValueError):
        return None
    return None


def find_sessions(cfg: RunConfig) -> dict[str, str]:
    """Session IDs of a run's worker and adversary.

    Runs record them as their sessions start (cfg.sessions). Older runs didn't, so
    this falls back to Claude Code's session logs for the folder: each agent's
    first prompt starts with a known header and contains the task, and was sent
    within a few minutes of the run starting.
    """
    found = dict(cfg.sessions)
    if all(role in found for role in FIRST_PROMPT):
        return found
    folder = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cfg.repo)
    start = run_started(cfg.run_dir)
    window = (start - timedelta(seconds=30), start + timedelta(minutes=10))
    task_hint = cfg.task.strip()[:200]
    best: dict[str, tuple[datetime, str]] = {}
    for path in folder.glob("*.jsonl"):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < window[0]:
            continue
        first = _first_prompt(path)
        if not first or not (window[0] <= first[0] <= window[1]) or task_hint not in first[1]:
            continue
        for role, header in FIRST_PROMPT.items():
            if role not in found and first[1].startswith(header):
                if role not in best or first[0] < best[role][0]:
                    best[role] = (first[0], path.stem)
    found.update({role: sid for role, (_, sid) in best.items()})
    return found


def runs(state_dir: Path) -> list[RunConfig]:
    """Every run with a readable config, newest first."""
    out = []
    for d in sorted(state_dir.glob("*-*"), reverse=True):
        try:
            out.append(RunConfig.load(str(d)))
        except (OSError, ValueError, TypeError):
            continue
    return out


def latest_run_for(folder: Path, state_dir: Path) -> RunConfig | None:
    """The newest run that worked in `folder`, or was started from it (--worktree)."""
    folder = folder.resolve()
    for cfg in runs(state_dir):
        if folder in {Path(p).resolve() for p in (cfg.repo, cfg.origin) if p}:
            return cfg
    return None


def last_worker_message(run_dir: str) -> str:
    """The worker's last message as recorded in transcript.md."""
    path = Path(run_dir) / "transcript.md"
    if not path.exists():
        return ""
    parts = re.split(r"^## \[\d\d:\d\d:\d\d\] ", path.read_text(), flags=re.M)
    for part in reversed(parts):
        if part.startswith("Worker\n"):
            return part[len("Worker\n"):].strip()
    return ""
