"""The bridge: relays between the worker and adversary Claude Code panes.

Hooks tell the bridge what happened (a turn ended, a prompt was submitted, a
permission is wanted). The bridge speaks by pasting text into a pane. It never
interprets what the agents say. The only structured signals are the
adversary's MCP tool calls (approve_tool, deny_tool, message_worker, hold, read_worker, list_agents,
changed_files, finish).
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from adversary import board as fourga
from adversary import cardmode, limits, sessions, snapshot, tmux, workspace
from adversary.config import RunConfig, draft_path, git_safe_env, write_launch_files
from adversary.transcript import (Transcript, background_running, latest_progress, find_subagent_log, last_assistant_text, recent_activity,
                                   subagent_finished, subagent_logs, token_usage, turn_had_tool_use, write_report)

WORKER, ADVERSARY = "worker", "adversary"
PASTE_SETTLE = 0.4   # seconds between paste and Enter (plus a little per KB)
SUBMIT_RETRY = 2.5   # seconds to wait for UserPromptSubmit before pressing Enter again
SUBMIT_RETRIES = 3
PASTE_CONFIRM = 10.0  # seconds to wait for a paste to show in the prompt box before pressing Enter
IDLE_SETTLE = 1.0    # seconds after Stop/SessionStart before pasting
RESUME_SETTLE = 1.5  # seconds after a permission decision before pasting a queued message_worker
DRAFT_RECHECK = 1.0  # seconds between checks while a person has a draft in the pane
DELIVERY_RETRY = 5.0  # seconds before retrying a delivery tmux refused
UNDELIVERED_AFTER = 30.0  # seconds a paste may go unsubmitted, with nothing running, before it's sent again
RESTART_DELAY = 5.0       # seconds before an agent that exited is started again; doubles with each restart
STALL_NUDGES = 3          # times in a row the supervisor is woken for a stall before the bridge only logs it
MAX_RESTARTS = 5          # restarts of one agent within RESTART_WINDOW before the bridge gives up on it
RESTART_WINDOW = 3600.0
DIALOG_DISMISS = 60.0     # seconds a dialog may cover the prompt of an unwatched pane before Esc closes it
# Footer hints of a Claude Code dialog or menu that owns the keyboard (the prompt box's own footer
# says "esc to interrupt", lower case). Typed text goes into the dialog, not the prompt.
DIALOG_HINT = re.compile(r"\bEsc to (?:cancel|go back|close|exit)\b|\bEnter to (?:continue|confirm|select)\b")
QUIT_GRACE = 10.0    # seconds the agents get to exit on SIGTERM before SIGKILL
MAX_PERM_REMINDERS = 2
QUIET_HOLD_AT = 2    # consecutive no-op worker turns (after an adversary reply) before an auto-hold
HOLD_CHECK_AFTER = 600   # seconds a hold on background work lasts before the supervisor is asked to check
HOLD_CHECK_MAX = 3600    # each unanswered check-in doubles the wait, up to this
_UNSET = object()    # sentinel: a cached value not computed yet (None is a valid result)
CARD_POLL = 15       # seconds between checks of a card run's comments
PARK_AFTER = 900     # seconds a managed card run waiting on people sits idle before it closes (frees its slot)
DONE_IDLE = 120      # seconds a Done card's follow-up run sits idle, its question answered, before it closes
LIVE_TEXT = 800       # characters of each agent's latest words in the live comment
STATUS_REFRESH = 120  # seconds between live-progress refreshes when nothing changed (to update the "ago")
# Characters per card comment. 4ga itself takes a 1,000,000-character comment (measured), so this is
# for reading: a longer agent message is split into numbered parts.
CARD_PART_MAX = 8000
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}   # what attach_image puts on a card
IMAGE_MAX = 10 * 1024 * 1024
HELP_DIR = Path(__file__).resolve().parent / "help"
# One-time help popups: (key, title, pane it relates to). Each is shown once per project.
POPUPS = {"intro": ("how this works", ADVERSARY), "worker_tip": ("the worker pane", WORKER)}
WORKING_SPINNER = re.compile(r"…\s*\(\d")  # "Processing… (24s · ↓ 2.3k tokens)", "Running… (7s)"


@dataclass
class Pane:
    role: str
    target: str
    busy: bool = True              # until SessionStart says the UI is up
    started: bool = False          # SessionStart seen (it fires again on compaction and /clear)
    queue: deque = field(default_factory=deque)   # (body, reason)
    reason: object = None          # why the current turn is running
    last_reason: object = None     # why the previous turn ran
    expecting_submit: bool = False  # we pasted; the next UserPromptSubmit should be ours
    pending: tuple | None = None   # (header, body, reason) of that paste, until its submit is seen
    pending_at: float = 0.0        # monotonic time of that paste
    human_prompts: list = field(default_factory=list)  # typed by the human since last relay
    direct: list = field(default_factory=list)  # message_worker texts waiting to be typed in mid-turn
    last_text: str = ""            # final message of its last turn
    transcript_path: str | None = None  # this session's JSONL, from live hook payloads
    waiting_on: str | None = None  # why a delivery waits: "a draft", "shell mode", "copy mode", "a dialog"
    waiting_since: float = 0.0     # monotonic time that wait began
    lost: tuple | None = None      # a delivery whose paste was dropped (header only): sent again after the turn
    idle_at: float | None = None   # wall-clock time its last turn ended (for the card's live comment)
    wait_retry: bool = False       # a recheck is scheduled
    launched: bool = False         # its session has come up at least once (only then is an exit a death)
    restarts: list = field(default_factory=list)   # monotonic times it was started again
    restart_at: float | None = None  # when it's due to be started again, after it exited
    died: str = ""                 # why it last exited, for the note to the supervisor


@dataclass
class PermRequest:
    id: int
    tool: str
    input: dict
    future: asyncio.Future
    reminders: int = 0
    agent_id: str = ""      # set when the request comes from a subagent, not the main worker
    agent_type: str = ""    # the subagent's type/name, e.g. "security-reviewer"


def split_message(text: str, limit: int) -> list[str]:
    """Split text into parts of at most `limit` characters, at a paragraph break if there is one,
    else a line break, else mid-line. A ``` code block cut by a split is closed at the end of its part
    and reopened at the start of the next, so each part renders on its own."""
    parts: list[str] = []
    fence = ""   # the opening fence line of a code block left open by the previous part
    while text:
        room = limit - (len(fence) + 1 if fence else 0) - 4   # space for a reopened fence and a closing ```
        if len(fence) + len(text) + (1 if fence else 0) <= limit:
            parts.append((fence + "\n" if fence else "") + text)
            break
        cut = text.rfind("\n\n", 0, room)
        if cut < room // 2:
            cut = text.rfind("\n", 0, room)
        if cut < room // 2:
            cut = room
        chunk, text = text[:cut].rstrip(), text[cut:].lstrip("\n")
        body = (fence + "\n" if fence else "") + chunk
        opened = ""
        for line in body.splitlines():
            if line.lstrip().startswith("```"):
                opened = "" if opened else line.strip()
        if opened:
            body += "\n```"
        parts.append(body)
        fence = opened
    return parts


