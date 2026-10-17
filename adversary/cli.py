"""`adversary run "<task>"` and `adversary resume`: launch worker, adversary and bridge in tmux."""

import argparse
import json
import os
import random
import shlex
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from adversary import mcps, sessions, snapshot, tmux
from adversary.config import RunConfig, git_safe_env, write_launch_files

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "adversary" / "runs"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def find_git_subdirs(root: Path, max_depth: int = 2) -> list[str]:
    """Git repositories inside `root`, as paths relative to it (for `git -C <subdir>`).

    Scans a couple of levels down, skipping dependency/build dirs, and does not descend into a
    repo once found. Lets the adversary inspect the history of repos that live inside a working
    folder which isn't itself one git repo (e.g. an app repo beside its framework repo)."""
    found: list[str] = []

    def scan(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_symlink() or not child.is_dir() or child.name in snapshot.SKIP_DIRS:
                continue
            if (child / ".git").exists():
                found.append(str(child.relative_to(root)))
            else:
                scan(child, depth + 1)

    scan(root, 1)
    return found


def backfill_subdir_bases(cfg: RunConfig) -> None:
    """Give inner repos with no recorded start (a run from before they were recorded, or a repo
    found since) the last commit before the run began, read from the run folder's name."""
    try:
        started = sessions.run_started(cfg.run_dir)
    except ValueError:
        return
    os.environ.update(git_safe_env(cfg.safe_dirs()[1:]))
    for d in cfg.git_subdirs:
        if d in cfg.git_subdir_bases:
            continue
        try:
            sha = git(Path(cfg.repo) / d, "rev-list", "-1", f"--before={started.isoformat()}", "HEAD")
        except subprocess.CalledProcessError:
            continue
        if sha:   # empty: the repo's history began during the run, so the snapshot covers it
            cfg.git_subdir_bases[d] = sha


def new_run_id() -> str:
    """The start time plus a random -####: a pool can start several runs in the same second, and
    the id names the run dir, its socket and its tmux session."""
    while True:
        ts = f"{datetime.now():%Y%m%d-%H%M%S}-{random.randrange(10000):04d}"
        if not (STATE_DIR / ts).exists():
            return ts


def card_for_run(card_id: str, board_env: str | None) -> tuple[dict, str]:
    """The card a run works ({"id", "url"}) and a task made from it, checking the board is reachable."""
    from adversary import board as fourga
    from adversary import cardmode
    try:
        client = fourga.Board.from_env(board_env)
        card = client.card(card_id)
        comments = client.comments(card_id)
        url = fourga.card_url(client.url, card_id)
        return {"id": str(card["id"]), "url": url}, cardmode.task_text(card, comments, str(client.me()["id"]), url,
                                                                       client.checklist(card_id))
    except fourga.BoardError as e:
        die(f"--card {card_id}: {e}")
        raise   # (die exits)


def manage(a: argparse.Namespace) -> None:
    from adversary import board as fourga
    from adversary.manager import Manager
    for exe in ("tmux", "claude", "git"):
        if not shutil.which(exe):
            die(f"`{exe}` not found on PATH")
    folder = Path(a.folder).expanduser().resolve()
    if not folder.is_dir():
        die(f"{folder} is not a directory")
    if a.pool < 1:
        die("--pool must be at least 1")
    run_args = a.run_args[1:] if a.run_args[:1] == ["--"] else a.run_args
    try:
        env = fourga.load_env(a.board_env)
        client = fourga.Board(env["FOURGA_URL"], env["FOURGA_CLIENT_ID"], env["FOURGA_CLIENT_SECRET"])
        client.me()
    except fourga.BoardError as e:
        die(str(e))
    members = list(dict.fromkeys((a.member or []) + [m for m in env.get("FOURGA_MEMBERS", "").split(",") if m.strip()]))
    if not members:
        die("name at least one person to keep on the project: --member you@example.com (or FOURGA_MEMBERS)")
    os.environ.update(git_safe_env([str(folder)] + [str(folder / d) for d in find_git_subdirs(folder)]))
    Manager(a.project, str(folder), a.pool, client, STATE_DIR, run_args=run_args, board_env=a.board_env,
            workspaces=Path(a.workspaces).expanduser() if a.workspaces else None, members=members,
            done_window_days=a.done_window_days
            ).serve(a.interval, a.once)


def die(msg: str) -> None:
    print(f"adversary: {msg}", file=sys.stderr)
    sys.exit(1)


def run(a: argparse.Namespace) -> None:
    for exe in ("tmux", "claude"):
        if not shutil.which(exe):
            die(f"`{exe}` not found on PATH")
    task = Path(a.task_file).read_text() if a.task_file else a.task
    card = None
    if a.card and not a.managed:
        die("--card is for runs `adversary manage` launches")
    if a.card:
        card, card_task = card_for_run(a.card, a.board_env)
        task = task or card_task
    if not task or not task.strip():
        die("no task given")

    repo = Path(a.repo).expanduser().resolve()
    if not repo.is_dir():
        die(f"{repo} is not a directory")
    # A repo another user owns is "dubious ownership" to git (it won't even rev-parse), which would
    # wrongly send the run to snapshot mode; trust it for this process, as the agents' scripts do.
    os.environ.update(git_safe_env([str(repo)]))
    try:
        base_sha = git(repo, "rev-parse", "HEAD") if shutil.which("git") else None
    except subprocess.CalledProcessError:
        base_sha = None  # not a git repo (or no commits yet): fall back to a folder snapshot
    if base_sha and git(repo, "status", "--porcelain"):
        print("adversary: warning: working tree has uncommitted changes; the review diff includes them.")

    ts = new_run_id()
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
        worker_effort=a.worker_effort, adversary_effort=a.adversary_effort,
        verify_cmds=a.verify_cmd or [], session=f"adversary-{ts}", strict_approvals=a.strict_approvals,
        stall_after=a.stall_after, origin=str(Path(a.repo).expanduser().resolve()),
        git_subdirs=find_git_subdirs(repo), adversary_mcps=a.adversary_mcp or [],
        card=card, board_env=a.board_env, managed=a.managed,
        workspace=json.loads(Path(a.workspace_info).read_text()) if a.workspace_info else None,
    )
    if cfg.adversary_mcps:
        print(f"adversary: MCP servers for the supervisor: {mcp_summary(cfg)}")
    # Where each inner repo starts, so the closing diff counts its changes with git (respecting
    # its .gitignore) rather than from the folder snapshot.
    os.environ.update(git_safe_env(cfg.safe_dirs()[1:]))
    for d in cfg.git_subdirs:
        try:
            cfg.git_subdir_bases[d] = git(repo / d, "rev-parse", "HEAD")
        except subprocess.CalledProcessError:
            pass   # no commits yet: the snapshot covers it
    if cfg.git_subdirs:
        print(f"adversary: git repos inside the folder (the supervisor can inspect these with "
              f"`git -C <subdir> log/status/diff`): {', '.join(cfg.git_subdirs)}")
    cfg.save()
    launch(cfg, no_attach=a.no_attach)


