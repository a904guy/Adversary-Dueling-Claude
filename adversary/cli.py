"""`adversary run "<task>"` and `adversary resume`: launch worker, adversary and bridge in tmux."""

import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from adversary import sessions, snapshot, tmux
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
        stall_after=a.stall_after, origin=str(Path(a.repo).expanduser().resolve()),
    )
    cfg.save()
    launch(cfg, no_attach=a.no_attach)


def launch(cfg: RunConfig, no_attach: bool, resume_file: Path | None = None) -> None:
    scripts = write_launch_files(cfg)
    repo, run_dir = cfg.repo, Path(cfg.run_dir)

    # Worker (left) and adversary (right) wait for the bridge socket; bridge goes full-width at the bottom.
    inside = bool(os.environ.get("TMUX"))
    if inside:
        worker = tmux.tmux("new-window", "-P", "-F", "#{pane_id}", "-n", "adversary", "-c", repo,
                           str(scripts["worker"]))
    else:
        worker = tmux.tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", cfg.session, "-n", "adversary",
                           "-x", "220", "-y", "60", "-c", repo, str(scripts["worker"]))
    adversary = tmux.tmux("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", worker, "-c", repo,
                          str(scripts["adversary"]))
    pkg_root = str(Path(__file__).resolve().parent.parent)
    bridge_cmd = (f"cd {shlex.quote(pkg_root)} && {shlex.quote(sys.executable)} -m adversary.bridge "
                  f"--run-dir {shlex.quote(str(run_dir))} --worker-pane {worker} --adversary-pane {adversary}"
                  + (f" --resume-file {shlex.quote(str(resume_file))}" if resume_file else "") + "; "
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
    elif not no_attach:
        os.execvp("tmux", ["tmux", "attach-session", "-t", cfg.session])
    else:
        print(f"adversary: started detached; attach with: tmux attach -t {cfg.session}")


def resume(a: argparse.Namespace) -> None:
    for exe in ("tmux", "claude"):
        if not shutil.which(exe):
            die(f"`{exe}` not found on PATH")
    if a.run:
        run_dir = Path(a.run).expanduser()
        run_dir = run_dir if run_dir.is_dir() else STATE_DIR / a.run
        try:
            cfg = RunConfig.load(str(run_dir))
        except (OSError, ValueError, TypeError):
            die(f"no run found at {a.run}")
    else:
        folder = Path(a.repo).expanduser().resolve()
        cfg = sessions.latest_run_for(folder, STATE_DIR)
        if not cfg:
            die(f"no previous run found for {folder}; start one with `adversary run \"task\"`")
    if not Path(cfg.repo).is_dir():
        die(f"the run's folder {cfg.repo} no longer exists")

    found = sessions.find_sessions(cfg)
    missing = [role for role in ("worker", "adversary") if role not in found]
    if missing:
        die(f"could not find the {' and '.join(missing)} Claude Code session for run {cfg.run_dir}")

    # The same sessions must not run twice: close the old tmux session first.
    if subprocess.run(["tmux", "has-session", "-t", f"={cfg.session}"], capture_output=True).returncode == 0:
        if not a.kill:
            if not sys.stdin.isatty():
                die(f"run is still open in tmux session {cfg.session}; pass --kill to close it and resume")
            answer = input(f"adversary: run is still open in tmux session {cfg.session}. Close it and resume? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                die("not resumed")
        subprocess.run(["tmux", "kill-session", "-t", f"={cfg.session}"], check=False)
    elif bridge_alive(cfg.sock):
        die(f"run is still open in another tmux window (its bridge answers on {cfg.sock}); close that window first")

    cfg.sessions = found
    if os.path.exists(cfg.sock):
        os.remove(cfg.sock)  # left behind by a bridge that didn't exit cleanly
    cfg.save()

    was_finished = (Path(cfg.run_dir) / "report.md").exists()
    worker_said = sessions.last_worker_message(cfg.run_dir)
    note = [
        "This supervised run was interrupted and has just been resumed. Your session and the worker's "
        "were both restored with their history, but the worker is idle and has not been told anything yet. "
        "Messages can be lost when a run stops, so don't assume your last reply reached the worker.",
        "The run had previously been ended with `finish`; it is open again." if was_finished else "",
        f"The worker's last message (from the run transcript):\n\n<worker_message>\n{worker_said}\n</worker_message>"
        if worker_said else "",
        "NOTE: the human added these instructions. They come from the person you stand in for, so they "
        f"amend the original task and take precedence over it where the two conflict:\n\n> {a.message}"
        if a.message else "",
        "Check where things stand (changed_files, the tests), then reply with what the worker should do "
        "next. Your reply is forwarded to it verbatim. If every requirement is verifiably done, call finish instead.",
    ]
    resume_file = Path(cfg.run_dir) / "resume.md"
    resume_file.write_text("\n\n".join(p for p in note if p))
    print(f"adversary: resuming run {cfg.run_dir}")
    print(f"adversary: task: {short(cfg.task)}")
    launch(cfg, no_attach=a.no_attach, resume_file=resume_file)


def bridge_alive(sock: str) -> bool:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(sock)
        return True
    except OSError:
        return False
    finally:
        s.close()


def short(text: str, n: int = 100) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


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
    r.add_argument("--stall-after", type=int, default=120, metavar="SECONDS",
                   help="log a stall when both agents sit idle this long with the run unfinished (default 120, 0 = off)")
    r.add_argument("--worker-model", help="model for the worker")
    r.add_argument("--adversary-model", help="model for the adversary")
    r.add_argument("--verify-cmd", action="append", help="extra command prefix the adversary may run (repeatable)")
    r.add_argument("--no-attach", action="store_true", help="don't attach to the tmux session")
    s = sub.add_parser("resume", help="resume the last run in this folder (or a given run)")
    s.add_argument("message", nargs="?", help="optional new instructions, passed to the adversary")
    s.add_argument("--repo", default=".", help="folder whose last run to resume (default: current directory)")
    s.add_argument("--run", help="a specific run: its directory or timestamp (e.g. 20261001-143802)")
    s.add_argument("--kill", action="store_true", help="close the run's tmux session if it is still open")
    s.add_argument("--no-attach", action="store_true", help="don't attach to the tmux session")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)
    elif a.cmd == "resume":
        resume(a)


if __name__ == "__main__":
    main()
