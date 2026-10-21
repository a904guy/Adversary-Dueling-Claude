"""Run configuration and the per-run files handed to the two Claude Code instances."""

import json
import os
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from adversary import cardmode, limits, mcps

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
    "ls", "wc", "diff",
]


# The supervisor's extra tools on a card run (the worker gets request_feedback alone).
CARD_TOOLS = ["request_feedback", "report_done", "suggest_task", "attach_image", "tick_checklist",
              "add_checklist_item"]


@dataclass
class RunConfig:
    task: str
    repo: str
    run_dir: str
    sock: str
    base_sha: str | None
    skip_permissions: bool = False
    worker_permission_mode: str | None = None
    adversary_permission_mode: str | None = None   # "auto" or "strict" (None: DEFAULT_ADVERSARY_MODE)
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
    git_subdir_bases: dict = field(default_factory=dict)   # subdir -> its HEAD when the run started
    adversary_mcps: list[str] = field(default_factory=list)   # --adversary-mcp specs; empty = the browsers, read-only
    cpu_quota: str | None = None    # --cpu-quota, e.g. "100%" (one core): the run's systemd slice (see limits)
    memory_max: str | None = None   # --memory-max, e.g. "3G"
    card: dict | None = None        # the 4ga card this run works ({"id", "url"}); see cardmode
    board_env: str | None = None    # 4ga credentials file for a card run (default ~/.config/adversary/4ga.env)
    workspace: dict | None = None   # a card workspace's repos/branches (see workspace.create)
    managed: bool = False           # launched by `adversary manage`: close by itself when the card is done

    @property
    def card_mode(self) -> bool:
        """A run the manager launched for a card: only then do the card tools, prompts and comment
        relay exist. Every other run works exactly as without a board."""
        return bool(self.card and self.managed)

    def adversary_bash(self) -> list[str]:
        cmds = list(VERIFY_BASH)
        # When the working folder itself isn't one git repo but holds some (common with a
        # framework repo beside the app repo), let the adversary inspect each with `git -C
        # <subdir> <read-only verb>`. Both the path and the verb are pinned, so this stays
        # read-only — a bare `git -C` prefix would also allow commit/reset/push.
        for d in self.git_subdirs:
            cmds += [f"git -C {d} {verb}" for verb in GIT_READONLY]
        # Marking a repo safe only relaxes git's ownership check so it can be read; the launch
        # already does this for the repos found at start, this covers one that appears later.
        cmds.append("git config --global --add safe.directory")
        return cmds + self.verify_cmds

    def safe_dirs(self) -> list[str]:
        """The repos git must trust despite their owner: the working folder and each git repo
        inside it. On a server where the agents run as root over files another user owns, git
        refuses them as "dubious ownership" (no log, no status) until they're safe.directory."""
        dirs = [self.repo] + [str(Path(self.repo) / d) for d in self.git_subdirs]
        # A card workspace's worktrees belong to the real repos, which report_done merges into.
        return dirs + [r["source"] for r in (self.workspace or {}).get("repos", []) if r.get("source")]

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


def git_safe_env(dirs: list[str], env: dict | None = None) -> dict[str, str]:
    """Environment entries that make git trust `dirs` for this process and its children, without
    touching anyone's ~/.gitconfig: git reads GIT_CONFIG_COUNT/KEY_n/VALUE_n as command-line
    config, one of the scopes safe.directory is honoured from. Appends after any already set."""
    env = os.environ if env is None else env
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        n = 0
    out = {}
    for d in dirs:
        out[f"GIT_CONFIG_KEY_{n}"], out[f"GIT_CONFIG_VALUE_{n}"] = "safe.directory", d
        n += 1
    out["GIT_CONFIG_COUNT"] = str(n)
    return out


def _git_safe_sh(dirs: list[str]) -> str:
    """The same as git_safe_env, as POSIX sh for a launch script (counting from what's set)."""
    if not dirs:
        return ""
    return ("_n=${GIT_CONFIG_COUNT:-0}\n"
            f"for _d in {' '.join(shlex.quote(d) for d in dirs)}; do\n"
            '  export "GIT_CONFIG_KEY_$_n=safe.directory" "GIT_CONFIG_VALUE_$_n=$_d"; _n=$((_n + 1))\n'
            "done\n"
            "export GIT_CONFIG_COUNT=$_n\n")


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read: copied MCP definitions can carry API keys."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)   # an existing file keeps its old mode through O_CREAT