BRIDGE_ROWS = 4  # the bridge pane only shows status and log lines
EFFORTS = ["low", "medium", "high", "xhigh", "max"]   # what `claude --effort` accepts
PERMISSION_MODES = ["manual", "acceptEdits", "auto", "plan"]   # worker modes that still ask the adversary


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
    attach_mode = not inside and not no_attach
    base_cmd = (f"cd {shlex.quote(pkg_root)} && {shlex.quote(sys.executable)} -m adversary.bridge "
                f"--run-dir {shlex.quote(str(run_dir))} --worker-pane {worker} --adversary-pane {adversary}"
                + (f" --resume-file {shlex.quote(str(resume_file))}" if resume_file else ""))
    if cfg.managed:
        # Launched by the manager: nobody reads the pane, and the tmux session ending is how the
        # manager knows the run closed, so don't hold it open.
        tail = ""
    elif attach_mode:
        # The launcher reprints the stats in this terminal after tmux exits, so a clean close lets
        # the pane (and window, and session) close straight away; only hold it open to show a crash.
        tail = '; s=$?; [ "$s" -eq 0 ] || { echo; echo "bridge exited with status $s — press Enter to close"; read _; }'
    else:
        # No launcher is waiting (opened in a window, or detached): keep the pane up so its stats
        # readout can be read before it closes.
        tail = "; echo; echo 'bridge exited — press Enter to close'; read _"
    bridge_cmd = base_cmd + tail
    bridge = tmux.tmux("split-window", "-v", "-f", "-l", str(BRIDGE_ROWS), "-P", "-F", "#{pane_id}", "-t", worker,
                       "sh", "-c", bridge_cmd)
    # Attaching from a terminal of another size rescales every pane; keep the bridge short.
    # Window-scoped hooks and the window-resized hook need tmux >= 3.2; on an older tmux this
    # is unavailable, so treat it as a best-effort nicety rather than aborting the whole run.
    try:
        tmux.tmux("set-hook", "-w", "-t", worker, "window-resized", f"resize-pane -t {bridge} -y {BRIDGE_ROWS}")
    except subprocess.CalledProcessError as e:
        print(f"adversary: note: couldn't pin the bridge pane height ({(e.stderr or '').strip() or 'tmux refused the hook'}); "
              "the bridge pane may grow if you resize the terminal")
    # Label panes with a user option: Claude Code rewrites the pane title (OSC escapes), so
    # #{pane_title} would show its process title instead of the role.
    for pane, label in ((worker, "WORKER · does the task"),
                        (adversary, "ADVERSARY · reviews & approves (read-only)"),
                        (bridge, "BRIDGE · p pause/resume · s status · q quit")):
        tmux.tmux("set-option", "-p", "-t", pane, "@adversary_label", label)
    tmux.tmux("set-option", "-w", "-t", worker, "pane-border-status", "top")
    tmux.tmux("set-option", "-w", "-t", worker, "pane-border-format",
              "#{?pane_active,#[reverse],} #{@adversary_label} #[default]")
    tmux.tmux("select-pane", "-t", adversary)  # the bridge focuses it again once its session has loaded

    print(f"adversary: run dir {run_dir}")
    if inside:
        print("adversary: opened in a new tmux window")
    elif not no_attach:
        # Attach as a child (not execvp) so that, once tmux exits, control returns here and the
        # run's final stats can be shown in this terminal — they survive tmux closing.
        subprocess.run(["tmux", "attach-session", "-t", cfg.session])
        _print_stats_after_close(cfg)
    else:
        print(f"adversary: started detached; attach with: tmux attach -t {cfg.session}")


