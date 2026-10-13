"""Run configuration and the per-run files handed to the two Claude Code instances."""

import json
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"

# Read-only git subcommands the adversary may run (bare in the repo root, and with `git -C
# <subdir>` for each git repo found inside a non-git working folder).
GIT_READONLY = ["status", "log", "diff", "show", "ls-files"]

# Read/verify-only Bash commands the adversary may run (prefix rules).
VERIFY_BASH = [
    *(f"git {verb}" for verb in GIT_READONLY),
    "pytest", "python -m pytest", "python3 -m pytest", "uv run pytest",
    "npm test", "npm run test", "npm run build", "npm run lint", "npx tsc",
    "pnpm test", "yarn test",
    "cargo test", "cargo build", "cargo check", "cargo clippy",
    "go test", "go build", "go vet",
    "make test", "make check", "make lint",
    "ls", "wc",
]


@dataclass
class RunConfig:
    task: str
    repo: str
    run_dir: str
    sock: str
    base_sha: str | None
    skip_permissions: bool = False
    worker_permission_mode: str | None = None
    max_exchanges: int = 30   # round trips without the human before the worker is held
    worker_model: str | None = None
    adversary_model: str | None = None
    worker_effort: str | None = "high"        # claude --effort for each side (low … max; None: Claude Code's own)
    adversary_effort: str | None = "xhigh"
    verify_cmds: list[str] = field(default_factory=list)
    session: str = ""
    strict_approvals: bool = False
    snapshot_dir: str | None = None   # set when the repo has no git
    stall_after: int = 120   # seconds both agents may sit idle before it is logged as a stall (0: off)
    origin: str | None = None   # folder the run was started from (differs from repo with --worktree)
    sessions: dict = field(default_factory=dict)   # role -> Claude Code session ID, for resuming
    git_subdirs: list[str] = field(default_factory=list)   # git repos inside the working folder

    def adversary_bash(self) -> list[str]:
        cmds = list(VERIFY_BASH)
        if self.snapshot_dir:
            cmds.append("diff")
        # When the working folder itself isn't one git repo but holds some (common with a
        # framework repo beside the app repo), let the adversary inspect each with `git -C
        # <subdir> <read-only verb>`. Both the path and the verb are pinned, so this stays
        # read-only — a bare `git -C` prefix would also allow commit/reset/push.
        for d in self.git_subdirs:
            cmds += [f"git -C {d} {verb}" for verb in GIT_READONLY]
        return cmds + self.verify_cmds

    def save(self) -> Path:
        path = Path(self.run_dir) / "config.json"
        path.write_text(json.dumps(asdict(self), indent=2))
        return path

    @classmethod
    def load(cls, run_dir: str) -> "RunConfig":
        return cls(**json.loads((Path(run_dir) / "config.json").read_text()))


def _hook(cfg: RunConfig, role: str, event: str, timeout: int | None = None) -> dict:
    cmd = f"{shlex.quote(sys.executable)} -m adversary.hook {shlex.quote(cfg.sock)} {role} {event}"
    hook = {"type": "command", "command": cmd}
    if timeout:
        hook["timeout"] = timeout
    return hook


def _hooks(cfg: RunConfig, role: str, events: list[str]) -> dict:
    hooks = {}
    for event in events:
        # A permission decision may wait on a full adversary turn.
        timeout = 86400 if event == "PermissionRequest" else None
        entry = {"hooks": [_hook(cfg, role, event, timeout)]}
        if event == "PermissionRequest":
            entry["matcher"] = "*"
        hooks[event] = [entry]
    return {"hooks": hooks}


def draft_path(cfg: RunConfig, role: str) -> Path:
    """Where the draft mod in `role`'s session reports whether its prompt box holds a draft."""
    return Path(cfg.run_dir) / f"draft-{role}.json"


