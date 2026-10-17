import asyncio
import os
import subprocess

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

    b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=deliver, log=lambda m: None, focus=lambda p: None, pane_mode=lambda p: None)
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


def test_line_stats_count_text_lines_and_skip_binaries(tmp_path):
    root, snap = tmp_path / "proj", tmp_path / "snap"
    root.mkdir()
    (root / "mod.py").write_text("a\nb\nc\n")
    (root / "gone.txt").write_text("x\ny\n")
    (root / "pic.png").write_bytes(b"\x89PNG\0\0data")
    snapshot.take(str(root), str(snap))
    (root / "mod.py").write_text("a\nB\nc\nd\n")          # 1 changed, 1 added: +2 −1
    (root / "gone.txt").unlink()                         # −2
    (root / "new.md").write_text("1\n2\n3\n")            # +3
    (root / "pic.png").write_bytes(b"\x89PNG\0\0other")  # binary: counts as a file, no lines
    c = snapshot.changes(str(root), str(snap))
    assert snapshot.line_stats(str(root), str(snap), c) == (5, 3)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_diff_vs_start_splits_inner_repos_from_the_rest(tmp_path):
    root = tmp_path / "proj"
    app = root / "app"
    app.mkdir(parents=True)
    _git(app, "init", "-q")
    (app / ".gitignore").write_text("out/\n")
    (app / "main.py").write_text("one\ntwo\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "init", "--no-gpg-sign")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=app, capture_output=True, text=True).stdout.strip()
    run = tmp_path / "run"
    run.mkdir()
    snapshot.take(str(root), str(run / "snapshot"))
    cfg = RunConfig(task="t", repo=str(root), run_dir=str(run), sock=str(tmp_path / "s"), base_sha=None,
                    snapshot_dir=str(run / "snapshot"), git_subdirs=["app"], git_subdir_bases={"app": base})
    (app / "main.py").write_text("one\nTWO\nthree\n")   # +2 −1
    (app / "new.py").write_text("n\n")                   # untracked: +1
    (app / "out").mkdir()
    (app / "out" / "build.txt").write_text("ignored\n")  # gitignored: not counted
    (root / ".work").mkdir()
    for i in range(3):
        (root / ".work" / f"shot{i}.txt").write_text("s\n")
    b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=None, log=lambda m: None,
               focus=lambda p: None, pane_mode=lambda p: None)
    assert b._diff_text() == "5 files, +6 −1"
    lines = b._diff_breakdown()
    assert lines[0].split() == ["app", "2", "files,", "+3", "−1"]
    assert lines[1].startswith("rest of folder") and "3 files, +3 −0" in lines[1] and "(.work/ 3)" in lines[1]


def test_resume_backfills_inner_repo_starts_from_the_run_time(tmp_path):
    from adversary.cli import backfill_subdir_bases
    app = tmp_path / "proj" / "app"
    app.mkdir(parents=True)
    _git(app, "init", "-q")
    for msg, when in (("before", "2026-01-01T10:00:00"), ("during", "2026-01-01T12:00:00")):
        (app / "f.txt").write_text(msg)
        _git(app, "add", "-A")
        subprocess.run(["git", "commit", "-q", "--no-gpg-sign", "-m", msg], cwd=app, check=True,
                       capture_output=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                                 "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    before = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=app, capture_output=True, text=True).stdout.strip()
    cfg = RunConfig(task="t", repo=str(tmp_path / "proj"), run_dir=str(tmp_path / "20260101-110000"),
                    sock="/s", base_sha=None, git_subdirs=["app"])
    backfill_subdir_bases(cfg)
    assert cfg.git_subdir_bases == {"app": before}
