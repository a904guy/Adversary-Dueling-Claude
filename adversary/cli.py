"""`adversary run "<task>" --repo PATH`: launch worker, adversary and bridge in tmux."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from adversary import snapshot, tmux
from adversary.config import RunConfig, write_launch_files

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "adversary" / "runs"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def die(msg: str) -> None:
    print(f"adversary: {msg}", file=sys.stderr)
    sys.exit(1)


def run(a: argparse.Namespace) -> None:
    for exe in ("tmux", "claude"):
        if not shutil.which(exe):
            die(f"`{exe}` not found on PATH")
    task = Path(a.task_file).read_text() if a.task_file else a.task
    if not task or not task.strip():
        die("no task given")

    repo = Path(a.repo).expanduser().resolve()
    if not repo.is_dir():
        die(f"{repo} is not a directory")
    try:
        base_sha = git(repo, "rev-parse", "HEAD") if shutil.which("git") else None
    except subprocess.CalledProcessError:
        base_sha = None  # not a git repo (or no commits yet): fall back to a folder snapshot
    if base_sha and git(repo, "status", "--porcelain"):
        print("adversary: warning: working tree has uncommitted changes; the review diff includes them.")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.worktree:
        if not base_sha:
            die("--worktree needs a git repository with at least one commit")
        wt = repo.parent / f"{repo.name}-adversary-{ts}"
        try:
            git(repo, "worktree", "add", "-b", f"adversary/{ts}", str(wt), base_sha)
        except subprocess.CalledProcessError as e:
            die(f"git worktree add failed: {e.stderr.strip()}")
        print(f"adversary: working in worktree {wt} (branch adversary/{ts})")
        repo = wt

    run_dir = STATE_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = None
    if not base_sha:
        snapshot_dir = str(run_dir / "snapshot")
        print(f"adversary: {repo} is not a git repo; snapshotting it to {snapshot_dir} ...")
        stats = snapshot.take(str(repo), snapshot_dir)
        print(f"adversary: snapshot: {stats['files']} files, {stats['copied']} copied"
              + (f", {stats['not_copied']} too large to copy (hashed only)" if stats["not_copied"] else "")
              + f" (skipped dirs: {', '.join(sorted(snapshot.SKIP_DIRS))})")
    sock_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    cfg = RunConfig(
        task=task, repo=str(repo), run_dir=str(run_dir), sock=str(sock_dir / f"adversary-{ts}.sock"),
        base_sha=base_sha, snapshot_dir=snapshot_dir, skip_permissions=a.skip_permissions, worker_permission_mode=a.worker_permission_mode,
        max_exchanges=a.max_exchanges, worker_model=a.worker_model, adversary_model=a.adversary_model,
        verify_cmds=a.verify_cmd or [], session=f"adversary-{ts}", strict_approvals=a.strict_approvals,
    )
    cfg.save()
    scripts = write_launch_files(cfg)

    # Worker (left) and adversary (right) wait for the bridge socket; bridge goes full-width at the bottom.
    inside = bool(os.environ.get("TMUX"))
    if inside:
        worker = tmux.tmux("new-window", "-P", "-F", "#{pane_id}", "-n", "adversary", "-c", str(repo),
                           str(scripts["worker"]))
    else:
        worker = tmux.tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", cfg.session, "-n", "adversary",
                           "-x", "220", "-y", "60", "-c", str(repo), str(scripts["worker"]))
    adversary = tmux.tmux("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", worker, "-c", str(repo),
                          str(scripts["adversary"]))
    pkg_root = str(Path(__file__).resolve().parent.parent)
    bridge_cmd = (f"cd {shlex.quote(pkg_root)} && {shlex.quote(sys.executable)} -m adversary.bridge "
                  f"--run-dir {shlex.quote(str(run_dir))} --worker-pane {worker} --adversary-pane {adversary}; "
                  "echo; echo 'bridge exited, press Enter to close'; read _")
    bridge = tmux.tmux("split-window", "-v", "-f", "-l", "12", "-P", "-F", "#{pane_id}", "-t", worker,
                       "sh", "-c", bridge_cmd)
    # Label panes with a user option: Claude Code rewrites the pane title (OSC escapes), so
    # #{pane_title} would show its process title instead of the role.
    for pane, label in ((worker, "WORKER · does the task"),
                        (adversary, "ADVERSARY · reviews & approves (read-only)"),
                        (bridge, "BRIDGE · p pause/resume · s status · q quit")):
        tmux.tmux("set-option", "-p", "-t", pane, "@adversary_label", label)
    tmux.tmux("set-option", "-w", "-t", worker, "pane-border-status", "top")
    tmux.tmux("set-option", "-w", "-t", worker, "pane-border-format",
              "#{?pane_active,#[reverse],} #{@adversary_label} #[default]")
    tmux.tmux("select-pane", "-t", worker)

    print(f"adversary: run dir {run_dir}")
    if inside:
        print("adversary: opened in a new tmux window")
    elif not a.no_attach:
        os.execvp("tmux", ["tmux", "attach-session", "-t", cfg.session])
    else:
        print(f"adversary: started detached; attach with: tmux attach -t {cfg.session}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="adversary", description="Supervise a Claude Code worker with an adversarial Claude Code reviewer.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start a supervised run")
    r.add_argument("task", nargs="?", help="the task for the worker (or use --task-file)")
    r.add_argument("--task-file", help="read the task from a file")
    r.add_argument("--repo", default=".", help="folder to work in (default: current directory)")
    perms = r.add_mutually_exclusive_group()
    perms.add_argument("--skip-permissions", action="store_true",
                       help="run the worker with --dangerously-skip-permissions (no approval traffic)")
    perms.add_argument("--worker-permission-mode", choices=["manual", "acceptEdits", "auto", "plan"],
                       help="worker permission mode; prompts that remain go to the adversary (default: manual)")
    r.add_argument("--strict-approvals", action="store_true",
                   help="ignore user-level settings for the worker so no user allow rule bypasses the adversary")
    r.add_argument("--worktree", action="store_true", help="run in a fresh git worktree on branch adversary/<ts>")
    r.add_argument("--max-exchanges", type=int, default=30, help="cap on worker↔adversary round trips (default 30)")
    r.add_argument("--worker-model", help="model for the worker")
    r.add_argument("--adversary-model", help="model for the adversary")
    r.add_argument("--verify-cmd", action="append", help="extra command prefix the adversary may run (repeatable)")
    r.add_argument("--no-attach", action="store_true", help="don't attach to the tmux session")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)


if __name__ == "__main__":
    main()