def _print_stats_after_close(cfg: RunConfig) -> None:
    """After `tmux attach` returns, print the run's final stats in this terminal so they persist
    after tmux is gone — unless the user merely detached and the run is still open."""
    alive = subprocess.run(["tmux", "has-session", "-t", f"={cfg.session}"], capture_output=True).returncode == 0
    if alive:
        print(f"adversary: detached; the run is still open (reattach with: tmux attach -t {cfg.session})")
        return
    try:
        text = (Path(cfg.run_dir) / "stats.txt").read_text()
    except OSError:
        return
    print("\n" + text.rstrip("\n"))


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
    # Recompute which subdirectories are git repos: a run started before this existed has none
    # recorded, and repos may have been added since. write_launch_files then regenerates the
    # adversary's allowlist with the `git -C <subdir>` rules.
    cfg.git_subdirs = find_git_subdirs(Path(cfg.repo))
    backfill_subdir_bases(cfg)
    # Settings given on the command line replace the saved ones; the rest keep what the run used.
    changed = apply_settings(cfg, a)
    if changed:
        print(f"adversary: changed for this and later sessions: {', '.join(changed)}")
    if cfg.adversary_mcps:
        print(f"adversary: MCP servers for the supervisor: {mcp_summary(cfg)}")
    if os.path.exists(cfg.sock):
        os.remove(cfg.sock)  # left behind by a bridge that didn't exit cleanly
    cfg.save()

    was_finished = (Path(cfg.run_dir) / "report.md").exists()
    note = resume_note(was_finished, sessions.last_worker_message(cfg.run_dir), a.message)
    resume_file = Path(cfg.run_dir) / "resume.md"
    resume_file.write_text(note)
    print(f"adversary: resuming run {cfg.run_dir}")
    print(f"adversary: task: {short(cfg.task)}")
    launch(cfg, no_attach=a.no_attach, resume_file=resume_file)


# What Claude Code records as the final message of a turn that produced no reply.
EMPTY_REPLIES = {"", "no response requested."}


