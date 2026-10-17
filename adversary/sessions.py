"""Finding a run's Claude Code sessions, and the run to resume for a folder."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adversary.config import RunConfig

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# The typed headers that can open each agent's first prompt (see Bridge.header). The worker's
# first prompt is the supervisor's plan on current runs, or the task itself on runs made before
# the plan-first change; either prefix identifies a worker session.
FIRST_PROMPT = {
    "worker": ("Your supervisor's plan for this task (pasted below).", "Here is your task (pasted below)."),
    "adversary": ("Your supervision brief (pasted below).",),
}
# Headers whose first prompt does NOT quote the task: the plan the adversary writes is in its own
# words, so the folder-scan fallback can't use the task text to recognise that worker session.
HEADERS_WITHOUT_TASK = {"Your supervisor's plan for this task (pasted below)."}


def session_log(cfg: RunConfig, role: str) -> Path | None:
    """Claude Code's JSONL log for a role's recorded session, if one is recorded."""
    sid = cfg.sessions.get(role)
    if not sid:
        return None
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cfg.repo) / f"{sid}.jsonl"


def run_started(run_dir: str) -> datetime:
    """Run dirs are named after their local start time, e.g. 20261001-143802, with a random
    -#### after it on newer runs so two started in the same second don't collide."""
    return datetime.strptime(Path(run_dir).name[:15], "%Y%m%d-%H%M%S").astimezone()


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

    Runs record them as their sessions start (cfg.sessions). A recorded id is trusted only
    if its log still holds a real conversation: a failed resume can leave a stub session
    (metadata lines only), and the bridge may have recorded that stub, so resuming it would
    open an empty session and the pane would die. A stub or missing id is dropped and found
    again from Claude Code's session logs for the folder — each agent's first prompt starts
    with a known header and contains the task, within a few minutes of the run starting, and
    no other run has recorded it. For older runs that recorded nothing, this is the only source.
    """
    folder = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cfg.repo)
    found = {role: sid for role, sid in cfg.sessions.items()
             if sid and _first_prompt(folder / f"{sid}.jsonl") is not None}
    if all(role in found for role in FIRST_PROMPT):
        return found
    start = run_started(cfg.run_dir)
    window = (start - timedelta(seconds=30), start + timedelta(minutes=10))
    # Several runs can share a folder and start together (a manager's pool), so a session another
    # run has already recorded is never this one's, however well its time and header match.
    taken = {sid for other in runs(Path(cfg.run_dir).parent) if other.run_dir != cfg.run_dir
             for sid in other.sessions.values() if sid}
    task_hint = cfg.task.strip()[:200]
    best: dict[str, tuple[float, str]] = {}   # role -> (mtime, stem); the most recently written wins
    for path in folder.glob("*.jsonl"):
        mtime = path.stat().st_mtime
        if datetime.fromtimestamp(mtime, timezone.utc) < window[0]:
            continue
        if path.stem in taken:
            continue
        first = _first_prompt(path)
        if not first or not (window[0] <= first[0] <= window[1]):
            continue
        for role, headers in FIRST_PROMPT.items():
            if role in found:
                continue
            for h in headers:
                if not first[1].startswith(h):
                    continue
                # Every first prompt quotes the task except the adversary's plan, which is in its
                # own words; for that one the time window and newest-mtime have to carry the match.
                if h not in HEADERS_WITHOUT_TASK and task_hint not in first[1]:
                    break
                if role not in best or mtime > best[role][0]:
                    best[role] = (mtime, path.stem)
                break
    found.update({role: stem for role, (_, stem) in best.items()})
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