def write_launch_files(cfg: RunConfig) -> dict[str, Path]:
    """Write settings/MCP config and a launch script per agent; return the scripts."""
    run = Path(cfg.run_dir)
    pkg_root = str(Path(__file__).resolve().parent.parent)
    base_events = ["SessionStart", "UserPromptSubmit", "Stop"]

    worker_events = base_events + ([] if cfg.skip_permissions else ["PermissionRequest"])
    (run / "worker-settings.json").write_text(json.dumps(_hooks(cfg, "worker", worker_events), indent=2))
    (run / "adversary-settings.json").write_text(json.dumps(_hooks(cfg, "adversary", base_events), indent=2))
    (run / "adversary-mcp.json").write_text(json.dumps({
        "mcpServers": {
            "adversary": {
                "command": sys.executable,
                "args": ["-m", "adversary.mcp_server"],
                "env": {"ADVERSARY_SOCK": cfg.sock, "PYTHONPATH": pkg_root},
            }
        }
    }, indent=2))

    worker = ["claude", "--settings", str(run / "worker-settings.json"), "--disallowedTools", "AskUserQuestion"]
    if cfg.strict_approvals:
        # User-level allow rules would let some tool calls skip the adversary.
        worker += ["--setting-sources", "project,local"]
    if cfg.skip_permissions:
        worker.append("--dangerously-skip-permissions")
    else:
        worker += ["--permission-mode", cfg.worker_permission_mode or "manual"]
    if cfg.worker_model:
        worker += ["--model", cfg.worker_model]
    if cfg.worker_effort:
        worker += ["--effort", cfg.worker_effort]

    # Read-only inspection plus web research (WebFetch/WebSearch) so the supervisor can study
    # references the task points at — a GitHub repo, docs, an API — and plan from them itself,
    # rather than being blind to anything outside the local repo. Still no way to change the project.
    allowed = ["Read", "Grep", "Glob", "WebFetch", "WebSearch",
               "mcp__adversary__approve_tool", "mcp__adversary__deny_tool",
               "mcp__adversary__finish", "mcp__adversary__changed_files", "mcp__adversary__message_worker",
               "mcp__adversary__hold", "mcp__adversary__read_worker", "mcp__adversary__list_agents"]
    allowed += [f"Bash({c}:*)" for c in cfg.adversary_bash()]
    adversary = [
        "claude",
        # Only project settings: user-level allow rules (e.g. `Bash(sudo tee:*)`) must not
        # widen the adversary's read-only toolset.
        "--setting-sources", "project",
        "--settings", str(run / "adversary-settings.json"),
        "--mcp-config", str(run / "adversary-mcp.json"),
        "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", "Edit,Write,NotebookEdit,AskUserQuestion",
        "--append-system-prompt-file", str(PROMPTS / "adversary_role.md"),
    ]
    if cfg.snapshot_dir:
        adversary += ["--add-dir", cfg.snapshot_dir]
    if cfg.adversary_model:
        adversary += ["--model", cfg.adversary_model]
    if cfg.adversary_effort:
        adversary += ["--effort", cfg.adversary_effort]

    # The draft mod reports whether the prompt box holds an unsent message (bridge waits if so).
    draftmod = str(Path(__file__).resolve().parent / "draftmod")
    scripts = {}
    for role, argv in (("worker", worker), ("adversary", adversary)):
        argv = argv + ["--plugin-dir", draftmod]
        if cfg.sessions.get(role):
            argv = argv + ["--resume", cfg.sessions[role]]
        draft_file = draft_path(cfg, role)
        draft_file.unlink(missing_ok=True)  # a previous launch's report is stale
        script = run / f"{role}.sh"
        # Wait for the bridge socket so no early hook event is lost.
        script.write_text(
            "#!/bin/sh\n"
            f"while [ ! -S {shlex.quote(cfg.sock)} ]; do sleep 0.2; done\n"
            f"cd {shlex.quote(cfg.repo)}\n"
            f"export PYTHONPATH={shlex.quote(pkg_root)}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"export ADVERSARY_DRAFT_FILE={shlex.quote(str(draft_file))}\n"
            f"exec {shlex.join(argv)}\n"
        )
        script.chmod(0o755)
        scripts[role] = script
    return scripts