def resume_note(was_finished: bool, worker_said: str, message: str | None) -> str:
    """The adversary's first message on resume. It differs with and without new instructions
    from the human, and when the worker's last turn left no message."""
    worker_said = (worker_said or "").strip()
    visible = "".join(c for c in worker_said if c.isprintable() and not c.isspace() and c not in "\u200b\u200c\u200d\u2060\ufeff")
    silent = not visible or worker_said.lower() in EMPTY_REPLIES
    note = [
        "This supervised run was interrupted and has just been resumed. Your session and the worker's "
        "were both restored with their history, but the worker is idle and has not been told anything yet. "
        "Messages can be lost when a run stops, so don't assume your last reply reached the worker.",
        "The run had previously been ended with `finish`; it is open again." if was_finished else "",
        "The worker's last turn ended without a message, so there is nothing from it to answer. Work out "
        "where it got to from the repository and your own history." if silent else
        f"The worker's last message (from the run transcript):\n\n<worker_message>\n{worker_said}\n</worker_message>",
    ]
    if message:
        note += [
            "NOTE: the human added these instructions. They come from the person you stand in for, so they "
            f"amend the original task and take precedence over it where the two conflict:\n\n> {message}",
            "Check where things stand (changed_files, the tests), then reply with what the worker should do "
            "next, starting with the human's new instructions. Your reply is forwarded to it verbatim.",
        ]
    else:
        note += [
            "The human gave no new instructions: carry on with the task as it stood when the run stopped, "
            "including any changes the human asked for earlier in your history.",
            "Check where things stand (changed_files, the tests). If work remains, reply with what the worker "
            "should do next; your reply is forwarded to it verbatim. If every requirement is verifiably done, "
            "call finish. If the worker has nothing to do until the human decides something, call hold.",
        ]
    return "\n\n".join(p for p in note if p)


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


def add_settings(p: argparse.ArgumentParser, resume: bool) -> None:
    """The run settings, shared by `run` and `resume` so the two take the same flags. On `run` each
    has its default; on `resume` each defaults to None, meaning "keep what the run used", and a
    given value replaces the saved one for this and later sessions (see apply_settings)."""
    keep = " (default: what the run used)" if resume else ""

    def d(value):
        return None if resume else value

    perms = p.add_mutually_exclusive_group()
    perms.add_argument("--skip-permissions", action="store_true", default=None if resume else False,
                       help="run the worker with --dangerously-skip-permissions (no approval traffic)")
    perms.add_argument("--worker-permission-mode", choices=PERMISSION_MODES,
                       help="worker permission mode; prompts that remain go to the adversary"
                            + (keep or " (default: manual)"))
    p.add_argument("--strict-approvals", action=argparse.BooleanOptionalAction, default=d(False),
                   help="ignore user-level settings for the worker so no user allow rule bypasses the adversary" + keep)
    p.add_argument("--max-exchanges", type=int, default=d(30),
                   help="round trips between the agents without you before the worker is put on hold"
                        + (keep or " (default 30)") + "; the count restarts whenever you type to either side, and on resume")
    p.add_argument("--stall-after", type=int, default=d(120), metavar="SECONDS",
                   help="log a stall when both agents sit idle this long with the run unfinished"
                        + (keep or " (default 120)") + "; 0 = off")
    p.add_argument("--worker-model", help="model for the worker" + keep)
    p.add_argument("--adversary-model", help="model for the adversary" + keep)
    p.add_argument("--worker-effort", choices=EFFORTS, default=d("high"),
                   help="effort level for the worker (claude --effort)" + (keep or "; default: high"))
    p.add_argument("--adversary-effort", choices=EFFORTS, default=d("xhigh"),
                   help="effort level for the adversary (claude --effort)" + (keep or "; default: xhigh"))
    p.add_argument("--verify-cmd", action="append",
                   help="extra command prefix the adversary may run (repeatable)"
                        + ("; added to the run's existing ones" if resume else ""))
    p.add_argument("--adversary-mcp", action="append", metavar="SPEC",
                   help="give the adversary an MCP server by its name in your MCP config (repeatable; default: "
                        "none): NAME for all its tools, NAME:readonly for a look-only browser preset, "
                        "NAME:tool1,tool2 for just those; claude-in-chrome for Claude in Chrome"
                        + ("; replaces the run's choice, none clears it" if resume else ""))
    p.add_argument("--no-attach", action="store_true", help="don't attach to the tmux session")


def mcp_summary(cfg: RunConfig) -> str:
    """Check the run's --adversary-mcp specs against what's enabled (exit on a bad one) and
    describe what the adversary gets, for the launch output."""
    try:
        plan = mcps.plan(cfg.adversary_mcps, cfg.repo, strict=True)
    except ValueError as e:
        die(f"--adversary-mcp: {e}")
    return ", ".join(plan.labels) if plan.labels else "none"