class Bridge:
    def __init__(self, cfg: RunConfig, panes: dict[str, str], deliver=None, log=print, screen_idle=None,
                 resume: str | None = None, focus=None, focused=None, popup=None, has_draft=None,
                 pane_mode=None, capture=None, dismiss=None, pane_state=None, respawn=None):
        self.cfg = cfg
        self.panes = {role: Pane(role, target) for role, target in panes.items()}
        self.deliver = deliver or self._tmux_deliver
        self.screen_idle = screen_idle or self._tmux_screen_idle
        self.focus = focus or self._tmux_focus
        self.focused = focused or self._tmux_focused
        self.popup = popup or self._tmux_popup
        self.has_draft = has_draft or self._draft_file_says
        self.pane_mode = pane_mode or self._tmux_pane_mode
        self.capture = capture or (lambda pane: tmux.capture(pane.target))
        self.dismiss = dismiss or self._tmux_dismiss
        self.pane_state = pane_state or self._tmux_pane_state
        self.respawn = respawn or self._tmux_respawn
        self._oom_seen = limits.oom_kills(cfg) or 0   # the slice's OOM kills so far (a resume starts from them)
        self._popup_task: asyncio.Task | None = None
        self._popups_unsupported = False   # this tmux rejected display-popup; stop retrying
        # Per-project record (which help popups were shown), kept with the runs, keyed by folder.
        project = re.sub(r"[^\w.-]+", "-", cfg.origin or cfg.repo).strip("-")
        self.project_file = Path(cfg.run_dir).parent.parent / "projects" / f"{project}.json"
        self.log_fn = log
        self.transcript = Transcript(cfg.run_dir)
        self.perms: dict[int, PermRequest] = {}
        self.next_perm = 1
        self.exchanges = 0         # forwarded adversary replies since the human last spoke (or resume)
        self.total_exchanges = 0   # over the run's whole life, for the report
        self.quiet_rounds = 0      # worker turns in a row that did nothing after an adversary reply
        self.approvals = 0
        self.denials = 0
        # Lifetime counters for the end-of-run stats (all persisted; see STATE_KEYS).
        self.worker_turns = 0      # worker turn-ends seen
        self.adversary_turns = 0   # adversary turn-ends seen
        self.holds = 0             # times the worker was parked (cap, quiet loop, or the hold tool)
        self.peeks = 0             # read_worker calls
        self.nudges = 0            # message_worker calls
        self.human_prompts_count = 0  # prompts the human typed to either pane
        self._started = time.monotonic()  # this process's start, for session runtime
        self._started_wall = time.time()  # wall-clock start, to scope subagent files to this session
        self._prior_runtime = 0    # runtime from earlier sessions of this run (seconds)
        self._prior_tokens = 0     # tokens written in earlier sessions of this run
        self._session_tokens_cache = 0  # this session's tokens written, refreshed only on a final save
        self._tok_cache: dict | None = None   # memoised token read for one shutdown render
        self._diff_cache = _UNSET  # memoised diff read for one shutdown render
        self.is_resume = resume is not None
        self.paused = False
        self.finished: str | None = None
        self.held: str | None = None           # adversary's hold reason: worker parked, nothing to relay
        self.held_backlog: list[str] = []      # worker messages kept back by an auto-hold, sent with its next one
        self.hold_check_at: float | None = None   # monotonic time to ask the supervisor about a hold
        self.hold_check_every = HOLD_CHECK_AFTER
        self._held_since = 0.0
        self.summary = ""
        self.idle_since: float | None = None   # when both agents last went idle together
        self.stalls = 0
        self.stall_nudges = 0   # stall wake-ups since the worker last ended a turn
        self.plan_sent = False                  # the worker has been given its first instructions
        self._resumed_at = 0.0                   # monotonic time of the last permission decision
        self._flush_tasks: set[asyncio.Task] = set()
        self.request_stop = None   # set by serve(): ends the run (a managed card run closes itself)
        # A card run talks to its 4ga card (see cardmode). Without working credentials it still runs,
        # but nothing reaches the card; the log says why.
        self.card: cardmode.CardLink | None = None
        self._card_busy = False
        self._card_next = 0.0
        self._parked_since: float | None = None
        self._card_checked = False   # looked up the card's column at start
        self._card_said = False      # this turn already posted its closing summary (report_done / can't complete)
        self._status_key, self._status_at = "", -1e9   # what the status comment last showed, and when
        self.card_followup = False   # resumed for a comment on a Done card; ends when work goes to the worker
        if cfg.card_mode:
            try:
                self.card = cardmode.CardLink(cfg)
            except fourga.BoardError as e:
                self.log(f"card {cfg.card.get('id')}: can't reach the board, so nothing will be posted to it: {e}")

        if resume is None:
            # The worker is NOT given the task directly: the adversary gets it, studies the
            # repo, and ends its first turn with a plan, which is forwarded to the worker as its
            # instructions. One planner, so the two don't start from competing plans.
            self.enqueue(ADVERSARY, self._adversary_brief(), "init")
        else:
            # Both sessions come back with their history; the adversary decides what happens next.
            self._load_state()
            self.transcript.add("Run resumed", resume)
            self.enqueue(ADVERSARY, resume, "resume")
        # Session metrics are deltas against this snapshot of the lifetime counters: on a fresh
        # run every baseline is 0 (session == total); on resume they start at the loaded totals.
        self._base = {k: getattr(self, k) for k in self.STATE_KEYS}

    # ── state kept across resumes ───────────────────────────────────────
    STATE_KEYS = ("total_exchanges", "approvals", "denials", "stalls", "next_perm",
                  "worker_turns", "adversary_turns", "holds", "peeks", "nudges", "human_prompts_count")

    def _state_path(self) -> Path:
        return Path(self.cfg.run_dir) / "state.json"

    def save_state(self, final: bool = False) -> None:
        # Tokens and the diff mean reading the session logs and shelling out to git, so only
        # refresh them on a final save (finish or the run closing), not on every turn-end; carry
        # the last token figure forward in between. The caches dedupe the reads within one
        # shutdown render; clear them so a final save recomputes against the latest state.
        if final:
            self._tok_cache, self._diff_cache = None, _UNSET
            self._session_tokens_cache = self._session_tokens()["total"]
        data = {k: getattr(self, k) for k in self.STATE_KEYS}
        data["runtime_seconds"] = self._runtime_total()
        data["tokens_total"] = self._prior_tokens + self._session_tokens_cache
        try:
            self._state_path().write_text(json.dumps(data))
        except OSError as e:
            self.log(f"could not save state: {e}")

    def _load_state(self) -> None:
        try:
            state = json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            # Runs from before state.json: keep permission numbers unique at least.
            text = self.transcript.path.read_text()
            state = {"next_perm": max(map(int, re.findall(r"Permission request #(\d+)", text)), default=0) + 1}
        state.setdefault("total_exchanges", state.get("exchanges"))  # state.json before the split
        for k in self.STATE_KEYS:
            if isinstance(state.get(k), int):
                setattr(self, k, state[k])
        if isinstance(state.get("runtime_seconds"), (int, float)):
            self._prior_runtime = int(state["runtime_seconds"])
        if isinstance(state.get("tokens_total"), int):
            self._prior_tokens = state["tokens_total"]
        # `exchanges` starts again at 0: resuming is the human picking the run back up.

    # ── text sent to the agents ─────────────────────────────────────────
    def _adversary_brief(self) -> str:
        return (
            "You are supervising a worker on this task. The worker has NOT been told the task — you "
            "have. Nobody else plans this run: you do, so the worker builds exactly what the task "
            "needs and the two of you don't start from competing plans.\n\n"
            f"<task>\n{self.cfg.task}\n</task>\n\n"
            f"Repository: {self.cfg.repo}\n{self._baseline_text()}\n"
            f"Worker permissions: {'all tool calls pre-approved (skip-permissions)' if self.cfg.skip_permissions else 'routed to you for approval'}\n\n"
            "Your Bash access is limited to these command prefixes, each run as ONE simple command "
            "(no `&&`, `;`, pipes, redirects, loops or `cd`; you are already in the repo): "
            f"{', '.join(self.cfg.adversary_bash())}. Anything else is denied, which does not mean Bash is "
            "unavailable. Use Read/Grep/Glob to inspect files, and WebFetch/WebSearch to research "
            "anything the task points you at that isn't in the repo — a referenced GitHub repo's source "
            "and docs, an API, a spec. You MUST run the project's tests yourself before calling finish.\n\n"
            "Study the repository now — and whatever else the task points you at — then END YOUR TURN with "
            "the worker's instructions: the goal in your own words and a concrete plan for how to accomplish "
            "it. That reply is forwarded to the worker verbatim as its first instructions and starts it "
            "working, so write it to the worker, not to me, and don't use message_worker for it.\n\n"
            "The plan must be YOUR analysis and decisions. If the task tells you to research, investigate, "
            "design or 'build a plan' first, that work is yours — do it before you hand off. Do NOT just "
            "restate the task and tell the worker to go figure out the plan: a plan that says 'research X "
            "and write the plan' is not a plan. The worker executes your plan and writes any documents you "
            "can't (you have no Write tool), but the thinking — which pieces, which approach, what is and "
            "isn't possible and why — is yours and belongs in the hand-off (or you verify the worker's "
            "version against the source yourself; 'worker, figure it out' is not supervising).\n\n"
            "Take as many tool calls as the planning needs — the worker stays idle until you hand off, so "
            "there is no rush to end the turn. Research references yourself on the web (a GitHub repo the "
            "task names, documentation, an API): WebFetch a file or page, WebSearch to find it. Only when "
            "something genuinely isn't reachable that way (a private repo, or a tree so large you need it "
            "on disk) make your FIRST hand-off 'fetch it to <a path I can read> and stop', read it when the "
            "worker returns, then send the real plan with message_worker. You still verify the result "
            "against the task yourself before calling finish.\n\n"
            "If the task is too ambiguous to plan without my input, call hold and end your turn with your "
            "question to me instead: nothing reaches the worker until you message_worker, so no half-formed "
            "plan leaks."
        )

    def _baseline_text(self) -> str:
        if self.cfg.base_sha:
            return (f"Base commit before any work: {self.cfg.base_sha} (see changes with "
                    f"`git diff {self.cfg.base_sha}` and `git status`, or the changed_files tool)")
        text = ("This folder is NOT a git repository. Its original contents were snapshotted to "
                f"{self.cfg.snapshot_dir}/files before the worker started. Use the changed_files tool to "
                f"list what changed, and `diff -ru {self.cfg.snapshot_dir}/files <file-or-dir>` (or Read "
                "both copies) to see how. Large files and dependency/build dirs were not copied.")
        if self.cfg.git_subdirs:
            subs = ", ".join(self.cfg.git_subdirs)
            text += ("\nThese subdirectories ARE git repositories, so you can inspect their history with "
                     f"`git -C <subdir> log`, `git -C <subdir> status` and `git -C <subdir> diff` "
                     f"(e.g. `git -C {self.cfg.git_subdirs[0]} log --oneline -20`): {subs}.")
        return text

    def changes_text(self) -> str:
        repo = self.cfg.repo
        if self.cfg.base_sha:
            run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout.strip()
            return (f"git status --short:\n{run('status', '--short') or '(clean)'}\n\n"
                    f"git diff --stat {self.cfg.base_sha[:12]}:\n{run('diff', '--stat', self.cfg.base_sha) or '(none)'}")
        if not self.cfg.snapshot_dir:
            return "(no baseline recorded for this run)"
        return snapshot.format_changes(snapshot.changes(repo, self.cfg.snapshot_dir))

    @staticmethod
    def _perm_text(req: PermRequest) -> str:
        detail = json.dumps(req.input, indent=2)
        if len(detail) > 4000:
            detail = detail[:4000] + "\n… (truncated)"
        origin = ""
        if req.agent_id or req.agent_type:
            who = req.agent_type or "a subagent"
            origin = (f"\n\nThis is NOT the main worker — it is **{who}**, a subagent the worker spawned"
                      + (f" (agent id `{req.agent_id}`)" if req.agent_id else "") + ".")
            if req.agent_id:
                origin += f" See what it has been doing with `read_worker(agent=\"{req.agent_id}\")`."
            origin += " If you deny, your reason goes to that subagent (the one that asked), not the main worker."
        return (
            f"Worker requests permission #{req.id}: {req.tool}{origin}\n\n```json\n{detail}\n```\n\n"
            f"Decide with approve_tool(request_id={req.id}) or deny_tool(request_id={req.id}, reason=...)."
        )

    # ── logging / status ────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        self.log_fn(f"{datetime.now():%H:%M:%S} {msg}")

    def status(self) -> str:
        w, a = self.panes[WORKER], self.panes[ADVERSARY]
        state = self.finished or ("PAUSED" if self.paused else "running")
        return (f"[{state}] exchanges since you {self.exchanges}/{self.cfg.max_exchanges} · approvals {self.approvals} · "
                f"denials {self.denials} · pending perms {len(self.perms)} · worker {'busy' if w.busy else 'idle'} · "
                f"adversary {'busy' if a.busy else 'idle'}" + (" · worker on hold" + (f" ({len(self.held_backlog)} msg kept)" if self.held_backlog else "")
                   if self.held else "")
                + "".join(f" · waiting on you in the {p.role} pane ({p.waiting_on})" for p in (w, a) if p.waiting_on))

    # ── delivery ────────────────────────────────────────────────────────
    def enqueue(self, role: str, text: str, reason, priority: bool = False) -> None:
        pane = self.panes[role]
        if reason == "worker_msg" and self.held_backlog:
            text = self._with_backlog(text)
        if reason == "worker_msg":
            # Several worker turns piled up (adversary busy, or relay paused): deliver them as one.
            for i, (body, r) in enumerate(pane.queue):
                if r == "worker_msg":
                    pane.queue[i] = (f"{body}\n\n---\n\n(A later message from the worker:)\n\n{text}", r)
                    self.schedule_flush(role, delay=0)
                    return
        (pane.queue.appendleft if priority else pane.queue.append)((text, reason))
        self.schedule_flush(role, delay=0)

    def schedule_flush(self, role: str, delay: float | None = None) -> None:
        if delay is None:
            delay = IDLE_SETTLE
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not running yet (constructor); flushed on SessionStart
        task = loop.create_task(self._flush_later(role, delay))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _flush_later(self, role: str, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        pane = self.panes[role]
        if self.paused or pane.busy or pane.expecting_submit or not pane.queue or self._waiting_on_person(pane):
            return
        body, reason = pane.queue.popleft()
        human = self.panes[WORKER].human_prompts
        if role == ADVERSARY and reason == "worker_msg" and human:
            said = "\n\n".join(f"> {p}" for p in human)
            body = ("NOTE: the human typed these instructions directly to the worker. They come from the person "
                    "you stand in for, so they amend the original task and take precedence over it where the two "
                    f"conflict. Review against the task as amended:\n\n{said}\n\n---\n\n{body}")
            human.clear()
        if role == ADVERSARY and reason == "worker_msg":
            self.release_hold("the worker sent a message")
        header = self.header(role, reason)
        # The turn takes `reason` only once its UserPromptSubmit shows this text went in as a
        # prompt: a paste that ran as a shell command, or never went in, routes nothing.
        pane.busy, pane.expecting_submit, pane.pending = True, True, (header, body, reason)
        pane.pending_at = time.monotonic()
        self.log(f"→ {role}: {self._short(body)}")
        try:
            await self.deliver(role, header, body)
        except Exception as e:
            self._delivery_failed(pane, e)

    @staticmethod
    def header(role: str, reason) -> str:
        """One typed line that precedes the pasted body.

        Claude Code wraps long pastes in <pasted_content> and won't act on pasted
        instructions unless the typed part of the message asks it to, so every
        delivery is typed header + pasted body.
        """
        kind = reason[0] if isinstance(reason, tuple) else reason
        return {
            "task": "Here is your task (pasted below). Treat it as my request:",
            "plan": "Your supervisor's plan for this task (pasted below). Follow it as if I wrote it:",
            "adversary_msg": "Message from your supervisor (pasted below). Follow it as if I wrote it:",
            "init": "Your supervision brief (pasted below). Follow it as if I wrote it:",
            "resume": "This supervised run was resumed (details pasted below). Follow it as if I wrote it:",
            "worker_msg": "The worker agent ended its turn with this message (pasted below):",
            "perm": "Permission request from the worker (pasted below). Decide it with your tools:",
            "direct": "Message from your supervisor, sent while you were working (pasted below). Follow it as if I wrote it:",
            "human_comment": "A person commented on the card (pasted below). It comes from the people you work for; act on it:",
        }.get(kind, "Message from the adversary bridge (pasted below):")

    async def _tmux_deliver(self, role: str, header: str, body: str) -> None:
        pane = self.panes[role]
        tmux.type_text(pane.target, header + " ")
        tmux.paste(pane.target, body, buffer=f"adv-{role}")
        await asyncio.sleep(PASTE_SETTLE + len(body) / 20000)
        # A session that's still busy (starting up, or held to one core by --cpu-quota) can drop or lag a
        # paste; Enter then submits the header alone. Wait until the paste shows in the prompt box.
        waited = 0.0
        while waited < PASTE_CONFIRM and not self.paste_visible(pane, body):
            await asyncio.sleep(0.3)
            waited += 0.3
        tmux.press(pane.target, "Enter")
        for _ in range(SUBMIT_RETRIES):
            await asyncio.sleep(SUBMIT_RETRY)
            if not pane.expecting_submit:
                return
            # Enter again only while the box still starts with our message: never submit
            # something a person typed, or a shell command.
            report = self._draft_report(pane)
            if report is not None and not str(report.get("head", "")).startswith(header[:40]):
                self.log(f"{role}: the pasted message never reached the prompt box (it holds something else, "
                         f"or is empty) and wasn't submitted; it was NOT delivered: {self._short(body)}")
                return
            if self.pane_mode(pane):
                self.log(f"{role}: no submit seen, and the pane is in {self.pane_mode(pane)}; not pressing Enter")
                return
            self.log(f"{role}: no submit seen yet, pressing Enter again")
            tmux.press(pane.target, "Enter")

    def paste_visible(self, pane: Pane, body: str) -> bool:
        """Whether the prompt box shows the pasted body: Claude Code's "[Pasted text …]" placeholder
        for a long paste, or the body's opening for a short one. True when the screen can't be read."""
        try:
            screen = self.capture(pane)
        except Exception:
            return True
        if not screen:
            return True
        if "[Pasted text" in screen:
            return True
        first = next((l.strip() for l in body.splitlines() if l.strip()), "")[:24]
        return not first or first in " ".join(screen.split())

    @staticmethod
    def _body_missing(pending: tuple, prompt: str) -> bool:
        """A delivery whose header went in without its pasted body (the paste was dropped)."""
        header, body, _ = pending
        if not body.strip():
            return False
        p = prompt.strip()
        if "[Pasted text" in p or "<pasted_content" in p:
            return False
        start = body.strip()[:80]
        return start not in prompt and len(p) <= len(header) + 20

    @staticmethod
    def _is_notification(prompt: str) -> bool:
        """A prompt Claude Code injected itself, not something the person typed: a background
        task finishing, or a message from a subagent the worker launched. Subagents report back
        as 'Another Claude session sent a message:' with a <teammate-message> block, not as a
        <task-notification>; both must count, or a subagent's progress update is mistaken for
        human input and resets the loop guard."""
        p = prompt.lstrip()
        return (p.startswith("<task-notification>")
                or p.startswith("Another Claude session sent a message:")
                or "<teammate-message" in p[:200])

    @staticmethod
    def _is_delivery(pending: tuple, prompt: str) -> bool:
        """Whether a submitted prompt is the bridge's paste rather than something a person typed.

        Claude Code shows the typed header, then the paste either verbatim, wrapped in
        <pasted_content>, or as a "[Pasted text #1 +40 lines]" placeholder.
        """
        header, body, _ = pending
        p = prompt.lstrip()
        if p.startswith(header.split(" (")[0]) or p.startswith("[Pasted text"):
            return True
        start = body.strip()[:80]
        return bool(start) and start in prompt

    @staticmethod
    def _blank(text: str) -> bool:
        """No visible text: whitespace only, or invisible characters such as a zero-width space."""
        return all(c.isspace() or unicodedata.category(c) in ("Cf", "Cc", "Zs", "Zl", "Zp") for c in text)

    @staticmethod
    def _short(text: str, n: int = 90) -> str:
        one = " ".join(text.split())
        return one if len(one) <= n else one[: n - 1] + "…"

    # ── hook events ─────────────────────────────────────────────────────
    @staticmethod
    def _in_log(path: str | None, body: str) -> bool:
        """Whether a session log holds `body` (its opening, as written in the JSONL) near its end."""
        if not path:
            return False
        probe = body.strip()[:120]
        if not probe:
            return True
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 2_000_000))
                tail = f.read().decode(errors="replace")
        except OSError:
            return False
        return probe in tail or json.dumps(probe)[1:-1] in tail

    def _record_session(self, role: str, payload: dict) -> None:
        """Remember `role`'s Claude Code session id, for `adversary resume`."""
        sid = payload.get("session_id")
        if sid and self.cfg.sessions.get(role) != sid:
            self.cfg.sessions[role] = sid
            self.cfg.save()

    async def on_hook(self, role: str, event: str, payload: dict) -> dict:
        pane = self.panes[role]
        # Keep the session's live JSONL path for read_worker. Only a real session fires
        # UserPromptSubmit/Stop/PermissionRequest; SessionStart can come from a stub (a failed
        # resume), so it only fills a path we don't have yet — same caution as _record_session.
        tp = payload.get("transcript_path")
        if tp and (event != "SessionStart" or not pane.transcript_path):
            pane.transcript_path = tp
        if event == "SessionStart":
            # Record the id only if we have none yet. On resume we already know the id we
            # launched with, and Claude Code can fire SessionStart for a throwaway stub session
            # (a failed resume writes only metadata) before the real one runs; overwriting the
            # known id with that stub makes the next resume open an empty session and the pane
            # dies. The id that actually runs a turn is captured at Stop instead.
            if not self.cfg.sessions.get(role):
                self._record_session(role, payload)
            if pane.started:
                return {}  # compaction or /clear, possibly mid-turn: not a new idle UI
            pane.started = pane.launched = True
            self.log(f"{role} session {payload.get('source') or 'started'}")
            if role == ADVERSARY:
                self.focus(pane)  # the human talks to the supervisor, so it gets the keyboard
            pane.busy = False
            self.schedule_flush(role)
            if role == WORKER:
                self._send_direct()   # messages that waited while it was starting (again)
            return {}
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            in_turn, pane.busy = pane.busy, True
            if role == WORKER:
                self.release_hold("the worker started a turn")
            if pane.expecting_submit and self._is_delivery(pane.pending, prompt):
                if self._body_missing(pane.pending, prompt):
                    # Only the header went in: send the whole message again once this turn ends.
                    self.log(f"{role}: the message went in without its content (the paste was dropped); "
                             "resending it after this turn")
                    pane.lost = pane.pending
                if pane.pending[2] != "direct":
                    pane.reason = pane.pending[2]  # a mid-turn message keeps the running turn's reason
                pane.expecting_submit, pane.pending = False, None
                if role == WORKER:
                    self._send_direct()
            elif self._is_notification(prompt):
                # Claude Code injects these itself (a background command finished), often
                # mid-turn. Not the human: the turn keeps its reason, and an idle pane's new
                # turn continues the previous one, so its reply is routed the same way.
                if not in_turn:
                    pane.reason = pane.last_reason
                self.log(f"{role}: background task notification")
                self.transcript.add(f"Notification → {role}", prompt)
            else:
                if pane.expecting_submit:
                    # A person submitted before our paste went in: their prompt, not ours.
                    in_turn = self._requeue_pending(pane)
                if not in_turn:
                    pane.reason = "user"  # mid-turn typing is queued input; the turn keeps its reason
                if self.finished and role == WORKER:
                    # The human restarted the worker by hand on a finished run; let its reply
                    # flow back to the adversary. (Typing to the adversary waits for its
                    # message_worker call, so a parting "thanks" doesn't reopen the run.)
                    self.reopen("the human typed new work into the worker")
                self._human_spoke()
                self.human_prompts_count += 1
                pane.human_prompts.append(prompt)
                self.log(f"human typed into {role}: {self._short(prompt)}")
                self.transcript.add(f"Human → {role}", prompt)
            return {}
        if event == "Stop":
            if pane.expecting_submit and pane.pending and pane.pending[2] != "direct":
                # A turn ran without our message going in as a prompt: a shell command (`!`)
                # or something else the person started. Their turn; ours is sent again.
                self.log(f"{role}: the bridge's message wasn't submitted as a prompt; resending it after this turn")
                self._requeue_pending(pane, quiet=True)
            elif pane.expecting_submit and pane.pending and pane.pending[2] == "direct":
                # A message typed in mid-turn whose submit never showed: some Claude Code versions fold
                # it into the running turn without a UserPromptSubmit. Waiting on that would hold back
                # every later message for this pane, so settle it now from the session log: in it,
                # it was delivered; not in it, it's sent again as the next message.
                body = pane.pending[1]
                pane.expecting_submit, pane.pending = False, None
                if self._in_log(payload.get("transcript_path") or pane.transcript_path, body):
                    self.log(f"{role}: the mid-turn message went in with the turn (no submit event); delivered")
                else:
                    self.log(f"{role}: the mid-turn message never went in; sending it again")
                    pane.direct.insert(0, body)
            if pane.lost:
                header, body, lost_reason = pane.lost
                pane.lost = None
                pane.queue.appendleft(("Your previous message arrived without its content (the paste was dropped). "
                                       f"Here it is in full; act on it rather than on the empty one:\n\n{body}",
                                       lost_reason))
                # This empty turn's reply mustn't be routed as if it answered the real message.
                pane.reason = None
            # The session that just ran a turn is the live one; a stub never gets here. This keeps
            # the recorded id pointing at the real session even when a resume forks to a new id.
            self._record_session(role, payload)
            pane.busy = False
            pane.idle_at = time.time()
            text = last_assistant_text(payload)
            # Only the worker's no-op turns drive the quiet-loop hold, and only when the adversary
            # started the turn; read the transcript for a tool call just in that case.
            had_tool = turn_had_tool_use(payload) if role == WORKER and pane.reason == "adversary_msg" else True
            self.on_stop(role, pane.reason, text, had_tool)
            pane.last_reason, pane.reason = pane.reason, None
            self.schedule_flush(role)
            if role == WORKER:
                self._send_direct()
            return {}
        if event == "PermissionRequest" and role == WORKER:
            return await self.on_permission(payload)
        return {}

    def on_stop(self, role: str, reason, text: str, had_tool_use: bool = True) -> None:
        self.panes[role].last_text = text
        self._on_stop(role, reason, text, had_tool_use)
        self.save_state()

    def _on_stop(self, role: str, reason, text: str, had_tool_use: bool = True) -> None:
        if role == WORKER:
            self.worker_turns += 1
        else:
            self.adversary_turns += 1
        if role == WORKER:
            self.stall_nudges = 0
            self.transcript.add("Worker", text or "(no text)")
            self.log(f"worker ended turn: {self._short(text or '(no text)')}")
            # Any permission still pending is moot now: the worker's turn is over.
            for req in list(self.perms.values()):
                self._resolve(req, {}, f"request #{req.id} expired (worker turn ended)")
            if self.finished:
                return
            if self._blank(text):
                text = "(the worker ended its turn without a message)"
            else:
                self._card_say("Message from Worker to Supervisor", text)
            # A turn the adversary's own reply started, in which the worker did nothing (no tool
            # call), while background work it launched is still out, means it is only waiting on
            # that work. Repeated, the two are only echoing ("still waiting" / "ok"); park it rather
            # than relay another no-op that the adversary will just answer, restarting the loop.
            # With nothing in the background, a text-only reply is conversation — "I don't
            # understand X", "I'll get to that after Y" — and always goes through; the exchange
            # cap still guards against a genuine back-and-forth loop.
            waiting = (self._background_work() if reason == "adversary_msg" and not had_tool_use else [])
            if waiting:
                self.quiet_rounds += 1
                if self.quiet_rounds >= QUIET_HOLD_AT and not self.held:
                    # Not relayed now (that restarts the echo), but not lost either: kept and sent
                    # with the worker's next message, so the supervisor sees the whole chain.
                    self.held_backlog.append(text)
                    self._quiet_hold(waiting)
                    return
            else:
                self.quiet_rounds = 0
            self.enqueue(ADVERSARY, text, "worker_msg")
            return

        # adversary
        self.transcript.add(f"Adversary ({reason})", text or "(no text)")
        if isinstance(reason, tuple) and reason[0] == "perm":
            req = self.perms.get(reason[1])
            if req:
                if req.reminders < MAX_PERM_REMINDERS:
                    req.reminders += 1
                    self.enqueue(ADVERSARY, f"Permission request #{req.id} is still pending. Call approve_tool or "
                                 f"deny_tool with request_id={req.id}.", ("perm", req.id), priority=True)
                else:
                    self.denials += 1
                    self._resolve(req, self._decision(False, "The supervisor did not decide; treat as denied."),
                                  f"#{req.id} auto-denied (no decision)")
            return
        if reason not in ("worker_msg", "resume", "init") or self.finished or self.held:
            # Not forwarded to the worker. On a card run, what the supervisor said for people (to a
            # person, or while the worker is parked or the run is over) goes on the card.
            said, self._card_said = self._card_said, False
            if reason in ("user", "human_comment", "worker_msg", "resume", "init") and not self._blank(text) and not said:
                self._card_say("Message from Supervisor", text)
            if reason == "human_comment" and not self.finished and self._worker_idle():
                # A reply to a person never reaches the worker. If the supervisor wrote the worker's next
                # step there (common right after a feedback answer), the run would sit still: say so once.
                self.enqueue(ADVERSARY, "Your reply went on the card for the people; it did NOT reach the worker, "
                             "which is still idle" + (" (on hold)" if self.held else "") + ". If it should work now "
                             "— e.g. the answer you were waiting for has come — send it the work with message_worker. "
                             "If it should keep waiting, call hold. Your reply to this note isn't posted or forwarded.",
                             "idle_note")
            return  # on hold: the worker stays idle and this reply goes nowhere
        if self._blank(text):
            if reason != "init":
                self.enqueue(ADVERSARY, "Your last turn produced no message for the worker. Reply to the worker "
                             "(your reply is forwarded verbatim), call hold to leave it idle, or call finish.",
                             "worker_msg")
            elif not self.plan_sent:
                # Init with no plan and nothing sent to the worker yet: ask for one. (If the plan
                # already went out via message_worker, a blank end-of-turn is fine — stay quiet.)
                self.enqueue(ADVERSARY, "Your turn produced no plan for the worker. Study the repo if you "
                             "need to, then end your turn with the goal and a concrete plan — that reply is "
                             "forwarded to the worker and starts it.", "init")
            return
        if reason == "init":
            # The plan is the human's own task reaching the worker for the first time, not a
            # round-trip the loop guard should count. Forward it once, unless message_worker
            # already delivered the first instructions during this turn.
            if not self.plan_sent:
                self.plan_sent = True
                self.enqueue(WORKER, text, "plan")
                self._card_say("Message from Supervisor to Worker", text)
            return
        if self.exchanges >= self.cfg.max_exchanges:
            self._cap_reached(text)
            return
        self.exchanges += 1
        self.total_exchanges += 1
        # Sent right away, like message_worker: into the worker's running turn if it is busy (Claude
        # Code folds it in), else as its next turn — never parked until the worker goes idle.
        self.panes[WORKER].direct.append(text)
        self._card_say("Message from Supervisor to Worker", text)
        self._send_direct()

    def _cap_reached(self, text: str) -> None:
        """The two have gone back and forth max_exchanges times without the human: a loop
        guard. The worker is put on hold, not the run ended; the human picks it back up."""
        cap = self.cfg.max_exchanges
        self.holds += 1
        self._set_hold(f"{cap} exchanges since the human last spoke", None)   # waits on the human: no check-in
        self.log(f"exchange cap: {cap} round trips without you; worker on hold, the adversary's reply not forwarded")
        self.transcript.add(f"Exchange cap ({cap}): worker on hold", f"Not forwarded to the worker:\n\n{text}")
        self.enqueue(ADVERSARY, f"Your last reply was not forwarded: you and the worker have gone back and forth {cap} "
                     "times without the human, so the worker is on hold in case the two of you are looping. Nothing "
                     "is lost. The count starts again when the human types to either of you; to carry on before "
                     "that, send the worker what it needs with message_worker. Your reply to this note isn't "
                     "forwarded.", "cap_note")

    def _background_work(self) -> list[str]:
        """Background tasks/subagents the worker launched this session that are still out."""
        path = self._worker_log()
        if not path:
            return []
        try:
            return background_running(path, since=self._started_wall - 60)   # this session, as in _subagents_note
        except Exception as e:   # never let a log quirk break relaying
            self.log(f"couldn't check the worker's background work: {e}")
            return []

    def _quiet_hold(self, waiting: list[str] | None = None) -> None:
        """The worker has ended turn after turn doing nothing in reply to the adversary while
        background work it launched is still running: that work will bring it back on its own,
        and the two are only echoing. Park the worker and tell the adversary to stop replying."""
        on = ", ".join(waiting[:4]) + (f" (+{len(waiting) - 4} more)" if len(waiting or []) > 4 else "") if waiting else ""
        self.holds += 1
        self._set_hold("worker idle, waiting on background work" + (f": {on}" if on else ""), HOLD_CHECK_AFTER)
        self.quiet_rounds = 0
        self.log("quiet-loop hold: the worker did nothing after the adversary's replies; worker on hold")
        self.transcript.add("Quiet-loop hold: worker on hold",
                            "The worker ended consecutive turns with no action after the adversary's replies.")
        self.enqueue(ADVERSARY,
                     "The worker has ended several turns in a row without doing anything after your replies. It "
                     "has nothing for you right now — it is waiting on background work"
                     + (f" ({on})" if on else "") + ", which will "
                     "bring it back to you on its own when it finishes. The worker is now on hold, so stop "
                     "replying: don't send it anything and don't check in, or you'll just restart the loop. End "
                     "your turn. Its latest message wasn't lost: it is kept and comes to you together with its "
                     "next one. It will return to you when there is real work, and if it hasn't within "
                     f"{HOLD_CHECK_AFTER // 60} minutes the bridge will ask you to check on it; if the human gives "
                     "you something new in the meantime, use message_worker then. Your reply to this note isn't "
                     "forwarded.",
                     "quiet_note")

    def _human_spoke(self) -> None:
        if self.exchanges:
            self.log(f"exchange count reset (was {self.exchanges}): you spoke")
        self.exchanges = 0
        self.quiet_rounds = 0

    def _send_direct(self) -> None:
        """Deliver message_worker texts now, even mid-turn: Claude Code queues a prompt typed
        while it works and folds it into the running turn. Held while a permission request
        is pending (a dialog may own the keyboard) or another delivery awaits its submit."""
        pane = self.panes[WORKER]
        if not pane.direct or self.paused or self.finished or self.perms or pane.expecting_submit:
            return
        if not pane.started:
            return   # starting (or restarting): sent from its SessionStart
        if pane.busy and self._waiting_on_person(pane):
            return  # (an idle worker's message goes through the queue, which checks too)
        if pane.busy:
            # Just after a permission decision the worker is resuming and its prompt box isn't
            # back yet; a paste now is lost. Wait out the rest of the resume settle, then retry.
            remaining = RESUME_SETTLE - (time.monotonic() - self._resumed_at)
            if remaining > 0:
                try:
                    asyncio.get_running_loop().call_later(remaining, self._send_direct)
                    return
                except RuntimeError:
                    pass
        body = "\n\n---\n\n".join(pane.direct)
        pane.direct.clear()
        if not pane.busy:
            self.enqueue(WORKER, body, "adversary_msg", priority=True)
            return
        header = self.header(WORKER, "direct")
        pane.expecting_submit, pane.pending = True, (header, body, "direct")  # its submit fires right away
        pane.pending_at = time.monotonic()
        self.log(f"→ worker (mid-turn): {self._short(body)}")
        task = asyncio.get_running_loop().create_task(self._deliver_direct(pane, header, body))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _deliver_direct(self, pane: Pane, header: str, body: str) -> None:
        try:
            await self.deliver(pane.role, header, body)
        except Exception as e:
            self._delivery_failed(pane, e)

    def _delivery_failed(self, pane: Pane, e: Exception) -> None:
        """tmux refused the keys (the pane briefly unreachable, say): undo the delivery's
        state and send it again shortly, rather than leave the pane marked busy forever."""
        detail = getattr(e, "stderr", None) or e
        self.log(f"{pane.role}: delivery failed ({str(detail).strip()}); retrying in {DELIVERY_RETRY:.0f}s")
        direct = pane.pending is not None and pane.pending[2] == "direct"
        self._requeue_pending(pane, quiet=True)
        if not direct:
            pane.busy = False
        loop = asyncio.get_running_loop()
        loop.call_later(DELIVERY_RETRY, self.schedule_flush, pane.role, 0)
        if pane.role == WORKER:
            loop.call_later(DELIVERY_RETRY, self._send_direct)

    # ── waiting for the person ──────────────────────────────────────────
    def _draft_report(self, pane: Pane) -> dict | None:
        """What the draft mod in that session last reported, or None with no report (the
        mod isn't loaded, or an older Claude Code without mods)."""
        try:
            report = json.loads(draft_path(self.cfg, pane.role).read_text())
        except (OSError, ValueError):
            return None
        return report if isinstance(report, dict) else None

    def _draft_file_says(self, pane: Pane) -> bool:
        return (self._draft_report(pane) or {}).get("draft") is True

    def _tmux_pane_mode(self, pane: Pane) -> str | None:
        """A pane state that would swallow or misread typed text: copy mode (keys drive the
        scrollback), or Claude Code's shell mode (`!`: Enter runs the box as a command).
        Shell mode is read off the screen, as the prompt line's `!` glyph in place of `❯`;
        mods don't report it."""
        try:
            if tmux.tmux("display", "-p", "-t", pane.target, "#{pane_in_mode}") == "1":
                return "copy mode"
        except subprocess.CalledProcessError:
            return None
        screen = tmux.capture(pane.target)
        if dialog_open(screen):
            return "a dialog"
        for line in reversed(screen.splitlines()):
            if line.startswith("❯"):
                return None
            if line.startswith(("!\u00a0", "! ")):
                return "shell mode"
        return None

    def _waiting_on_person(self, pane: Pane) -> bool:
        """True while typing into the pane would land somewhere other than an empty prompt: a
        message half written, shell mode, copy mode, or a dialog over the prompt. Nothing is
        typed until that's done. A dialog nobody is watching (Claude Code opens some by itself)
        is closed with Esc after DIALOG_DISMISS seconds. Rechecked every DRAFT_RECHECK seconds."""
        why = "a draft" if self.has_draft(pane) else self.pane_mode(pane)
        if why is None:
            if pane.waiting_on:
                self.log(f"{pane.role}: no longer waiting ({pane.waiting_on} done); delivering")
                pane.waiting_on = None
            return False
        if pane.waiting_on != why:
            pane.waiting_on, pane.waiting_since = why, time.monotonic()
            self.log({"a draft": f"{pane.role}: you have a message drafted there; waiting until it's sent or cleared",
                      "shell mode": f"{pane.role}: the prompt is in shell mode (!); waiting until it's back to normal",
                      "copy mode": f"{pane.role}: the pane is in copy mode; waiting until you leave it",
                      "a dialog": f"{pane.role}: a dialog covers the prompt; waiting until it's closed"}[why])
        elif why == "a dialog" and time.monotonic() - pane.waiting_since >= DIALOG_DISMISS:
            pane.waiting_since = time.monotonic()   # one Esc per DIALOG_DISMISS
            if self.dismiss(pane):
                self.log(f"{pane.role}: a dialog has covered the prompt for {DIALOG_DISMISS:.0f}s with nobody "
                         "watching; closed it with Esc")
        if not pane.wait_retry:
            pane.wait_retry = True
            asyncio.get_running_loop().call_later(DRAFT_RECHECK, self._recheck_wait, pane.role)
        return True

    def _tmux_dismiss(self, pane: Pane) -> bool:
        """Press Esc in a pane nobody is attached to; False (left alone) when someone is."""
        try:
            if tmux.tmux("display", "-p", "-t", pane.target, "#{session_attached}") not in ("", "0"):
                return False
            tmux.press(pane.target, "Escape")
        except subprocess.CalledProcessError:
            return False
        return True

    def check_undelivered(self, now: float | None = None) -> None:
        """Called every second. A paste whose submit never came, with no turn running, was lost:
        typed into a dialog that had taken the keyboard, say. Left alone, the pane waits for that
        submit forever and every later message to it is held back. After UNDELIVERED_AFTER
        seconds it's settled from the session log: delivered if it's there, sent again if not."""
        now = time.monotonic() if now is None else now
        for pane in self.panes.values():
            if (not pane.expecting_submit or not pane.pending or now - pane.pending_at < UNDELIVERED_AFTER
                    or not self.screen_idle(pane)):
                continue
            header, body, reason = pane.pending
            report = self._draft_report(pane)
            if report and report.get("draft") and str(report.get("head", "")).startswith(header[:40]):
                # Still in the prompt box, unsubmitted: Enter, not a second copy.
                pane.pending_at = now
                self.log(f"{pane.role}: the bridge's message is still unsent in the prompt box; pressing Enter")
                self._press_enter(pane)
                continue
            if self._in_log(pane.transcript_path, body):
                self.log(f"{pane.role}: the message went in without a submit event; delivered")
                pane.expecting_submit, pane.pending = False, None
                continue
            self.log(f"{pane.role}: the bridge's message never went in ({UNDELIVERED_AFTER:.0f}s, no submit, "
                     f"nothing running); sending it again: {self._short(body)}")
            self._requeue_pending(pane, quiet=True)
            pane.busy = False   # nothing's running: the message goes in as a new prompt
            if reason == "direct":
                self._send_direct()
            else:   # first; mid-turn messages held behind it follow once it's submitted
                self.schedule_flush(pane.role, delay=0)

    # ── agents that exit ────────────────────────────────────────────────
    def check_agents(self, now: float | None = None) -> None:
        """Called every second. An agent whose Claude Code exited on its own (killed for running out
        of the run's memory, a crash) is started again with its conversation (`--resume`), after
        RESTART_DELAY seconds doubling with each restart, and the supervisor is told what happened.
        After MAX_RESTARTS within RESTART_WINDOW it's left down: a managed run closes then, so the
        manager can start it fresh."""
        now = time.monotonic() if now is None else now
        for pane in self.panes.values():
            if pane.restart_at is not None:
                if now >= pane.restart_at:
                    self._restart(pane, now)
                continue
            if not pane.launched:
                continue   # still coming up: the startup watch looks after that
            state = self.pane_state(pane)
            if state is not None:
                self._agent_exited(pane, state, now)

    def _agent_exited(self, pane: Pane, state: str, now: float) -> None:
        kills = limits.oom_kills(self.cfg)
        oom = kills is not None and kills > self._oom_seen
        self._oom_seen = kills if kills is not None else self._oom_seen
        pane.died = (f"the kernel killed it for running out of the run's memory (limit {self.cfg.memory_max})"
                     if oom else f"its Claude Code exited ({state})")
        pane.started, pane.busy, pane.reason, pane.waiting_on = False, True, None, None
        if pane.expecting_submit:
            self._requeue_pending(pane, quiet=True)   # whatever was being typed in goes again
        if pane.role == WORKER:
            for req in list(self.perms.values()):
                self._resolve(req, {}, f"request #{req.id} expired (the worker exited)")
        recent = [t for t in pane.restarts if now - t < RESTART_WINDOW]
        self.transcript.add(f"{pane.role.capitalize()} exited", pane.died)
        if len(recent) >= MAX_RESTARTS:
            pane.restart_at = float("inf")
            self.log(f"{pane.role}: exited again: {pane.died}. It was started again {len(recent)} times in the "
                     f"last {RESTART_WINDOW / 60:.0f} minutes, so it's left down"
                     + ("; closing the run so the manager can start it fresh" if self.cfg.managed else ""))
            if self.cfg.managed and self.request_stop:
                self.request_stop()
            return
        delay = min(RESTART_DELAY * 2 ** len(recent), 300.0)
        pane.restart_at = now + delay
        self.log(f"{pane.role}: {pane.died}; starting it again in {delay:.0f}s with its conversation")

    def _restart(self, pane: Pane, now: float) -> None:
        pane.restart_at = None
        pane.restarts.append(now)
        try:
            new = self.respawn(pane)
        except Exception as e:
            detail = getattr(e, "stderr", None) or e
            self.log(f"{pane.role}: couldn't start it again ({str(detail).strip()})")
            self._agent_exited(pane, "it couldn't be started again", now)
            return
        if new:
            pane.target = new
        self.log(f"{pane.role}: started again (attempt {len(pane.restarts)})")
        memory = (" It was killed for memory: tell it to keep memory use down from here (one build or test "
                  "run at a time, nothing heavy in parallel or left running in the background)."
                  if "memory" in pane.died else "")
        if pane.role == WORKER:
            note = (f"The worker stopped: {pane.died}. The bridge started it again with its conversation history, "
                    "but whatever it was doing was cut off (a command it was running is gone) and it hasn't been "
                    "told anything since. Check where it got to (read_worker, the repo's state), then give it its "
                    f"next step; your reply is forwarded to it.{memory}")
        else:
            note = (f"Your session stopped: {pane.died}. The bridge started it again with your conversation "
                    "history. Anything you were in the middle of was cut off, and messages may have been lost "
                    "either way, so don't assume your last reply reached the worker. Check the worker "
                    f"(read_worker) and decide the next step; your reply is forwarded to it.{memory}")
        self.enqueue(ADVERSARY, note, "resume")

    def _tmux_pane_state(self, pane: Pane) -> str | None:
        """None while the agent's pane runs; otherwise how it ended. Each pane execs into claude, so
        the pane closes when it exits (or stays, dead, under remain-on-exit)."""
        try:
            out = tmux.tmux("display-message", "-p", "-t", pane.target,
                            "#{pane_id} #{pane_dead} #{pane_dead_status} #{pane_dead_signal}")
        except subprocess.CalledProcessError:
            return "its pane closed"
        parts = out.split()
        if not parts or parts[0] != pane.target:
            return "its pane closed"   # tmux resolved the target to some other pane
        if len(parts) > 1 and parts[1] == "1":
            return f"signal {parts[3]}" if len(parts) > 3 and parts[3] not in ("", "0") else f"exit status {parts[2] if len(parts) > 2 else '?'}"
        return None

    def _tmux_respawn(self, pane: Pane) -> str | None:
        """Start the agent again with its session (`--resume`), in its pane if tmux kept it, else in
        a new pane beside the other agent (or above the bridge). Returns a new pane's id."""
        script = str(write_launch_files(self.cfg)[pane.role])
        try:
            dead = tmux.tmux("display-message", "-p", "-t", pane.target, "#{pane_id} #{pane_dead}") == f"{pane.target} 1"
        except subprocess.CalledProcessError:
            dead = False
        if dead:
            tmux.tmux("respawn-pane", "-k", "-t", pane.target, script)
            new = None
        else:
            other = self.panes[ADVERSARY if pane.role == WORKER else WORKER]
            if self.pane_state(other) is None:
                # The worker sits left of the supervisor.
                where = ["-h", "-b"] if pane.role == WORKER else ["-h"]
                anchor = other.target
            else:
                where, anchor = ["-v", "-b"], os.environ.get("TMUX_PANE", "")
            new = tmux.tmux("split-window", *where, "-P", "-F", "#{pane_id}", "-t", anchor, "-c", self.cfg.repo, script)
            tmux.tmux("set-option", "-p", "-t", new, "@adversary_label", tmux.PANE_LABELS[pane.role])
        task = asyncio.get_running_loop().create_task(_startup_watch(self))   # trust dialogs and the like
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)
        return new

    def _press_enter(self, pane: Pane) -> None:
        try:
            tmux.press(pane.target, "Enter")
        except subprocess.CalledProcessError:
            pass

    def _recheck_wait(self, role: str) -> None:
        self.panes[role].wait_retry = False
        self.schedule_flush(role, delay=0)
        if role == WORKER:
            self._send_direct()

    def _requeue_pending(self, pane: Pane, quiet: bool = False) -> bool:
        """Put back a paste that didn't go in as a prompt. Returns whether the pane was
        already mid-turn before that paste."""
        _, body, reason = pane.pending or (None, None, None)
        pane.expecting_submit, pane.pending = False, None
        if reason is None:
            return pane.reason is not None
        if not quiet:
            self.log(f"{pane.role}: a typed prompt was submitted before the bridge's message; resending it after this turn")
        if reason == "direct":
            pane.direct.insert(0, body)
            return True
        pane.queue.appendleft((body, reason))
        return False

    def _set_hold(self, reason: str, check_after: float | None) -> None:
        """Park the worker. With check_after (seconds), the supervisor is asked to look at the hold
        if nothing has woken the worker by then — a background task can hang, or finish without
        the notification that would bring the worker back, and the run would sit parked forever."""
        if not self.held:
            self._held_since = time.monotonic()
        self.held = reason
        self.hold_check_every = check_after or HOLD_CHECK_AFTER
        self.hold_check_at = time.monotonic() + check_after if check_after else None

    def _with_backlog(self, text: str) -> str:
        """Prefix a message for the supervisor with what the worker said while it was on hold."""
        kept, self.held_backlog = self.held_backlog, []
        n = len(kept)
        head = ("(While the worker was on hold it also said the following, which wasn't forwarded at the "
                f"time — {n} message{'s' if n > 1 else ''}, oldest first:)\n\n")
        return head + "\n\n---\n\n".join(kept) + f"\n\n---\n\n(Then:)\n\n{text}"

    def check_hold(self, now: float | None = None) -> bool:
        """Called every second: once a hold's check-in time passes with the worker still parked, tell
        the supervisor how long it has been and what is (or isn't) still running, and let it decide.
        Re-armed at double the interval (capped), so an unanswered hold is never silent for good."""
        now = time.monotonic() if now is None else now
        if not self.held or self.finished or self.hold_check_at is None or now < self.hold_check_at:
            return False
        if self.panes[WORKER].busy:
            return False   # the worker woke; its turn-end releases or replaces the hold
        waiting = self._background_work()
        mins = max(1, round((now - self._held_since) / 60))
        self.hold_check_every = min(self.hold_check_every * 2, HOLD_CHECK_MAX)
        self.hold_check_at = now + self.hold_check_every
        if waiting:
            state = ("Still running in the background: " + "; ".join(waiting[:6])
                     + (f" (+{len(waiting) - 6} more)" if len(waiting) > 6 else "") + ". It may just be slow, "
                     "or stuck — `list_agents` shows when each was last active, and `read_worker(agent=...)` "
                     "what a subagent is doing.")
        else:
            state = ("Nothing the worker launched in the background is still running, yet it hasn't been brought "
                     "back: the work may have ended without the notification that wakes it, or the worker may "
                     "be waiting on something the bridge can't see. Check with `read_worker` / `list_agents`.")
        kept = ""
        if self.held_backlog:
            kept = "\n\nWhat the worker said that was kept back:\n\n" + "\n\n---\n\n".join(self.held_backlog)
            self.held_backlog = []
        self.log(f"hold check-in: worker on hold {mins}m ({len(waiting)} background item(s) running)")
        self.transcript.add("Hold check-in", f"on hold {mins}m: {self.held}")
        self.enqueue(ADVERSARY,
                     f"Check-in: the worker has been on hold for {mins} minute{'s' if mins != 1 else ''} "
                     f"({self.held}). {state}{kept}\n\nDecide: to keep waiting, call `hold` again (optionally "
                     "with `check_after_minutes`); to get it moving, `message_worker` with what to do (e.g. "
                     "check on or stop the stuck task, or carry on without it). If you do neither, you'll be "
                     f"asked again in {self.hold_check_every // 60} minutes. Your reply to this note isn't forwarded.",
                     "hold_check")
        return True

    def release_hold(self, why: str) -> None:
        if self.held:
            self.hold_check_at = None
            self.held = None
            self.log(f"hold released: {why}")
            self.transcript.add(f"Hold released: {why}")

    async def on_permission(self, payload: dict) -> dict:
        if self.finished:
            return {}  # fall back to the on-screen prompt for the human
        loop = asyncio.get_running_loop()
        # A request from a subagent carries agent_id/agent_type; the main worker's carries neither.
        # Keep them so the supervisor is told which agent is asking and can read that agent's log.
        req = PermRequest(self.next_perm, payload.get("tool_name", "?"), payload.get("tool_input", {}),
                          loop.create_future(), agent_id=(payload.get("agent_id") or "").strip(),
                          agent_type=(payload.get("agent_type") or "").strip())
        self.next_perm += 1
        self.perms[req.id] = req
        self.quiet_rounds = 0  # a permission request means the worker is doing real work
        origin = f" (from subagent {req.agent_type or req.agent_id})" if (req.agent_id or req.agent_type) else ""
        self.log(f"permission #{req.id}: {req.tool}{origin} {self._short(json.dumps(req.input), 70)}")
        self.transcript.add(f"Permission request #{req.id}: {req.tool}", f"```json\n{json.dumps(req.input, indent=2)}\n```")
        self.enqueue(ADVERSARY, self._perm_text(req), ("perm", req.id), priority=True)
        return await req.future

    @staticmethod
    def _decision(allow: bool, message: str = "") -> dict:
        decision = {"behavior": "allow"} if allow else {"behavior": "deny", "message": message}
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}

    def _resolve(self, req: PermRequest, output: dict, note: str) -> None:
        self.perms.pop(req.id, None)
        if not req.future.done():
            req.future.set_result(output)
        self.log(note)
        self.transcript.add(note)
        # The worker is resuming from the permission hook; mark the moment so the next mid-turn
        # paste waits for it to come back (see _send_direct). This covers a message_worker queued
        # during the wait and one the human prompts a beat after the decision — both race the
        # worker's return from the permission-pending state, where a paste is simply lost.
        self._resumed_at = time.monotonic()
        self._send_direct()

    # ── MCP tool calls from the adversary ───────────────────────────────
    def on_mcp(self, tool: str, args: dict, role: str = ADVERSARY) -> dict:
        if tool in ("request_feedback", "report_done", "suggest_task", "attach_image", "tick_checklist",
                    "add_checklist_item") \
                or (tool == "finish" and self.card):
            return self._card_tool(tool, args, role)
        if role != ADVERSARY:
            return {"text": f"{tool} is the supervisor's tool.", "error": True}
        if tool in ("approve_tool", "deny_tool"):
            try:
                req = self.perms.get(int(args.get("request_id")))
            except (TypeError, ValueError):
                req = None
            if not req:
                return {"text": f"No pending permission request #{args.get('request_id')} "
                                f"(it may have expired or been answered by the human).", "error": True}
            if tool == "approve_tool":
                self.approvals += 1
                self._resolve(req, self._decision(True), f"approved #{req.id} {args.get('note', '')}".strip())
                return {"text": f"Approved #{req.id}. The worker's tool call will run. This was a "
                                "permission decision, not a message to the worker: nothing you say now reaches "
                                "it, so just end your turn — no reply, and do NOT call hold (the worker is "
                                "mid-turn). Its next request or turn-end will come to you."}
            reason = args.get("reason") or "Denied by supervisor."
            self.denials += 1
            self._resolve(req, self._decision(False, reason), f"denied #{req.id}: {reason}")
            return {"text": f"Denied #{req.id}. The worker sees your reason. This was a permission decision: "
                            "nothing else you say now reaches the worker, so just end your turn — do NOT call "
                            "hold. Its next request or turn-end will come to you."}
        if tool == "message_worker":
            text = (args.get("message") or "").strip()
            if not text:
                return {"text": "message is empty.", "error": True}
            self._card_working()
            reopened = bool(self.finished)
            if reopened:
                self.reopen("the adversary sent the worker new work with message_worker")
            self.nudges += 1
            self.transcript.add("Adversary → worker (message_worker)", text)
            self.plan_sent = True  # the worker has its instructions; don't also auto-forward an init reply
            self.quiet_rounds = 0  # the adversary gave the worker real work; start the loop count fresh
            self.release_hold("the adversary messaged the worker")
            self.panes[WORKER].direct.append(text)
            self._card_say("Message from Supervisor to Worker", text)
            self._send_direct()
            note = "The finished run is live again. " if reopened else ""
            if self.panes[WORKER].busy:
                return {"text": note + "Sent. The worker is mid-turn, so it is delivered into its current turn "
                                "(after any pending permission request is decided)."}
            return {"text": note + "Sent. The worker was idle, so this starts its next turn."}
        if tool == "hold":
            if self.finished:
                return {"text": "The run has finished; relaying has stopped.", "error": True}
            w = self.panes[WORKER]
            if w.busy or w.queue or w.direct:
                return {"text": "The worker is busy or has a message on its way, so its next turn-end will reach "
                                "you anyway. Call hold once it is idle.", "error": True}
            reason = (args.get("reason") or "").strip() or "no reason given"
            # A hold on background work gets a check-in by default (the work can hang, or end without
            # waking the worker); a hold waiting on the human doesn't, unless asked for.
            try:
                minutes = float(args["check_after_minutes"]) if args.get("check_after_minutes") is not None else None
            except (TypeError, ValueError):
                minutes = None
            if minutes is None:
                check_after = HOLD_CHECK_AFTER if self._background_work() else None
            else:
                check_after = max(60.0, minutes * 60) if minutes > 0 else None
            if not self.held:
                self.holds += 1   # renewing a hold (e.g. after a check-in) isn't a new one
            self._set_hold(reason, check_after)
            self.log(f"worker on hold: {reason}" + (f" (check-in in {round(check_after / 60)}m)" if check_after else ""))
            self.transcript.add("Worker on hold", reason)
            when = (f" If nothing has woken it in {round(check_after / 60)} minutes, the bridge will ask you to "
                    "check on it." if check_after else "")
            return {"text": "The worker is on hold. Your reply this turn is not forwarded, and nothing is sent "
                            "to it until you call message_worker (or the human types to it)." + when + " End your turn."}
        if tool == "read_worker":
            return self._read_worker(args)
        if tool == "list_agents":
            return self._list_agents(bool(args.get("all")))
        if tool == "changed_files":
            return {"text": self.changes_text()}
        if tool == "finish":
            outcome = "COMPLETE" if args.get("outcome") == "complete" else "CANNOT_COMPLETE"
            path = self.finish(outcome, args.get("summary", ""))
            return {"text": f"Run finished ({outcome}). Relaying has stopped; report written to {path}. "
                            "If the human later gives you new work, call message_worker: that picks the "
                            "run back up and starts the worker again."}
        return {"text": f"Unknown tool {tool}", "error": True}

    # ── the card (a run launched with --card) ───────────────────────────
    def _worker_idle(self) -> bool:
        w = self.panes[WORKER]
        return not (w.busy or w.queue or w.direct or w.expecting_submit)

    def _card_say(self, header: str, text: str) -> None:
        """Post one message of the run's conversation on the card, headed with who it's from and to;
        a long one goes as several comments, numbered."""
        if not self.card or self._blank(text):
            return
        parts = split_message(text.strip(), CARD_PART_MAX)
        for i, part in enumerate(parts, 1):
            label = f"{header} (part {i} of {len(parts)})" if len(parts) > 1 else header
            self._card_post(f"**{label}:**\n\n{part}")

    def _card_post(self, text: str) -> None:
        if not self.card:
            return
        try:
            self.card.post(text)
        except fourga.BoardError as e:
            self.log(f"couldn't post to the card: {e}")

    def _card_tool(self, tool: str, args: dict, role: str) -> dict:
        if not self.card:
            return {"text": "This run isn't tied to a reachable card" + (" (see the bridge log)" if self.cfg.card else "")
                            + ", so there's no board to report to.", "error": True}
        if role != ADVERSARY and tool != "request_feedback":
            return {"text": f"{tool} is the supervisor's tool.", "error": True}
        try:
            if tool == "request_feedback":
                question = (args.get("question") or "").strip()
                if not question:
                    return {"text": "question is empty.", "error": True}
                who = "Supervisor" if role == ADVERSARY else "Worker"
                self.card.request_feedback(who, question)
                self.log(f"{role} asked for feedback on the card: {self._short(question)}")
                self.transcript.add(f"{who} asked for feedback on the card", question)
                if role != ADVERSARY:
                    self.enqueue(ADVERSARY, "The worker asked the people on the card for feedback (the card is now in "
                                 f"Needs Feedback):\n\n> {question}\n\nTheir answer will come to you as a comment. "
                                 "Your reply to this note isn't forwarded to the worker.", "feedback_note")
                return {"text": "Posted on the card, which is now in Needs Feedback. The answer comes back as a comment "
                                "from a person. Carry on with whatever doesn't depend on it" +
                                ("; if nothing is left, call hold." if role == ADVERSARY else ".")}
            if tool == "suggest_task":
                title = (args.get("title") or "").strip()
                if not title:
                    return {"text": "title is empty.", "error": True}
                tasks = args.get("tasks") or []
                tasks = [str(t) for t in tasks] if isinstance(tasks, list) else []
                new = self.card.suggest(title, args.get("description") or "", tasks)
                self.transcript.add("Suggested a card", f"{title}\n\n{args.get('description') or ''}"
                                    + "".join(f"\n- [ ] {t}" for t in tasks))
                return {"text": f"Filed in Suggested as card {new['id']}" + (f" with {len(tasks)} checklist items"
                                                                              if tasks else "") + "."}
            if tool == "tick_checklist":
                item = str(args.get("item") or "").strip()
                if not item:
                    return {"text": "item is empty: give its number or its name.", "error": True}
                done = args.get("done", True) is not False
                try:
                    name = self.card.tick(item, done)
                except LookupError as e:
                    return {"text": str(e), "error": True}
                self.transcript.add(f"Checklist item {'ticked' if done else 'unticked'}", name)
                return {"text": f"{'Ticked' if done else 'Unticked'}: {name}"}
            if tool == "add_checklist_item":
                name = " ".join(str(args.get("name") or "").split())
                if not name:
                    return {"text": "name is empty.", "error": True}
                self.card.add_item(name)
                self.transcript.add("Checklist item added", name)
                return {"text": f"Added to the checklist: {name}"}
            if tool == "attach_image":
                path = Path(str(args.get("path") or "")).expanduser()
                if not path.is_absolute():
                    path = Path(self.cfg.repo) / path
                if path.suffix.lower() not in IMAGE_TYPES:
                    return {"text": f"attach_image takes an image ({', '.join(sorted(IMAGE_TYPES))}).", "error": True}
                if not path.is_file():
                    return {"text": f"No file at {path}.", "error": True}
                if path.stat().st_size > IMAGE_MAX:
                    return {"text": f"{path.name} is over {IMAGE_MAX >> 20} MB.", "error": True}
                self.card.attach_image(path, bool(args.get("cover")))
                self.transcript.add("Attached an image to the card", str(path))
                return {"text": f"Attached {path.name}" + (" as the card's cover." if args.get("cover") else ".")}
            if tool == "finish":
                if args.get("outcome") == "complete":
                    return {"text": "On a card run, finish with report_done(summary): it merges the work, posts your "
                                    "summary and moves the card to Done.", "error": True}
                summary = args.get("summary", "")
                self.card.post(f"**Supervisor: this can't be completed as it stands.**\n\n{summary}")
                self._card_said = True
                self.card.open.append({"who": "Supervisor", "question": "cannot complete"})
                self.card.save()
                self.card.move(fourga.NEEDS_FEEDBACK)
                path = self.finish("CANNOT_COMPLETE", summary)
                return {"text": f"Run finished (CANNOT_COMPLETE); the card is in Needs Feedback with your summary. "
                                f"Report: {path}. A person's reply comes back to you as a comment."}
            # report_done
            open_items = self.card.unticked()
            leave = (args.get("leave_unticked") or "").strip()
            if open_items and not leave:
                return {"text": "Not done yet: these checklist items are unticked:\n" + "\n".join(
                    f"- {n}" for n in open_items) + "\nTick each one you've verified with tick_checklist. If one "
                    "genuinely shouldn't be done, call report_done again with leave_unticked saying why.",
                    "error": True}
            if self.card.open:
                asks = "\n".join(f"- {o['who']}: {self._short(o['question'], 200)}" for o in self.card.open)
                return {"text": "Not done yet: these feedback requests have no answer from a person on the card:\n"
                                f"{asks}\nWait for the reply (call hold if there's nothing else to do).", "error": True}
            diff = self._diff_text()   # before merging: the merge brings in other cards' work too
            merged: list[str] = []
            commits: list[str] = []
            if self.cfg.workspace:
                commits = workspace.commits(self.cfg.workspace)
                try:
                    merged = workspace.merge(self.cfg.workspace)
                except workspace.MergeError as e:
                    if e.needs_person:
                        # Nothing the worker can fix from its workspace: someone has to act on the real repo.
                        self.log(f"report_done: merge needs a person: {self._short(str(e))}")
                        self.card.label(fourga.ATTENTION_LABEL, fourga.ATTENTION_COLOR)
                        self.card.request_feedback("Supervisor", "The work is finished and verified, but it can't be "
                                                   f"merged without someone:\n\n{e}\n\nReply here once it's sorted "
                                                   "and I'll merge it.")
                        return {"text": f"Not merged — this needs a person, not the worker:\n\n{e}\n\nThe card is in "
                                        "Needs Feedback with that explained and marked Needs attention. Call hold; "
                                        "when someone replies, call report_done again.", "error": True}
                    # The worker knows the code and the branch, so the fix goes straight to it.
                    self.log(f"report_done: merge refused; sent to the worker to fix: {self._short(str(e))}")
                    self.transcript.add("Merge refused at report_done; sent to the worker", str(e))
                    self.release_hold("the merge needs the worker")
                    fix = ("The supervisor reported this card done, but your work couldn't be merged into main yet:\n\n"
                           f"{e}\n\nFix this in your workspace (commit everything; for a conflict, merge the target "
                           "branch into your card branch, resolve the conflicts keeping both sides' intent, run the "
                           "tests, and commit the merge). Then end your turn with what you changed.")
                    self.panes[WORKER].direct.append(fix)
                    self._card_say("Message from the bridge to Worker", fix)
                    self._send_direct()
                    return {"text": f"Not merged, so the card stays open:\n\n{e}\n\nThis was sent to the worker to "
                                    "fix. When it reports back, check the resolution (read the merged files, run "
                                    "the tests), then call report_done again.", "error": True}
            summary = (args.get("summary") or "").strip()
            if open_items and leave:
                summary += ("\n\n**Left unticked:** " + ", ".join(open_items) + f" — {leave}")
            self.card.followup = False
            self.card.post(self._done_comment(summary, merged, commits, diff))
            self._card_said = True   # the turn's closing words would only repeat the Done comment
            self.card.move(fourga.DONE)
            try:
                self.card.label(fourga.ATTENTION_LABEL, fourga.ATTENTION_COLOR, on=False)
            except fourga.BoardError:
                pass
            path = self.finish("COMPLETE", summary)
            if self.cfg.managed and self.request_stop:
                self.card.set_live(False)
                asyncio.get_running_loop().call_later(5, self.request_stop)
            return {"text": f"The card is Done: summary posted{', work merged' if merged else ''}. Report: {path}. "
                            + ("This run closes in a moment." if self.cfg.managed else "End your turn.")}
        except fourga.BoardError as e:
            self.log(f"{tool}: the board refused: {e}")
            return {"text": f"The board refused that ({e}). Try again shortly; if it keeps failing, say so in "
                            "your reply.", "error": True}

    def _done_comment(self, summary: str, merged: list[str], commits: list[str], diff: str | None) -> str:
        tok = self._session_tokens()["total"] + self._prior_tokens
        facts = [f"ran {self._dur(self._runtime_total())}"]
        if tok:
            facts.append(f"{tok:,} tokens written")
        if self.card and self.card.asked:
            facts.append(f"{self.card.asked} feedback request{'s' if self.card.asked != 1 else ''}")
        if diff:
            facts.append(f"diff {diff}")
        body = f"**Done.**\n\n{summary}"
        if merged:
            body += "\n\n**Merged:**\n" + "\n".join(f"- {m}" for m in merged)
        if commits:
            shown = commits[-15:]
            body += ("\n\n**Commits:**\n" + ("- …\n" if len(commits) > len(shown) else "")
                     + "\n".join(f"- {c}" for c in shown))
        return body + "\n\n" + " · ".join(facts)

    async def card_tick(self, now: float | None = None) -> None:
        """Relay people's new comments to the supervisor, and park a managed run that has sat idle
        waiting on people. Runs each serve tick; reaches the board at most every CARD_POLL seconds."""
        if not self.card or self._card_busy:
            return
        now = time.monotonic() if now is None else now
        if now < self._card_next:
            return
        self._card_next = now + CARD_POLL
        self._card_busy = True
        try:
            if not self.card.started:
                await asyncio.to_thread(self.card.start)
            if not self._card_checked:
                self._card_checked = True
                if self.is_resume and (self.card.followup or
                                       await asyncio.to_thread(self.card.column) == fourga.norm(fourga.DONE)):
                    # Resumed for a comment on a finished card (or cut off while answering one): a question
                    # is answered on the card, and the worker stays parked unless the supervisor gives it
                    # work (see _card_working). The card shows In Progress while an agent is on it.
                    self.card_followup = True
                    self.held = "the card is Done; answering a follow-up comment"
                    self.card.followup = True
                    self.card.save()
                    await asyncio.to_thread(self.card.move, fourga.IN_PROGRESS)
                    self.log("follow-up on a Done card: card in In Progress while answering; the worker waits")
            was_open = bool(self.card.open)
            fresh, files = [], []
            # Each on its own: a failure in one must not lose what the other already marked as seen.
            try:
                fresh = await asyncio.to_thread(self.card.new_human_comments)
            except fourga.BoardError as e:
                self.log(f"card: couldn't read comments: {e}")
            try:
                files = await asyncio.to_thread(self.card.new_attachments)
            except fourga.BoardError as e:
                self.log(f"card: couldn't read attachments: {e}")
            changes: list[str] = []
            try:
                changes = await asyncio.to_thread(self.card.checklist_changes)
            except fourga.BoardError as e:
                self.log(f"card: couldn't read the checklist: {e}")
            parts = []
            for c in fresh:
                who = (c.get("data") or {}).get("userName") or "someone"
                text = fourga.comment_text(c)
                self.log(f"card comment from {who}: {self._short(text)}")
                self.transcript.add(f"Card comment from {who}", text)
                parts.append(f"{who}:\n\n{text}")
            if files:
                self.log(f"card: {len(files)} file(s) attached: {', '.join(a['name'] for a in files)}")
                self.transcript.add("Files attached to the card", cardmode.attachments_text(files))
                parts.append(cardmode.attachments_text(files))
            if changes:
                self.log(f"card: checklist changed: {'; '.join(changes)}")
                self.transcript.add("Card checklist changed", "\n".join(changes))
                parts.append("The card's checklist was changed (it's part of the requirements):\n"
                             + "\n".join(f"- {c}" for c in changes))
            person = bool(fresh or changes)   # comments and checklist edits come from people
            if person:
                self._human_spoke()
                self.human_prompts_count += max(1, len(fresh))
            if parts:
                self._parked_since = None
                said = "\n\n---\n\n".join(parts)
                queue = self.panes[ADVERSARY].queue
                queued = next((i for i, (_, r) in enumerate(queue) if r in ("resume", "init")), None)
                if queued is not None:
                    # Not delivered yet (the run is just starting): fold it into the first message, so the
                    # supervisor starts with it rather than reading "no new instructions" and acting on that.
                    body, r = queue[queued]
                    if not person:
                        body += "\n\n" + said   # only files: the card's, part of the task
                    elif self.card_followup and "already reported Done" not in body:
                        body += ("\n\nThis card was already reported Done, and a person has commented on it since; "
                                 "that is why the run was resumed. Your reply this turn is posted on the card as "
                                 "your answer — it does NOT go to the worker, which stays idle. If they asked a "
                                 "question, answer it (check the code or your history first). If they want changes "
                                 "or a revision, give the worker the work with message_worker (the card moves back "
                                 "to In Progress), verify it as usual, and call report_done again to merge it. "
                                 f"Their comment (it comes from the people you work for):\n\n{said}")
                    else:
                        body += ("\n\nA person has since commented on the card (this is why the run was resumed; "
                                 f"it comes from the people you work for, so act on it):\n\n{said}")
                    queue[queued] = (body, r)
                elif person:
                    if self._worker_idle():
                        said += ("\n\n(Your reply to this is posted on the card for them; it does not reach the "
                                 "worker, which is " + ("on hold" if self.held else "idle") + ". To give the worker "
                                 "work, use message_worker.)")
                    self.enqueue(ADVERSARY, said, "human_comment")
                else:
                    self.enqueue(ADVERSARY, said + "\n\n(Files only, no comment. Your reply isn't sent to the "
                                 "worker; use message_worker if it needs them.)", "human_comment")
            if fresh and was_open:
                await asyncio.to_thread(self.card.move, fourga.IN_PROGRESS)
        except fourga.BoardError as e:
            self.log(f"card: {e}")
        except Exception as e:   # never let the board take the relay down
            self.log(f"card: unexpected error: {e!r}")
        finally:
            self._card_busy = False
        if self.card and not self.finished:
            try:
                await asyncio.to_thread(self._update_status, now)
            except Exception as e:   # the status is a nicety: never let it disturb the run
                self.log(f"card: couldn't update the status comment: {e}")
        self._check_park(now)

    def _progress(self) -> tuple[str, str] | None:
        """The live comment's body, and a key that changes only when it does (times aside): each
        agent's latest words — and, for one that's working, its current action — the one working first.
        None while neither is working: the comment is then left as it last stood."""
        sides = []
        for role, name in ((ADVERSARY, "Supervisor"), (WORKER, "Worker")):
            pane = self.panes[role]
            log = self._worker_log() if role == WORKER else self._role_log(role)
            text, action, when = latest_progress(log, text_chars=LIVE_TEXT) if log else ("", "", None)
            if pane.busy:
                head = f"**{name}** ● working"
            elif pane.idle_at:
                head = f"**{name}** · idle since {datetime.fromtimestamp(pane.idle_at):%H:%M}"
            else:
                head = f"**{name}** · idle"
            lines = [head]
            if text:
                lines.append("\n".join("> " + l if l.strip() else ">" for l in text.splitlines()))
            if pane.busy and action:
                ago = f" ({self._dur(int(time.time() - when))} ago)" if when else ""
                lines.append(f"Now: {action}{ago}")
            if text or (pane.busy and action):   # (an agent that hasn't said or done anything yet: nothing to show)
                sides.append((not pane.busy, "\n\n".join(lines), f"{role}:{pane.busy}:{text}:{action if pane.busy else ''}"))
        if not any(not idle for idle, _, _ in sides):
            return None
        sides.sort(key=lambda x: x[0])   # (stable) whoever is working first
        return "\n\n---\n\n".join(body for _, body, _ in sides), "|".join(k for _, _, k in sides)

    def _update_status(self, now: float) -> None:
        progress = self._progress()
        if progress is None:
            return
        body, key = progress
        if key == self._status_key and now - self._status_at < STATUS_REFRESH:
            return
        self._status_key, self._status_at = key, now
        self.card.set_status(f"**Live** · updated {datetime.now():%H:%M:%S}\n\n{body}")

    def _check_park(self, now: float) -> None:
        """A managed run with nothing to do but wait on people closes, freeing its pool slot; the
        manager resumes it when someone comments on the card. That's one waiting on feedback (an open
        request, or it couldn't complete) idle for PARK_AFTER, or a Done card's follow-up whose
        question has been answered, idle for DONE_IDLE."""
        if not (self.cfg.managed and self.card and self.request_stop):
            return
        idle = all(not p.busy and not p.queue and not p.direct for p in self.panes.values()) and not self.perms
        wait = PARK_AFTER if self.card.open else DONE_IDLE if self.card_followup else None
        if not (idle and wait is not None):
            self._parked_since = None
            return
        if self._parked_since is None:
            self._parked_since = now
        elif now - self._parked_since >= wait:
            if self.card_followup and not self.card.open:
                # Only a question, now answered: the card goes back to Done.
                try:
                    self.card.move(fourga.DONE)
                except fourga.BoardError as e:
                    self.log(f"couldn't move the card back to Done: {e}")
                self.card.followup = False
            self.log("closing: " + ("waiting on people with nothing to do" if self.card.open else
                                    "the follow-up on the Done card is answered") +
                     "; the manager resumes the run when someone comments on the card")
            self.transcript.add("Parked", "Closed while idle; resumed on the card's next comment.")
            self.card.set_live(False)   # closed on purpose: nothing to pick back up until someone comments
            self.request_stop()

    def _card_working(self) -> None:
        """Work is going to the worker on a Done card's follow-up: the card is back In Progress."""
        if not self.card_followup or not self.card:
            return
        self.card_followup = False
        self.card.followup = False   # real work now: it ends with report_done, like any card
        self.card.save()
        try:
            self.card.move(fourga.IN_PROGRESS)
            self.log("follow-up became work: the card stays In Progress until report_done")
        except fourga.BoardError as e:
            self.log(f"couldn't move the card back to In Progress: {e}")

    def _role_log(self, role: str) -> Path | None:
        """A role's session JSONL: its live hook path, else the recorded-id fallback."""
        tp = self.panes[role].transcript_path
        if tp and Path(tp).exists():
            return Path(tp)
        path = sessions.session_log(self.cfg, role)
        return path if path and path.exists() else None

    def _worker_log(self) -> Path | None:
        return self._role_log(WORKER)

    def _read_worker(self, args: dict) -> dict:
        """Show the adversary the last N responses and tool calls of the worker, or of one of the
        subagents it spawned when `agent` is given (read-only)."""
        try:
            count = int(args.get("count", 10))
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(count, 50))
        path = self._worker_log()
        if not path:
            return {"text": "The worker's session log isn't available yet (it may not have started a turn). "
                            "Its turn-end messages still reach you as usual.", "error": True}
        agent = (args.get("agent") or "").strip()
        if agent:
            return self._read_subagent(path, agent, count)
        body = recent_activity(path, count)
        if not body:
            return {"text": "The worker's session log has no responses or tool calls yet.", "error": True}
        self.peeks += 1
        self.log(f"adversary read the worker's last {count}")
        return {"text": f"Worker's last {count} responses and tool calls (oldest first):\n\n{body}"
                        f"{self._subagents_note(path)}"}

    def _read_subagent(self, worker_log: Path, agent: str, count: int) -> dict:
        found = find_subagent_log(worker_log, agent)
        if not found:
            avail = subagent_logs(worker_log)
            if not avail:
                return {"text": f"No subagent '{agent}': this worker hasn't spawned any subagents.", "error": True}
            lst = ", ".join(f"{m.get('agentType', 'agent')} ({m.get('agent_id')})" for _, m in avail[:12])
            return {"text": f"No subagent matches '{agent}'. Available: {lst}.", "error": True}
        sub_path, meta = found
        label = meta.get("agentType") or "subagent"
        body = recent_activity(sub_path, count, skip_sidechain=False, speaker=label)   # all sidechain
        if not body:
            return {"text": f"Subagent {label} ({meta.get('agent_id')}) has no responses or tool calls yet.",
                    "error": True}
        self.peeks += 1
        self.log(f"adversary read subagent {meta.get('agent_id')} last {count}")
        # If the supervisor named a type (not the exact id) and several subagents share it, it got
        # the newest — say so, or it reads the wrong one and doesn't know.
        ambiguous = ""
        if agent != meta.get("agent_id"):
            same = sum(1 for _, m in subagent_logs(worker_log) if m.get("agentType") == meta.get("agentType"))
            if same > 1:
                ambiguous = f"\n(Newest of {same} '{label}' subagents; pass an agent id to read a different one.)"
        desc = f" — {self._short(meta['description'], 100)}" if meta.get("description") else ""
        return {"text": f"Subagent {label} ({meta.get('agent_id')}){desc}{ambiguous}\nLast {count} responses "
                        f"and tool calls (oldest first):\n\n{body}"}

    def _list_agents(self, include_old: bool = False) -> dict:
        """The worker and its subagents, with the ids read_worker(agent=...) takes (read-only)."""
        w = self.panes[WORKER]
        state = "busy (in a turn)" if w.busy else ("held" if self.held else "idle")
        lines = [f"- **worker** (main) — {state}; read with read_worker()"]
        path = self._worker_log()
        subs = subagent_logs(path) if path else []
        cutoff = self._started_wall - 60   # this session, as in _subagents_note
        now, older = time.time(), 0
        for f, m in subs:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff and not include_old:
                older += 1
                continue
            status = "finished" if subagent_finished(f) else "running"
            lines.append(f"- **{m.get('agentType', 'agent')}** — agent id `{m.get('agent_id')}`, "
                         f"{m.get('requestShape') or '?'}, {status}, last activity {self._dur(now - mtime)} ago"
                         + (f": {self._short(m['description'], 100)}" if m.get("description") else "")
                         + f" — read with read_worker(agent=\"{m.get('agent_id')}\")")
        if len(lines) == 1:
            lines.append("- no subagents spawned" + (" this session" if not include_old else ""))
        if older:
            lines.append(f"({older} more from earlier sessions of this run; list_agents(all=true) shows them.)")
        self.log("adversary listed agents")
        return {"text": "Agents on this run (newest subagent first):\n" + "\n".join(lines)}

    def _subagents_note(self, worker_log: Path) -> str:
        """A short list of the subagents spawned in THIS session, appended to a plain worker read so
        the supervisor knows they exist — their own steps aren't in the worker's own log. Scoped to
        this session by file mtime: agent-*.jsonl files persist across resume, and an old run's
        subagents are not this one's."""
        cutoff = self._started_wall - 60   # a minute's grace for clock skew
        recent = []
        for f, m in subagent_logs(worker_log):
            try:
                if f.stat().st_mtime >= cutoff:
                    recent.append(m)
            except OSError:
                continue
        if not recent:
            return ""
        lines = [f"- {m.get('agentType', 'agent')} (agent id `{m.get('agent_id')}`, {m.get('requestShape', '?')})"
                 + (f": {self._short(m['description'], 80)}" if m.get("description") else "")
                 + f" — read with read_worker(agent=\"{m.get('agent_id')}\")"
                 for m in recent[:8]]
        return ("\n\n── Subagents this worker spawned (their own steps are NOT in the list above) ──\n"
                + "\n".join(lines))

    def reopen(self, why: str) -> None:
        """Bring a finished run back to life so the worker can be started on new work.

        finish() is not final: the human can return with a new task. Clears the finished
        flag (the report stays on disk as the record of that round) so message_worker and
        the relay work again."""
        if not self.finished:
            return
        self.log(f"run reopened ({why}); was {self.finished}")
        self.transcript.add("Run reopened", why)
        self.finished, self.summary = None, ""
        self.idle_since = None

    def finish(self, outcome: str, summary: str):
        if self.finished:
            return os.path.join(self.cfg.run_dir, "report.md")
        self.finished, self.summary = outcome, summary
        for req in list(self.perms.values()):
            self._resolve(req, {}, f"request #{req.id} released to the human (run finished)")
        for pane in self.panes.values():
            pane.queue.clear()
        self.transcript.add(f"Run finished: {outcome}", summary)
        path = write_report(self.cfg.run_dir, self.cfg.repo, self._baseline_label(), outcome, summary,
                            self.stats(), self.changes_text())
        self.log(f"FINISHED: {outcome}. Report: {path}")
        self.save_state(final=True)
        return path

    def _baseline_label(self) -> str:
        if self.cfg.base_sha:
            return f"git commit `{self.cfg.base_sha}`"
        return f"folder snapshot `{self.cfg.snapshot_dir}` (no git)"

    # ── run metrics / end-of-run stats ──────────────────────────────────
    def _elapsed(self) -> int:
        return max(0, int(time.monotonic() - self._started))

    def _runtime_total(self) -> int:
        return self._prior_runtime + self._elapsed()

    @staticmethod
    def _dur(seconds: int) -> str:
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m"
        return f"{s}s"

    def _session_tokens(self) -> dict:
        """Tokens *written* (model output) this session per pane, plus a combined total. Output
        is the meaningful "work produced" figure; input is dominated by repeated cache reads, so
        it would run to tens of millions and swamp the readout. Memoised for one shutdown render."""
        if self._tok_cache is None:
            out = {"total": 0}
            for role in (WORKER, ADVERSARY):
                path = self._role_log(role)
                out[role] = token_usage(path)["out"] if path else 0
                out["total"] += out[role]
            self._tok_cache = out
        return self._tok_cache

    def _diff_shortstat(self) -> list[tuple[str, int, int, int, str]] | None:
        """What changed since the run started, as (area, files, insertions, deletions, note) per
        area, or None if unknown. Memoised for one shutdown render."""
        if self._diff_cache is not _UNSET:
            return self._diff_cache
        try:
            self._diff_cache = self._compute_diff_shortstat()
        except (OSError, ValueError, subprocess.SubprocessError):
            self._diff_cache = None
        return self._diff_cache

    @staticmethod
    def _git_stats(cwd: str, base: str) -> tuple[int, int, int]:
        """(files, insertions, deletions) in a git repo since `base`, committed or not. git diff
        leaves out untracked files, which are most of what a worker adds, so they're counted from
        `ls-files --others` (ignored files stay out) with their lines as insertions."""
        git = lambda *a: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout
        out = git("diff", "--shortstat", base)
        f, a, d = (re.search(rf"(\d+) {w}", out) for w in ("files? changed", "insertion", "deletion"))
        untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
        added = sum(len(snapshot.text_lines(Path(cwd) / p) or []) for p in untracked)
        return ((int(f.group(1)) if f else 0) + len(untracked),
                (int(a.group(1)) if a else 0) + added, int(d.group(1)) if d else 0)

    def _compute_diff_shortstat(self) -> list[tuple[str, int, int, int, str]] | None:
        cfg = self.cfg
        if cfg.base_sha:
            return [("", *self._git_stats(cfg.repo, cfg.base_sha), "")]
        if not cfg.snapshot_dir:
            return None
        # A folder without git: each inner repo whose start was recorded is counted with git, and
        # everything else (other inner repos, loose files) from the snapshot.
        changes = snapshot.changes(cfg.repo, cfg.snapshot_dir)
        areas = []
        rest = dict(changes)
        for sub in cfg.git_subdirs:
            mine = {k: [p for p in v if p.startswith(sub + "/")] for k, v in rest.items()}
            base = cfg.git_subdir_bases.get(sub)
            if base:
                areas.append((sub, *self._git_stats(str(Path(cfg.repo) / sub), base), ""))
            elif any(mine.values()):
                areas.append((sub, sum(map(len, mine.values())),
                              *snapshot.line_stats(cfg.repo, cfg.snapshot_dir, mine), "incl. ignored files"))
            rest = {k: [p for p in v if not p.startswith(sub + "/")] for k, v in rest.items()}
        n = sum(map(len, rest.values()))
        if n or not areas:
            tops = Counter(p.split("/")[0] + ("/" if "/" in p else "") for v in rest.values() for p in v)
            note = ", ".join(f"{t} {c:,}" for t, c in tops.most_common(3)) if areas and n else ""
            areas.append(("rest of folder" if areas else "", n,
                          *snapshot.line_stats(cfg.repo, cfg.snapshot_dir, rest), note))
        return areas

    @staticmethod
    def _diff_fmt(files: int, ins: int, dels: int) -> str:
        return f"{files:,} file{'' if files == 1 else 's'}, +{ins:,} −{dels:,}"

    def _diff_text(self) -> str | None:
        areas = self._diff_shortstat()
        if not areas:
            return None
        return self._diff_fmt(*(sum(a[i] for a in areas) for i in (1, 2, 3)))

    def _diff_breakdown(self) -> list[str]:
        """One line per area when the change spans more than one (inner repos, the rest)."""
        areas = self._diff_shortstat() or []
        if len(areas) < 2:
            return []
        w = max(len(a[0]) for a in areas)
        return [f"{name:<{w}}  {self._diff_fmt(f, i, d)}" + (f"  ({note})" if note else "")
                for name, f, i, d, note in areas]

    def _metric_rows(self) -> list[tuple[str, int, int]]:
        """Each run metric as (label, this-session value, lifetime total). Session is the delta
        against the counters at launch, so it is 0-based even on a resumed run."""
        b = self._base
        def pair(current: int, key: str) -> tuple[int, int]:
            return current - b.get(key, 0), current
        return [
            ("Exchanges (relayed to worker)", *pair(self.total_exchanges, "total_exchanges")),
            ("Worker turns", *pair(self.worker_turns, "worker_turns")),
            ("Supervisor turns", *pair(self.adversary_turns, "adversary_turns")),
            ("Permission requests", self.next_perm - b.get("next_perm", 1), self.next_perm - 1),
            ("  · approved", *pair(self.approvals, "approvals")),
            ("  · denied", *pair(self.denials, "denials")),
            ("Holds (worker parked)", *pair(self.holds, "holds")),
            ("Nudges (message_worker)", *pair(self.nudges, "nudges")),
            ("Peeks (read_worker)", *pair(self.peeks, "peeks")),
            ("Stalls detected", *pair(self.stalls, "stalls")),
            ("Human interjections", *pair(self.human_prompts_count, "human_prompts_count")),
        ]

    def stats_lines(self, state: str | None = None) -> list[str]:
        """The end-of-run stats block, as plain lines for the bridge pane. Session and total
        columns on a resumed run; a single column on a first run (they are the same)."""
        rows = self._metric_rows()
        resumed = self.is_resume
        label_w = max(len(r[0]) for r in rows)
        bar = "─" * (label_w + 24)
        try:
            started = sessions.run_started(self.cfg.run_dir).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            started = Path(self.cfg.run_dir).name
        state = state or self.finished or ("PAUSED" if self.paused else "running")
        lines = [bar, f"  adversary run · {state}",
                 f"  task: {self._short(self.cfg.task, label_w + 14)}"]
        if resumed:
            lines.append(f"  started {started} · total {self._dur(self._runtime_total())} "
                         f"· this session {self._dur(self._elapsed())}")
        else:
            lines.append(f"  started {started} · ran {self._dur(self._elapsed())}")
        lines.append(bar)
        if resumed:
            lines.append(f"  {'':<{label_w}}  {'session':>8}  {'total':>8}")
            lines += [f"  {label:<{label_w}}  {s:>8}  {t:>8}" for label, s, t in rows]
        else:
            lines += [f"  {label:<{label_w}}  {t:>8}" for label, _, t in rows]
        lines.append(bar)
        decided = self.approvals + self.denials
        if decided:
            lines.append(f"  approval rate: {round(100 * self.approvals / decided)}% of {decided} decided")
        tok = self._session_tokens()
        if tok["total"]:
            line = (f"  tokens written: worker {tok[WORKER]:,} · supervisor {tok[ADVERSARY]:,} "
                    f"· total {tok['total']:,}")
            if resumed and self._prior_tokens:
                line += f"  (run total {self._prior_tokens + tok['total']:,})"
            lines.append(line)
        diff = self._diff_text()
        if diff:
            lines.append(f"  diff vs start: {diff}")
            lines += [f"    {line}" for line in self._diff_breakdown()]
            lines.append(bar)
        return lines

    def print_stats(self, state: str | None = None) -> None:
        for line in self.stats_lines(state):
            self.log_fn(line)

    def print_status(self) -> None:
        """For the `s` key: the live status line, then the full stats block. The token and diff
        caches are only for the one-shot close render, so clear them to show current figures."""
        self.log(self.status())
        self._tok_cache, self._diff_cache = None, _UNSET
        for line in self.stats_lines():
            self.log_fn(line)

    def save_stats_file(self, state: str | None = None) -> list[str]:
        """Write the stats block to stats.txt (the launcher reprints it after tmux closes) and
        return the lines, so the caller can also print them to the bridge pane."""
        lines = self.stats_lines(state)
        try:
            (Path(self.cfg.run_dir) / "stats.txt").write_text("\n".join(lines) + "\n")
        except OSError as e:
            self.log(f"could not write stats.txt: {e}")
        return lines

    def stats(self) -> dict:
        """The run metrics as a dict for report.md: lifetime totals, with this session in
        parentheses on a resumed run."""
        def val(s: int, t: int) -> str:
            return f"{t} (this session {s})" if self.is_resume and s != t else f"{t}"
        d = {label.lstrip(" ·"): val(s, t) for label, s, t in self._metric_rows()}
        d["Runtime"] = (f"{self._dur(self._runtime_total())} (this session {self._dur(self._elapsed())})"
                        if self.is_resume else self._dur(self._elapsed()))
        decided = self.approvals + self.denials
        if decided:
            d["Approval rate"] = f"{round(100 * self.approvals / decided)}%"
        tok = self._session_tokens()
        if tok["total"]:
            run = f"; run total {self._prior_tokens + tok['total']:,}" if self.is_resume and self._prior_tokens else ""
            d["Tokens written (this session)"] = (f"worker {tok[WORKER]:,}, supervisor {tok[ADVERSARY]:,}, "
                                                  f"total {tok['total']:,}{run}")
        diff = self._diff_text()
        if diff:
            d["Diff vs start"] = diff + "".join(f"; {' '.join(b.split())}" for b in self._diff_breakdown())
        d["Run dir"] = f"`{self.cfg.run_dir}`"
        return d

    # ── stall watchdog ──────────────────────────────────────────────────
    def check_stall(self, now: float | None = None) -> dict | None:
        """Log (once per episode) when both agents sit idle with the run unfinished.

        Neither side is working and nothing is on its way to either, so nobody will
        move the run forward: almost always a relay failure. Called every second.
        """
        now = time.monotonic() if now is None else now
        w, a = self.panes[WORKER], self.panes[ADVERSARY]
        if self.finished or self.paused or self.held or not self.cfg.stall_after:
            self.idle_since = None
            return None
        # A turn can end without a Stop hook (Esc interrupt), leaving `busy` stuck, so the
        # screen counts too: Claude Code shows "esc to interrupt" only while it works.
        screen = {p.role: self.screen_idle(p) for p in (w, a) if p.busy}
        if any(p.busy and not screen[p.role] for p in (w, a)) or w.waiting_on or a.waiting_on:
            self.idle_since = None
            return None
        if self.idle_since is None:
            self.idle_since = now
            return None
        idle = now - self.idle_since
        if idle < self.cfg.stall_after or self.idle_since < 0:
            return None
        self.idle_since = -1.0  # logged; re-armed once either side works again
        self.stalls += 1
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "run_dir": self.cfg.run_dir,
            "repo": self.cfg.repo,
            "idle_seconds": round(idle),
            "status": self.status(),
            "pending_perms": sorted(self.perms),
            **{f"{p.role}_{k}": v for p in (w, a) for k, v in (
                ("bridge_thinks_busy", p.busy),
                ("turn_reason", repr(p.reason)),
                ("last_reason", repr(p.last_reason)),
                ("queued", [self._short(body) for body, _ in p.queue]),
                ("direct", [self._short(t) for t in p.direct]),
                ("expecting_submit", p.expecting_submit),
                ("last_text", p.last_text[-2000:]),
            )},
        }
        self.log(f"STALL: both agents idle for {round(idle)}s with the run unfinished "
                 f"(worker last turn: {record['worker_last_reason']}, adversary last turn: "
                 f"{record['adversary_last_reason']})")
        for role in screen:
            self.log(f"STALL: {role} looks idle on screen but its turn never reported Stop (interrupted?); "
                     "treating it as idle")
            self.panes[role].busy = False   # so what's queued for it can go in
        a = self.panes[ADVERSARY]
        waiting_on_people = bool(self.card and (self.card.open or self.card_followup))   # parks instead
        if self.stall_nudges < STALL_NUDGES and a.started and not a.queue and not waiting_on_people:
            # Nobody else will move the run on: wake the supervisor (its reply goes to the worker).
            self.stall_nudges += 1
            self.enqueue(ADVERSARY, f"The bridge sees you and the worker both idle for {round(idle)}s with the run "
                         "unfinished, and nothing on its way to either of you, so the run has stopped moving. A "
                         "message may have been lost. Check the worker (read_worker), then give it its next step "
                         "(your reply is forwarded to it), call hold if it's rightly waiting on something, or "
                         "finish if the work is done.", "resume")
        else:
            for role in screen:
                self.schedule_flush(role, delay=0)
        self.transcript.add(f"STALL: both agents idle for {round(idle)}s",
                            f"```json\n{json.dumps(record, indent=2)}\n```")
        line = json.dumps(record) + "\n"
        for path in (os.path.join(self.cfg.run_dir, "stalls.jsonl"),
                     os.path.join(os.path.dirname(os.path.dirname(self.cfg.run_dir)), "stalls.jsonl")):
            try:
                with open(path, "a") as f:
                    f.write(line)
            except OSError as e:
                self.log(f"could not write {path}: {e}")
        return record

    def _tmux_focus(self, pane: Pane) -> None:
        try:
            tmux.tmux("select-window", "-t", pane.target)
            tmux.tmux("select-pane", "-t", pane.target)
        except subprocess.CalledProcessError as e:
            self.log(f"could not focus the {pane.role} pane: {e.stderr.strip() if e.stderr else e}")

    # ── one-time help popups ────────────────────────────────────────────
    def project_data(self) -> dict:
        try:
            return json.loads(self.project_file.read_text())
        except (OSError, ValueError):
            return {}

    def _mark_shown(self, key: str) -> None:
        data = self.project_data()
        data.setdefault("shown", {})[key] = datetime.now().isoformat(timespec="seconds")
        try:
            self.project_file.parent.mkdir(parents=True, exist_ok=True)
            self.project_file.write_text(json.dumps(data, indent=2))
        except OSError as e:
            self.log(f"could not save {self.project_file}: {e}")

    def check_popups(self) -> None:
        """Called every second. Once per project: explain the layout once the adversary has
        loaded, and point the human to the adversary pane the first time they select the worker."""
        if (self._popups_unsupported or (self._popup_task and not self._popup_task.done())
                or not self.panes[ADVERSARY].started):
            return
        shown = self.project_data().get("shown", {})
        if "intro" not in shown:
            key = "intro"
        elif "worker_tip" not in shown and self.focused(self.panes[WORKER]):
            key = "worker_tip"
        else:
            return
        self._popup_task = asyncio.get_running_loop().create_task(self._show_popup(key))

    async def _show_popup(self, key: str) -> None:
        title, role = POPUPS[key]
        if await self.popup(self.panes[role], title, (HELP_DIR / f"{key}.txt").read_text()):
            self._mark_shown(key)

    def _tmux_focused(self, pane: Pane) -> bool:
        """The pane is the active one in the active window, and someone is attached."""
        try:
            out = tmux.tmux("display", "-p", "-t", pane.target, "#{pane_active}#{window_active}#{session_attached}")
        except subprocess.CalledProcessError:
            return False
        return out[:2] == "11" and out[2:] not in ("", "0")

    async def _tmux_popup(self, pane: Pane, title: str, text: str) -> bool:
        """Show `text` in a popup on a client viewing the pane's session; True once it was shown
        and closed. False (try again later) when nobody is attached. If tmux rejects the popup
        itself (an older tmux without some option), popups are disabled for the session so the
        one-time help isn't retried every second."""
        try:
            session = tmux.tmux("display", "-p", "-t", pane.target, "#{session_name}")
            clients = tmux.tmux("list-clients", "-t", session, "-F",
                                "#{client_name} #{client_width} #{client_height}").splitlines()
        except subprocess.CalledProcessError:
            return False
        if not clients:
            return False
        client, width, height = clients[0].rsplit(" ", 2)
        # Title goes in the content: `display-popup -T` (title) and `-b` (border style) need a
        # newer tmux (3.3+) than `-w/-h/-E` do, and spamming failures on 3.2 is worse than a
        # borderless, titleless popup. Keep to the widely supported options.
        content = f"  adversary · {title}\n\n{text}"
        lines = content.splitlines()
        path = Path(self.cfg.run_dir) / f"popup-{title.replace(' ', '-')}.txt"
        path.write_text(content)
        cmd = f"bash -c {shlex.quote(f'cat {shlex.quote(str(path))}; read -rsn1 -p "  Press any key to close "')}"
        proc = await asyncio.create_subprocess_exec(
            "tmux", "display-popup", "-c", client, "-t", pane.target, "-E",
            "-w", str(min(max(map(len, lines)) + 4, int(width))),
            "-h", str(min(len(lines) + 4, int(height))), cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            self._popups_unsupported = True
            self.log(f"could not show the {title} popup ({err.decode().strip() or 'tmux rejected it'}); "
                     "skipping the one-time help popups for this run")
            return False
        return True

    def _tmux_screen_idle(self, pane: Pane) -> bool:
        text = tmux.capture(pane.target)
        working = "esc to interrupt" in text or WORKING_SPINNER.search(text)
        return bool(text.strip()) and not working  # empty: pane gone, unknown

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.log("relay PAUSED (press p to resume)" if self.paused else "relay resumed")
        if not self.paused:
            for role in self.panes:
                self.schedule_flush(role, delay=0)
            self._send_direct()

    # ── socket server ───────────────────────────────────────────────────
    async def handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            if msg.get("type") == "hook":
                reply = {"output": await self.on_hook(msg["role"], msg["event"], msg.get("payload", {}))}
            elif msg.get("type") == "mcp":
                reply = self.on_mcp(msg.get("tool", ""), msg.get("args", {}), msg.get("role") or ADVERSARY)
            else:
                reply = {}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except (ConnectionError, json.JSONDecodeError) as e:
            self.log(f"connection error: {e}")
        finally:
            writer.close()


def dialog_open(screen: str) -> bool:
    """Whether a Claude Code dialog or menu covers the prompt box: its key hints ("Enter to
    continue · Esc to cancel") on one of the last few lines, where the prompt's footer would be."""
    lines = [l for l in screen.splitlines() if l.strip()][-4:]
    return any(DIALOG_HINT.search(l) for l in lines)


def _cursor_on(screen: str, option: str) -> bool:
    """Whether a Claude Code menu's cursor (❯) is on the line holding `option`."""
    return any(option in line and "❯" in line for line in screen.splitlines())


async def _startup_watch(bridge: Bridge) -> None:
    """Get each pane past Claude Code's startup dialogs (folder trust, skip-permissions confirm)."""
    accepted: set[tuple[str, str]] = set()
    for _ in range(120):
        await asyncio.sleep(1)
        waiting = False
        for role, pane in bridge.panes.items():
            if pane.started:
                continue
            waiting = True
            screen = tmux.capture(pane.target)
            for marker, label in (("Yes, I trust this folder", "trust"), ("Yes, I accept", "skip-permissions")):
                if marker in screen and (role, label) not in accepted:
                    accepted.add((role, label))
                    bridge.log(f"{role}: accepting startup dialog ({label})")
                    # Claude Code versions order these options differently ("No, exit" first in some,
                    # "Yes" first with the cursor on it in others): move down only when the cursor (❯)
                    # isn't already on the yes line, or Enter would pick "No, exit" and end the session.
                    if not _cursor_on(screen, marker):
                        tmux.press(pane.target, "Down")
                        await asyncio.sleep(0.2)
                    tmux.press(pane.target, "Enter")
        if not waiting:
            return
    # Still not up after two minutes: something on screen the bridge doesn't know how to answer. A
    # managed run closes, so its pool slot isn't held by a stuck session; the manager retries it.
    stuck = [role for role, pane in bridge.panes.items() if not pane.started]
    if stuck and bridge.cfg.managed and bridge.request_stop:
        bridge.log(f"{' and '.join(stuck)} never came up (a startup dialog?); closing the run so the manager can retry")
        bridge.request_stop()


async def _keyboard(bridge: Bridge, stop: asyncio.Event) -> None:
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    loop = asyncio.get_running_loop()

    def on_key() -> None:
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "p":
            bridge.toggle_pause()
        elif ch == "s":
            bridge.print_status()
        elif ch == "q":
            if not bridge.finished:
                bridge.finish("ABORTED", "Stopped by the human from the bridge pane.")
            stop.set()

    loop.add_reader(fd, on_key)
    try:
        await stop.wait()
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _window_of(pane: str) -> str | None:
    try:
        return tmux.tmux("display-message", "-p", "-t", pane, "#{window_id}") or None
    except subprocess.CalledProcessError:
        return None


def _pane_pid(pane: str) -> int | None:
    try:
        return int(tmux.tmux("display-message", "-p", "-t", pane, "#{pane_pid}"))
    except (subprocess.CalledProcessError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A child of ours that exited but wasn't reaped yet counts as gone.
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


async def stop_processes(pids: list[int], grace: float = QUIT_GRACE, log=print) -> None:
    """SIGTERM each pid, wait up to `grace` seconds for them to exit, SIGKILL any left."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    for pid in pids:
        if _alive(pid):
            log(f"pid {pid} didn't exit within {grace:.0f}s; killing it")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def close_agents(bridge: Bridge, panes: list[str]) -> None:
    """Shut down the Claude Code sessions in `panes` (each pane execs into claude)."""
    pids = [pid for pid in map(_pane_pid, panes) if pid]
    if not pids:
        return
    bridge.log("closing the worker and adversary sessions…")
    await stop_processes(pids, log=bridge.log)


async def serve(run_dir: str, worker_pane: str, adversary_pane: str, resume_file: str | None = None) -> None:
    cfg = RunConfig.load(run_dir)
    os.environ.update(git_safe_env(cfg.safe_dirs()))   # the bridge's own git calls (diff stats)
    resume = Path(resume_file).read_text() if resume_file else None
    bridge = Bridge(cfg, {WORKER: worker_pane, ADVERSARY: adversary_pane}, resume=resume)
    if os.path.exists(cfg.sock):
        os.remove(cfg.sock)
    if bridge.card:
        bridge.card.set_live(True)   # until it closes on purpose; a crash or reboot leaves it set
        # Before the socket exists, so before the agents start (they wait for it): a resume caused by
        # a comment gets that comment into its first message, and a Done card's follow-up is known.
        await bridge.card_tick()
    server = await asyncio.start_unix_server(bridge.handle_conn, path=cfg.sock)
    bridge.log(f"bridge up · run dir {run_dir}")
    bridge.log("keys: p pause/resume relay · s status + stats · q end the run (shows the stats, then closes everything)")
    stop = asyncio.Event()
    # "Asked to close remotely": `resume --kill` sends the tmux session SIGHUP, and a kill from
    # another shell sends SIGTERM. Treat both like q — end the loop cleanly so the stats get
    # recorded and shown — but, unlike q, don't write a finish report (a bare close is an
    # interruption, not a finished run).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (ValueError, RuntimeError, NotImplementedError):
            pass
    watch = asyncio.create_task(_startup_watch(bridge))
    bridge.request_stop = stop.set
    card_task: asyncio.Task | None = None   # the running card_tick, kept referenced
    last_status = ""
    try:
        async with server:
            kb = asyncio.create_task(_keyboard(bridge, stop))
            while not stop.is_set():
                status = bridge.status()
                if status != last_status:
                    bridge.log(status)
                    last_status = status
                bridge.check_stall()
                bridge.check_undelivered()
                bridge.check_agents()
                bridge.check_hold()
                bridge.check_popups()
                if bridge.card and not bridge._card_busy:
                    # In the background: a slow board mustn't hold up the relay's own checks.
                    card_task = asyncio.create_task(bridge.card_tick())
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
            kb.cancel()
    finally:
        watch.cancel()
        if os.path.exists(cfg.sock):
            os.remove(cfg.sock)
    # The loop ends on q or a close signal: record the run's final numbers, close the two Claude
    # Code sessions, and show the stats. They are written to stats.txt first (durable, and what the
    # launcher reprints in the user's own terminal after tmux is gone), then printed to the bridge
    # pane. The worker/adversary panes close on their own as their sessions exit.
    window = _window_of(worker_pane)
    if window:
        # The window-resized hook pins this pane to a few rows; drop it so the stats aren't clipped.
        subprocess.run(["tmux", "set-hook", "-u", "-w", "-t", window, "window-resized"], capture_output=True)
    bridge.save_state(final=True)
    await close_agents(bridge, [p.target for p in bridge.panes.values()])   # an agent started again has a new pane
    lines = bridge.save_stats_file(None if bridge.finished else "closed (interrupted)")
    try:
        for line in lines:
            bridge.log_fn(line)
    except OSError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="adversary bridge (started by `adversary run`)")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--worker-pane", required=True)
    ap.add_argument("--adversary-pane", required=True)
    ap.add_argument("--resume-file", help="resume the run: message for the adversary")
    a = ap.parse_args()
    asyncio.run(serve(a.run_dir, a.worker_pane, a.adversary_pane, a.resume_file))


if __name__ == "__main__":
    main()