# Settings for both agents that keep Claude Code from opening dialogs by itself: one that takes the
# keyboard swallows whatever the bridge types next. /auto-mode-setup's "Teach auto mode about your
# environment?" opened in a card's worker and held up the run for ten hours.
QUIET_UI = {"skillOverrides": {"auto-mode-setup": "off"}}
# The worker's defaults when a run doesn't name them: auto mode (Claude Code's classifier approves
# what's safe; the rest still goes to the supervisor) and the latest Opus (`opus` is Claude Code's
# alias for it), whatever the account's own default model is.
DEFAULT_WORKER_MODE = "auto"
DEFAULT_WORKER_MODEL = "opus"
# The supervisor's Bash: "auto" lets Claude Code's classifier pass ordinary commands (lsusb, ps,
# journalctl, curl localhost…) so it can check claims about the machine, not just the code, and stop
# risky ones; "strict" keeps it to the read/verify allowlist. Either way it can't Edit or Write.
ADVERSARY_MODES = ["auto", "strict"]
DEFAULT_ADVERSARY_MODE = "auto"

BASH_NOTE = {
    "auto": (
        "Your Bash is open to ordinary commands: inspect the machine as well as the code (`lsusb`, `ps`, "
        "`systemctl status`, `journalctl`, `curl` against the app, running the tests and the program) so "
        "you can check the worker's claims for yourself. Claude Code stops risky commands; if one is "
        "denied, find a read-only way to see the same thing. **Look, don't change.** Never edit files, "
        "install anything, restart services, commit or fix the worker's work yourself, even when it would "
        "be quicker: if something needs changing, tell the worker. Your job is to direct and verify.\n"),
    "strict": (
        "Your Bash access is an allowlist of read and verify commands. Run each one as a single simple "
        "command, with no `&&`, pipes or redirects. If a command is denied, try a simpler form or use "
        "Read/Grep/Glob, and don't conclude that Bash is unavailable. You cannot edit files, and you should "
        "not try. Your job is to direct and verify.\n"),
}


def adversary_mode(cfg: "RunConfig") -> str:
    return cfg.adversary_permission_mode or DEFAULT_ADVERSARY_MODE


def draft_path(cfg: RunConfig, role: str) -> Path:
    """Where the draft mod in `role`'s session reports whether its prompt box holds a draft."""
    return Path(cfg.run_dir) / f"draft-{role}.json"