def apply_settings(cfg: RunConfig, a: argparse.Namespace) -> list[str]:
    """Overlay the settings given to `resume` on the run's saved config; return what changed."""
    changed = []
    for name in ("strict_approvals", "max_exchanges", "stall_after", "worker_model", "adversary_model",
                 "worker_effort", "adversary_effort"):
        value = getattr(a, name, None)
        if value is not None and value != getattr(cfg, name):
            setattr(cfg, name, value)
            changed.append(f"{name.replace('_', '-')}={value}")
    # The worker's permission handling: switching one way clears the other.
    mode, skip = getattr(a, "worker_permission_mode", None), getattr(a, "skip_permissions", None)
    if mode and (cfg.skip_permissions or mode != cfg.worker_permission_mode):
        cfg.worker_permission_mode, cfg.skip_permissions = mode, False
        changed.append(f"worker-permission-mode={mode}")
    elif skip and not cfg.skip_permissions:
        cfg.skip_permissions = True
        changed.append("skip-permissions")
    specs = getattr(a, "adversary_mcp", None)
    if specs and specs != cfg.adversary_mcps:
        cfg.adversary_mcps = list(specs)
        changed.append(f"adversary-mcp={','.join(specs)}")
    for cmd in getattr(a, "verify_cmd", None) or []:
        if cmd not in cfg.verify_cmds:
            cfg.verify_cmds.append(cmd)
            changed.append(f"verify-cmd={cmd}")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(prog="adversary", description="Supervise a Claude Code worker with an adversarial Claude Code reviewer.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start a supervised run")
    r.add_argument("task", nargs="?", help="the task for the worker (or use --task-file)")
    r.add_argument("--task-file", help="read the task from a file")
    r.add_argument("--repo", default=".", help="folder to work in (default: current directory)")
    r.add_argument("--worktree", action="store_true", help="run in a fresh git worktree on branch adversary/<ts>")
    # Set by `adversary manage` when it launches a card's run; a run started by hand has no card.
    r.add_argument("--card", help=argparse.SUPPRESS)
    r.add_argument("--board-env", help=argparse.SUPPRESS)
    r.add_argument("--workspace-info", help=argparse.SUPPRESS)   # set by the manager: the card's workspace
    r.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)   # launched by the manager
    add_settings(r, resume=False)
    s = sub.add_parser("resume", help="resume the last run in this folder (or a given run)",
                       description="Resume a run. Every setting flag is optional: anything not given keeps "
                                   "what the run used, and a given one is saved for later resumes too.")
    s.add_argument("message", nargs="?", help="optional new instructions, passed to the adversary")
    s.add_argument("--repo", default=".", help="folder whose last run to resume (default: current directory)")
    s.add_argument("--run", help="a specific run: its directory or id (e.g. 20261001-143802-0421)")
    s.add_argument("--kill", action="store_true", help="close the run's tmux session if it is still open")
    add_settings(s, resume=True)
    m = sub.add_parser("manage", help="keep a pool of runs working a 4ga project's cards",
                       description="Watch a 4ga project and run adversary on its cards: each card in Approved To "
                                   "Do gets its own run (up to --pool at once) in its own workspace, and moves "
                                   "through In Progress, Needs Feedback and Done as the run goes. Flags after "
                                   "`--` are passed to every run (e.g. -- --worker-permission-mode auto).")
    m.add_argument("--project", required=True, help="the 4ga project's name (created, with a board, if missing)")
    m.add_argument("--folder", default=".", help="the project's code folder (default: current directory)")
    m.add_argument("--pool", type=int, default=2, help="runs at once (default 2)")
    m.add_argument("--member", action="append", metavar="EMAIL",
                   help="a person kept on the project as a manager and on its boards (repeatable; also "
                        "FOURGA_MEMBERS in the credentials file). At least one is required: a project only the "
                        "bot can see is no use to anyone.")
    m.add_argument("--board-env", metavar="FILE", help="4ga credentials (default ~/.config/adversary/4ga.env)")
    m.add_argument("--interval", type=float, default=15, help="seconds between board checks (default 15)")
    m.add_argument("--workspaces", help="where card workspaces go (default ~/.local/state/adversary/workspaces/<project>)")
    m.add_argument("--done-window-days", type=float, default=30, metavar="DAYS",
                   help="a comment on a Done card resumes its run (to answer, or to revise the work) for this many "
                        "days after it was finished (default 30)")
    m.add_argument("--once", action="store_true", help="one pass, then exit")
    m.add_argument("run_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)
    elif a.cmd == "resume":
        resume(a)
    elif a.cmd == "manage":
        manage(a)


if __name__ == "__main__":
    main()
