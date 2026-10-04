import asyncio

from adversary import snapshot
from adversary.bridge import ADVERSARY, WORKER, Bridge
from adversary.config import RunConfig, write_launch_files


def test_snapshot_detects_changes_and_skips_dep_dirs(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "src" / "a.py").write_text("a = 1\n")
    (root / "gone.txt").write_text("bye\n")
    (root / "node_modules" / "x" / "big.js").write_text("ignored\n")
    snap = tmp_path / "snap"
    stats = snapshot.take(str(root), str(snap))
    assert stats["files"] == 2 and (snap / "files" / "src" / "a.py").read_text() == "a = 1\n"
    assert not (snap / "files" / "node_modules").exists()

    (root / "src" / "a.py").write_text("a = 2\n")
    (root / "gone.txt").unlink()
    (root / "new.md").write_text("hi\n")
    c = snapshot.changes(str(root), str(snap))
    assert c == {"added": ["new.md"], "modified": ["src/a.py"], "deleted": ["gone.txt"]}
    assert snapshot.format_changes(c) == "A new.md\nM src/a.py\nD gone.txt"


def test_snapshot_skips_copy_of_large_files(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_FILE", 10)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "big.bin").write_bytes(b"x" * 100)
    stats = snapshot.take(str(root), str(tmp_path / "snap"))
    assert stats == {"files": 1, "copied": 0, "not_copied": 1, "bytes_copied": 0}
    (root / "big.bin").write_bytes(b"y" * 100)
    assert snapshot.changes(str(root), str(tmp_path / "snap"))["modified"] == ["big.bin"]


async def test_bridge_without_git_uses_snapshot(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("1\n")
    run = tmp_path / "run"
    run.mkdir()
    snapshot.take(str(root), str(run / "snapshot"))
    cfg = RunConfig(task="t", repo=str(root), run_dir=str(run), sock=str(tmp_path / "s"), base_sha=None,
                    snapshot_dir=str(run / "snapshot"))
    sent = []

    async def deliver(role, header, body):
        sent.append((role, body))

    b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=deliver, log=lambda m: None, focus=lambda p: None)
    await b.on_hook(ADVERSARY, "SessionStart", {})
    for _ in range(5):
        await asyncio.sleep(0)
    brief = [t for r, t in sent if r == ADVERSARY][0]
    assert "NOT a git repository" in brief and str(run / "snapshot") in brief

    (root / "a.txt").write_text("2\n")
    assert b.on_mcp("changed_files", {})["text"] == "M a.txt"
    b.on_mcp("finish", {"outcome": "complete", "summary": "ok"})
    report = (run / "report.md").read_text()
    assert "no git" in report and "M a.txt" in report

    scripts = write_launch_files(cfg)
    adv = scripts["adversary"].read_text()
    assert "--add-dir" in adv and "Bash(diff:*)" in adv and "mcp__adversary__changed_files" in adv
