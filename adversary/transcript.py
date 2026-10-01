"""Run log (transcript.md), final report (report.md), and last-message extraction."""

import json
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
