"""Run log (transcript.md), final report (report.md), and last-message extraction."""

import json
import re
from datetime import datetime
from pathlib import Path


def last_assistant_text(payload: dict) -> str:
    """The agent's final message for the turn, from the Stop hook payload."""
    if payload.get("last_assistant_message"):
        return payload["last_assistant_message"]
    path = payload.get("transcript_path")
    if not path or not Path(path).exists():
        return ""
    for line in reversed(Path(path).read_text().splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if any(t.strip() for t in texts):
            return "\n".join(texts).strip()
    return ""


def turn_had_tool_use(payload: dict) -> bool:
    """Whether the just-ended turn made any tool call, as opposed to a text-only no-op reply.

    Read commands (Read/Grep/Glob) don't raise a permission prompt in manual mode, so the
    permission count can't tell a turn that did nothing from one that read files. The session
    transcript can: walk back over this turn's assistant entries (stopping at the real prompt
    that began it — tool_result messages are role "user" too, so they don't end the turn) and
    see whether any carried a tool_use block. If the transcript can't be read, assume a tool
    ran, so a missing file never causes the bridge to park a working agent.
    """
    path = payload.get("transcript_path")
    if not path or not Path(path).exists():
        return True
    for line in reversed(Path(path).read_text().splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = entry.get("type")
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            content = []
        if kind == "assistant":
            if any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content):
                return True
        elif kind == "user":
            # A tool_result is delivered as a "user" entry; the turn's real prompt ends the walk.
            if not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                break
    return False


def _tool_summary(name: str, inp, limit: int) -> str:
    """A one-line gist of a tool call: the key argument for common tools, else trimmed JSON."""
    if isinstance(inp, dict):
        for key in ("command", "pattern", "file_path", "path", "url", "query", "prompt"):
            if isinstance(inp.get(key), str) and inp[key].strip():
                return f"{name}: {_clip(inp[key].strip(), limit)}"
        try:
            blob = json.dumps(inp, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = str(inp)
        return f"{name} {_clip(blob, limit)}" if inp else name
    return f"{name} {_clip(str(inp), limit)}" if inp else name


def _result_text(result) -> str:
    """A tool_result's content, which Claude Code stores as a string or a list of blocks."""
    if isinstance(result, list):
        parts = []
        for b in result:
            if isinstance(b, dict):
                parts.append(b.get("text", "") if b.get("type") == "text" else f"[{b.get('type', 'block')}]")
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return result if isinstance(result, str) else ("" if result is None else str(result))


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"… (+{len(text) - limit} chars)"


def token_usage(path) -> dict:
    """Sum the model-reported token usage across a session JSONL.

    Claude Code splits one assistant message across several JSONL entries (one per content
    block) that all repeat the same `message.usage`, so a naive sum over-counts by the block
    count; dedupe on `message.id` (first seen wins). Input counts the real input plus both
    cache tiers (what the model actually read); output is the generated tokens. Returns
    {"in", "out", "total"} of ints, zeros if the log can't be read.
    """
    path = Path(path)
    out = {"in": 0, "out": 0, "total": 0}
    if not path.exists():
        return out
    seen: set[str] = set()
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        mid = msg.get("id")
        if isinstance(mid, str):
            if mid in seen:
                continue
            seen.add(mid)
        inp = sum(usage.get(k) or 0 for k in
                  ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        out["in"] += inp
        out["out"] += usage.get("output_tokens") or 0
    out["total"] = out["in"] + out["out"]
    return out


def recent_activity(path, count: int, input_chars: int = 300, result_chars: int = 800,
                    text_chars: int = 1500, skip_sidechain: bool = True, speaker: str = "Worker") -> str:
    """The last `count` responses and tool calls in a session JSONL, oldest first, as markdown.

    Walks the session JSONL backwards (tool_results are written after the tool_use they belong
    to, so a backward walk fills a tool_use_id→result map before reaching each call) and stops
    once `count` items are gathered. An item is one assistant text block (a response) or one
    tool call; thinking blocks and injected metadata are skipped. The agent's own prose (what
    it says it did) gets a larger budget than tool inputs and results, which are clipped so a
    big Write or Read can't flood the supervisor. Returns "" if the log holds no activity.

    By default sidechain (subagent) entries are skipped — the worker's own log and its subagents'
    are separate files, so this keeps a worker read to the worker. Pass skip_sidechain=False to
    read a subagent transcript, whose entries are all sidechain.
    """
    path = Path(path)
    if not path.exists():
        return ""
    results: dict[str, str] = {}
    items: list[str] = []
    for line in reversed(path.read_text().splitlines()):
        if len(items) >= count:
            break
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isMeta") or (skip_sidechain and entry.get("isSidechain")):
            continue
        kind = entry.get("type")
        content = entry.get("message", {}).get("content", [])
        if kind == "user":
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    flag = "error: " if b.get("is_error") else ""
                    results[b["tool_use_id"]] = flag + _clip(_result_text(b.get("content")), result_chars)
            continue
        if kind != "assistant" or not isinstance(content, list):
            continue
        # Process blocks in reverse so the final reversal restores their in-message order.
        for b in reversed(content):
            if len(items) >= count or not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text" and b.get("text", "").strip():
                items.append(f"**{speaker}:** " + _clip(b["text"], text_chars))
            elif btype in ("tool_use", "server_tool_use"):
                gist = _tool_summary(b.get("name", "tool"), b.get("input"), input_chars)
                res = results.get(b.get("id"), "(no result yet)")
                items.append(f"**{gist}**\n→ {res}" if res else f"**{gist}**")
    return "\n\n".join(reversed(items))


def subagent_logs(worker_log) -> list[tuple[Path, dict]]:
    """Transcripts of the subagents a worker session spawned, newest first, as (path, meta).

    Claude Code stores them beside the session log in <session>/subagents/agent-<id>.jsonl, each
    with an agent-<id>.meta.json sidecar (agentType, description, toolUseId, requestShape). The
    returned meta always carries `agent_id` (the id from the filename, which is what a permission
    request's `agent_id` matches)."""
    worker_log = Path(worker_log)
    d = worker_log.parent / worker_log.stem / "subagents"
    if not d.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    try:
        files = sorted(d.glob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for f in files:
        try:
            meta = json.loads(f.with_suffix(".meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["agent_id"] = f.stem[len("agent-"):]   # "agent-<id>" -> "<id>"
        out.append((f, meta))
    return out


def subagent_finished(path) -> bool:
    """Whether a subagent's transcript shows it done: its last entry is a final assistant reply
    (stop_reason end_turn). Anything else — a tool call awaiting its result, a result awaiting the
    next reply — means it is still running. An unreadable log counts as not finished."""
    try:
        lines = [l for l in Path(path).read_text().splitlines() if l.strip()]
    except OSError:
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") or {}
        return entry.get("type") == "assistant" and msg.get("stop_reason") == "end_turn"
    return False


_NOTIFIED = re.compile(r"<tool-use-id>([^<\s]+)</tool-use-id>")


def _entry_time(entry: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def background_running(worker_log, since: float = 0.0) -> list[str]:
    """Background work the worker started (at or after `since`, epoch seconds) that hasn't
    reported back yet, as short labels; empty when nothing is outstanding.

    Covers background tool calls (any tool_use with run_in_background, e.g. Bash) and background
    subagents and agent-team teammates (their transcripts sit in <session>/subagents). Claude Code
    reports each finished background task to the worker with a <task-notification> naming the
    launching <tool-use-id> (status completed/failed/killed/stopped — all terminal), so a launch
    with no such notification is still out. A subagent whose transcript ends in a final reply
    counts as done too (a teammate that has gone idle won't wake the worker on its own). Work
    launched before `since` (an earlier session of a resumed run) is ignored: a task cut off when
    that session ended never reports back, and would otherwise look outstanding forever.
    """
    worker_log = Path(worker_log)
    try:
        text = worker_log.read_text()
    except OSError:
        return []
    notified = set(_NOTIFIED.findall(text))
    out: list[str] = []
    for line in text.splitlines():
        if '"run_in_background"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        when = _entry_time(entry)
        if entry.get("type") != "assistant" or (when is not None and when < since):
            continue
        for b in (entry.get("message") or {}).get("content") or []:
            if (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in notified
                    and (b.get("input") or {}).get("run_in_background")):
                inp = b.get("input") or {}
                what = inp.get("description") or inp.get("command") or inp.get("prompt") or ""
                out.append(f"{b.get('name', 'tool')}: {_clip(str(what), 60)}" if what else b.get("name", "tool"))
                notified.add(b.get("id"))   # don't count it again as a subagent below
    for f, meta in subagent_logs(worker_log):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        if meta.get("requestShape") != "background" and not meta.get("teamName"):
            continue   # a foreground subagent runs inside the worker's turn
        if meta.get("toolUseId") in notified or subagent_finished(f):
            continue
        out.append(f"{meta.get('name') or meta.get('agentType') or 'subagent'} ({meta.get('agent_id')})")
    return out


def find_subagent_log(worker_log, agent: str) -> tuple[Path, dict] | None:
    """A worker's subagent transcript matched by the `agent` the supervisor named: its exact id,
    then an id substring, then its agentType (case-insensitive). None if nothing matches."""
    agent = (agent or "").strip()
    if not agent:
        return None
    logs = subagent_logs(worker_log)
    low = agent.lower()
    for f, meta in logs:
        if meta.get("agent_id") == agent:
            return f, meta
    for f, meta in logs:
        if low in str(meta.get("agent_id", "")).lower():
            return f, meta
    for f, meta in logs:
        if low == str(meta.get("agentType", "")).lower():
            return f, meta
    return None


class Transcript:
    def __init__(self, run_dir: str):
        self.path = Path(run_dir) / "transcript.md"
        if not self.path.exists():
            self.path.write_text("# Adversary run transcript\n")

    def add(self, heading: str, body: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.path.open("a") as f:
            f.write(f"\n## [{stamp}] {heading}\n")
            if body:
                f.write(f"\n{body}\n")


def write_report(run_dir: str, repo: str, baseline: str, outcome: str, summary: str, stats: dict,
                 changes: str) -> Path:
    lines = [
        "# Adversary run report",
        "",
        f"- **Outcome:** {outcome}",
        f"- **Repo:** `{repo}`",
        f"- **Baseline:** {baseline}",
        *[f"- **{k}:** {v}" for k, v in stats.items()],
        "",
        "## Adversary summary",
        "",
        summary or "(none)",
        "",
        "## Changes since the run started",
        "",
        "```",
        changes,
        "```",
        "",
    ]
    path = Path(run_dir) / "report.md"
    path.write_text("\n".join(lines))
    return path