def write_launch_files(cfg: RunConfig) -> dict[str, Path]:
    """Write settings/MCP config and a launch script per agent; return the scripts."""
    run = Path(cfg.run_dir)
    pkg_root = str(Path(__file__).resolve().parent.parent)
    base_events = ["SessionStart", "UserPromptSubmit", "Stop"]

    worker_events = base_events + ([] if cfg.skip_permissions else ["PermissionRequest"])
    worker_settings = {**_hooks(cfg, "worker", worker_events), **QUIET_UI}
    if cfg.card_mode:
        # The worker's request_feedback reaches the bridge through its own copy of the bridge's MCP
        # server, which offers it nothing else; the call itself needs no approval.
        worker_settings["permissions"] = {"allow": ["mcp__adversary__request_feedback"]}
        (run / "worker-mcp.json").write_text(json.dumps({"mcpServers": {"adversary": {
            "command": sys.executable, "args": ["-m", "adversary.mcp_server"],
            "env": {"ADVERSARY_SOCK": cfg.sock, "ADVERSARY_ROLE": "worker", "PYTHONPATH": pkg_root}}}}, indent=2))
        (run / "worker-role.md").write_text(cardmode.worker_note(cfg.workspace))
    (run / "worker-settings.json").write_text(json.dumps(worker_settings, indent=2))
    # Its launch skips the settings where your .mcp.json answers are saved; pass them on so it
    # doesn't stop at the "New MCP servers found" dialog every launch.
    (run / "adversary-settings.json").write_text(json.dumps(
        {**_hooks(cfg, "adversary", base_events), **QUIET_UI, **mcps.approvals(cfg.repo)}, indent=2))
    # The supervisor's MCP servers: the bridge's own, plus the worker's browsers read-only (or the
    # ones named with --adversary-mcp). Its launch doesn't load the user's servers, so they're
    # handed over here.
    mcp = mcps.plan(cfg.adversary_mcps, cfg.repo)
    _write_private(run / "adversary-mcp.json", json.dumps({
        "mcpServers": {
            **mcp.servers,
            "adversary": {
                "command": sys.executable,
                "args": ["-m", "adversary.mcp_server"],
                "env": {"ADVERSARY_SOCK": cfg.sock, "PYTHONPATH": pkg_root,
                        **({"ADVERSARY_CARD": "1"} if cfg.card_mode else {})},
            },
        }
    }, indent=2))
    # The role prompt, plus a note of the external tools this run's supervisor has.
    role = (PROMPTS / "adversary_role.md").read_text() + "\n" + BASH_NOTE[adversary_mode(cfg)]
    if mcp.labels:
        role += (
            "\n\n## External tools this run\n"
            f"You also have these MCP servers: {', '.join(mcp.labels)}. Use them to **verify** the worker's "
            "claims — load the page it built, take a snapshot or screenshot, read the console and network "
            "errors — not to do the work or change anything outside the repo. A browser server that drives a "
            "shared, logged-in browser profile may already be in use by the worker (or fail as busy); prefer "
            "an isolated/headless one when there is a choice.\n")
    if cfg.card_mode:
        role += cardmode.role_note(cfg.workspace)
    (run / "adversary-role.md").write_text(role)

    worker = ["claude", "--settings", str(run / "worker-settings.json"), "--disallowedTools", "AskUserQuestion"]
    if cfg.strict_approvals:
        # User-level allow rules would let some tool calls skip the adversary.
        worker += ["--setting-sources", "project,local"]
    if cfg.skip_permissions:
        worker.append("--dangerously-skip-permissions")
    else:
        worker += ["--permission-mode", cfg.worker_permission_mode or DEFAULT_WORKER_MODE]
    worker += ["--model", cfg.worker_model or DEFAULT_WORKER_MODEL]
    if cfg.worker_effort:
        worker += ["--effort", cfg.worker_effort]
    if cfg.card_mode:
        # Files people attach to the card are downloaded here; both sides may read them.
        (run / "attachments").mkdir(exist_ok=True)
        worker += ["--mcp-config", str(run / "worker-mcp.json"),
                   "--append-system-prompt-file", str(run / "worker-role.md"),
                   "--add-dir", str(run / "attachments")]

    # Read-only inspection plus web research (WebFetch/WebSearch) so the supervisor can study
    # references the task points at — a GitHub repo, docs, an API — and plan from them itself,
    # rather than being blind to anything outside the local repo. Still no way to change the project.
    allowed = ["Read", "Grep", "Glob", "WebFetch", "WebSearch",
               "mcp__adversary__approve_tool", "mcp__adversary__deny_tool",
               "mcp__adversary__finish", "mcp__adversary__changed_files", "mcp__adversary__message_worker",
               "mcp__adversary__hold", "mcp__adversary__read_worker", "mcp__adversary__list_agents"]
    if cfg.card_mode:
        allowed += [f"mcp__adversary__{t}" for t in CARD_TOOLS]
    allowed += mcp.allow
    allowed += [f"Bash({c}:*)" for c in cfg.adversary_bash()]
    adversary = [
        "claude",
        # Only project settings: user-level allow rules (e.g. `Bash(sudo tee:*)`) must not
        # widen the adversary's read-only toolset.
        "--setting-sources", "project",
        "--settings", str(run / "adversary-settings.json"),
        "--mcp-config", str(run / "adversary-mcp.json"),
        # auto: the classifier passes what's safe (allowed tools skip it); strict: only the allowlist.
        "--permission-mode", "auto" if adversary_mode(cfg) == "auto" else "dontAsk",
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", "Edit,Write,NotebookEdit,AskUserQuestion",
        "--append-system-prompt-file", str(run / "adversary-role.md"),
    ]
    if mcp.chrome:
        adversary.append("--chrome")
    if cfg.card_mode:
        adversary += ["--add-dir", str(run / "attachments")]
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
            # Trust the run's repos for git even when another user owns them (see safe_dirs).
            + _git_safe_sh(cfg.safe_dirs()) +
            f"exec {shlex.join(limits.prefix(cfg) + argv)}\n"
        )
        script.chmod(0o755)
        scripts[role] = script
    return scripts
