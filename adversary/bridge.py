"""The bridge: relays between the worker and adversary Claude Code panes.

Hooks tell the bridge what happened (a turn ended, a prompt was submitted, a
permission is wanted). The bridge speaks by pasting text into a pane. It never
interprets what the agents say. The only structured signals are the
adversary's MCP tool calls (approve_tool, deny_tool, message_worker, hold, read_worker,
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from adversary import sessions, snapshot, tmux
from adversary.config import RunConfig, draft_path
from adversary.transcript import (Transcript, last_assistant_text, recent_activity, token_usage,
                                   turn_had_tool_use, write_report)

WORKER, ADVERSARY = "worker", "adversary"
PASTE_SETTLE = 0.4   # seconds between paste and Enter (plus a little per KB)
SUBMIT_RETRY = 2.5   # seconds to wait for UserPromptSubmit before pressing Enter again
SUBMIT_RETRIES = 3
IDLE_SETTLE = 1.0    # seconds after Stop/SessionStart before pasting
DRAFT_RECHECK = 1.0  # seconds between checks while a person has a draft in the pane
DELIVERY_RETRY = 5.0  # seconds before retrying a delivery tmux refused
QUIT_GRACE = 10.0    # seconds the agents get to exit on SIGTERM before SIGKILL
MAX_PERM_REMINDERS = 2
QUIET_HOLD_AT = 2    # consecutive no-op worker turns (after an adversary reply) before an auto-hold
_UNSET = object()    # sentinel: a cached value not computed yet (None is a valid result)
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
    human_prompts: list = field(default_factory=list)  # typed by the human since last relay
    direct: list = field(default_factory=list)  # message_worker texts waiting to be typed in mid-turn
    last_text: str = ""            # final message of its last turn
    transcript_path: str | None = None  # this session's JSONL, from live hook payloads
    waiting_on: str | None = None  # why a delivery waits for the person: "a draft", "shell mode", "copy mode"
    wait_retry: bool = False       # a recheck is scheduled


@dataclass
class PermRequest:
    id: int
    tool: str
    input: dict
    future: asyncio.Future
    reminders: int = 0


class Bridge:
    def __init__(self, cfg: RunConfig, panes: dict[str, str], deliver=None, log=print, screen_idle=None,
                 resume: str | None = None, focus=None, focused=None, popup=None, has_draft=None,
                 pane_mode=None):
        self.cfg = cfg
        self.panes = {role: Pane(role, target) for role, target in panes.items()}
        self.deliver = deliver or self._tmux_deliver
        self.screen_idle = screen_idle or self._tmux_screen_idle
        self.focus = focus or self._tmux_focus
        self.focused = focused or self._tmux_focused
        self.popup = popup or self._tmux_popup
        self.has_draft = has_draft or self._draft_file_says
        self.pane_mode = pane_mode or self._tmux_pane_mode
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
        self._prior_runtime = 0    # runtime from earlier sessions of this run (seconds)
        self._prior_tokens = 0     # tokens written in earlier sessions of this run
        self._session_tokens_cache = 0  # this session's tokens written, refreshed only on a final save
        self._tok_cache: dict | None = None   # memoised token read for one shutdown render
        self._diff_cache = _UNSET  # memoised diff read for one shutdown render
        self.is_resume = resume is not None
        self.paused = False
        self.finished: str | None = None
        self.held: str | None = None           # adversary's hold reason: worker parked, nothing to relay
        self.summary = ""
        self.idle_since: float | None = None   # when both agents last went idle together
        self.stalls = 0
        self.plan_sent = False                  # the worker has been given its first instructions
        self._flush_tasks: set[asyncio.Task] = set()

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
        return snapshot.format_changes(snapshot.changes(repo, self.cfg.snapshot_dir))

    @staticmethod
    def _perm_text(req: PermRequest) -> str:
        detail = json.dumps(req.input, indent=2)
        if len(detail) > 4000:
            detail = detail[:4000] + "\n… (truncated)"
        return (
            f"Worker requests permission #{req.id}: {req.tool}\n\n```json\n{detail}\n```\n\n"
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
                f"adversary {'busy' if a.busy else 'idle'}" + (" · worker on hold" if self.held else "")
                + "".join(f" · waiting on you in the {p.role} pane ({p.waiting_on})" for p in (w, a) if p.waiting_on))

    # ── delivery ────────────────────────────────────────────────────────
    def enqueue(self, role: str, text: str, reason, priority: bool = False) -> None:
        pane = self.panes[role]
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
        }.get(kind, "Message from the adversary bridge (pasted below):")

    async def _tmux_deliver(self, role: str, header: str, body: str) -> None:
        pane = self.panes[role]
        tmux.type_text(pane.target, header + " ")
        tmux.paste(pane.target, body, buffer=f"adv-{role}")
        await asyncio.sleep(PASTE_SETTLE + len(body) / 20000)
        tmux.press(pane.target, "Enter")
        for _ in range(SUBMIT_RETRIES):
            await asyncio.sleep(SUBMIT_RETRY)
            if not pane.expecting_submit:
                return
            # Enter again only while the box still starts with our message: never submit
            # something a person typed, or a shell command.
            report = self._draft_report(pane)
            if report is not None and not str(report.get("head", "")).startswith(header[:40]):
                self.log(f"{role}: no submit seen, and the prompt box no longer holds the bridge's message; "
                         "not pressing Enter")
                return
            if self.pane_mode(pane):
                self.log(f"{role}: no submit seen, and the pane is in {self.pane_mode(pane)}; not pressing Enter")
                return
            self.log(f"{role}: no submit seen yet, pressing Enter again")
            tmux.press(pane.target, "Enter")

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
            pane.started = True
            self.log(f"{role} session {payload.get('source') or 'started'}")
            if role == ADVERSARY:
                self.focus(pane)  # the human talks to the supervisor, so it gets the keyboard
            pane.busy = False
            self.schedule_flush(role)
            return {}
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            in_turn, pane.busy = pane.busy, True
            if role == WORKER:
                self.release_hold("the worker started a turn")
            if pane.expecting_submit and self._is_delivery(pane.pending, prompt):
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
            # The session that just ran a turn is the live one; a stub never gets here. This keeps
            # the recorded id pointing at the real session even when a resume forks to a new id.
            self._record_session(role, payload)
            pane.busy = False
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
            self.transcript.add("Worker", text or "(no text)")
            self.log(f"worker ended turn: {self._short(text or '(no text)')}")
            # Any permission still pending is moot now: the worker's turn is over.
            for req in list(self.perms.values()):
                self._resolve(req, {}, f"request #{req.id} expired (worker turn ended)")
            if self.finished:
                return
            if self._blank(text):
                text = "(the worker ended its turn without a message)"
            # A turn the adversary's own reply started, in which the worker did nothing (no tool
            # call), means the worker had nothing to do with that reply. Repeated, the two are only
            # echoing while the worker waits on a background subagent. Park it rather than relay
            # another no-op that the adversary will just answer, restarting the loop.
            if reason == "adversary_msg" and not had_tool_use:
                self.quiet_rounds += 1
                if self.quiet_rounds >= QUIET_HOLD_AT and not self.held:
                    self._quiet_hold()
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
            return
        if self.exchanges >= self.cfg.max_exchanges:
            self._cap_reached(text)
            return
        self.exchanges += 1
        self.total_exchanges += 1
        self.enqueue(WORKER, text, "adversary_msg")

    def _cap_reached(self, text: str) -> None:
        """The two have gone back and forth max_exchanges times without the human: a loop
        guard. The worker is put on hold, not the run ended; the human picks it back up."""
        cap = self.cfg.max_exchanges
        self.holds += 1
        self.held = f"{cap} exchanges since the human last spoke"
        self.log(f"exchange cap: {cap} round trips without you; worker on hold, the adversary's reply not forwarded")
        self.transcript.add(f"Exchange cap ({cap}): worker on hold", f"Not forwarded to the worker:\n\n{text}")
        self.enqueue(ADVERSARY, f"Your last reply was not forwarded: you and the worker have gone back and forth {cap} "
                     "times without the human, so the worker is on hold in case the two of you are looping. Nothing "
                     "is lost. The count starts again when the human types to either of you; to carry on before "
                     "that, send the worker what it needs with message_worker. Your reply to this note isn't "
                     "forwarded.", "cap_note")

    def _quiet_hold(self) -> None:
        """The worker has ended turn after turn doing nothing in reply to the adversary: it is
        waiting on something that will bring it back on its own (a background subagent or task),
        and the two are only echoing. Park the worker and tell the adversary to stop replying."""
        self.holds += 1
        self.held = "worker idle, waiting on a background subagent or task"
        self.quiet_rounds = 0
        self.log("quiet-loop hold: the worker did nothing after the adversary's replies; worker on hold")
        self.transcript.add("Quiet-loop hold: worker on hold",
                            "The worker ended consecutive turns with no action after the adversary's replies.")
        self.enqueue(ADVERSARY,
                     "The worker has ended several turns in a row without doing anything after your replies. It "
                     "has nothing for you right now — it is waiting on a background subagent or task, which will "
                     "bring it back to you on its own when it finishes. The worker is now on hold, so stop "
                     "replying: don't send it anything and don't check in, or you'll just restart the loop. End "
                     "your turn. It will return to you when there is real work; if the human gives you something "
                     "new in the meantime, use message_worker then. Your reply to this note isn't forwarded.",
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
        if pane.busy and self._waiting_on_person(pane):
            return  # (an idle worker's message goes through the queue, which checks too)
        body = "\n\n---\n\n".join(pane.direct)
        pane.direct.clear()
        if not pane.busy:
            self.enqueue(WORKER, body, "adversary_msg", priority=True)
            return
        header = self.header(WORKER, "direct")
        pane.expecting_submit, pane.pending = True, (header, body, "direct")  # its submit fires right away
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
        for line in reversed(tmux.capture(pane.target).splitlines()):
            if line.startswith("❯"):
                return None
            if line.startswith(("!\u00a0", "! ")):
                return "shell mode"
        return None

    def _waiting_on_person(self, pane: Pane) -> bool:
        """True while typing into the pane would land in something of the person's: a message
        half written, shell mode or copy mode. Nothing is typed until that's done.
        Rechecked every DRAFT_RECHECK seconds."""
        why = "a draft" if self.has_draft(pane) else self.pane_mode(pane)
        if why is None:
            if pane.waiting_on:
                self.log(f"{pane.role}: no longer waiting ({pane.waiting_on} done); delivering")
                pane.waiting_on = None
            return False
        if pane.waiting_on != why:
            pane.waiting_on = why
            self.log({"a draft": f"{pane.role}: you have a message drafted there; waiting until it's sent or cleared",
                      "shell mode": f"{pane.role}: the prompt is in shell mode (!); waiting until it's back to normal",
                      "copy mode": f"{pane.role}: the pane is in copy mode; waiting until you leave it"}[why])
        if not pane.wait_retry:
            pane.wait_retry = True
            asyncio.get_running_loop().call_later(DRAFT_RECHECK, self._recheck_wait, pane.role)
        return True

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

    def release_hold(self, why: str) -> None:
        if self.held:
            self.held = None
            self.log(f"hold released: {why}")
            self.transcript.add(f"Hold released: {why}")

    async def on_permission(self, payload: dict) -> dict:
        if self.finished:
            return {}  # fall back to the on-screen prompt for the human
        loop = asyncio.get_running_loop()
        req = PermRequest(self.next_perm, payload.get("tool_name", "?"), payload.get("tool_input", {}),
                          loop.create_future())
        self.next_perm += 1
        self.perms[req.id] = req
        self.quiet_rounds = 0  # a permission request means the worker is doing real work
        self.log(f"permission #{req.id}: {req.tool} {self._short(json.dumps(req.input), 70)}")
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
        self._send_direct()

    # ── MCP tool calls from the adversary ───────────────────────────────
    def on_mcp(self, tool: str, args: dict) -> dict:
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
            reopened = bool(self.finished)
            if reopened:
                self.reopen("the adversary sent the worker new work with message_worker")
            self.nudges += 1
            self.transcript.add("Adversary → worker (message_worker)", text)
            self.plan_sent = True  # the worker has its instructions; don't also auto-forward an init reply
            self.quiet_rounds = 0  # the adversary gave the worker real work; start the loop count fresh
            self.release_hold("the adversary messaged the worker")
            self.panes[WORKER].direct.append(text)
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
            self.holds += 1
            self.held = reason
            self.log(f"worker on hold: {reason}")
            self.transcript.add("Worker on hold", reason)
            return {"text": "The worker is on hold. Your reply this turn is not forwarded, and nothing is sent "
                            "to it until you call message_worker (or the human types to it). End your turn."}
        if tool == "read_worker":
            return self._read_worker(args)
        if tool == "changed_files":
            return {"text": self.changes_text()}
        if tool == "finish":
            outcome = "COMPLETE" if args.get("outcome") == "complete" else "CANNOT_COMPLETE"
            path = self.finish(outcome, args.get("summary", ""))
            return {"text": f"Run finished ({outcome}). Relaying has stopped; report written to {path}. "
                            "If the human later gives you new work, call message_worker: that picks the "
                            "run back up and starts the worker again."}
        return {"text": f"Unknown tool {tool}", "error": True}

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
        """Show the adversary the worker's last N responses and tool calls (read-only)."""
        try:
            count = int(args.get("count", 10))
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(count, 50))
        path = self._worker_log()
        if not path:
            return {"text": "The worker's session log isn't available yet (it may not have started a turn). "
                            "Its turn-end messages still reach you as usual.", "error": True}
        body = recent_activity(path, count)
        if not body:
            return {"text": "The worker's session log has no responses or tool calls yet.", "error": True}
        self.peeks += 1
        self.log(f"adversary read the worker's last {count}")
        return {"text": f"Worker's last {count} responses and tool calls (oldest first):\n\n{body}"}

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

    def _diff_shortstat(self) -> tuple[int, int, int] | None:
        """(files changed, insertions, deletions) since the run started, or None if unknown.
        Memoised for one shutdown render."""
        if self._diff_cache is not _UNSET:
            return self._diff_cache
        self._diff_cache = self._compute_diff_shortstat()
        return self._diff_cache

    def _compute_diff_shortstat(self) -> tuple[int, int, int] | None:
        try:
            if self.cfg.base_sha:
                git = lambda *a: subprocess.run(["git", *a], cwd=self.cfg.repo,
                                                capture_output=True, text=True).stdout
                out = git("diff", "--shortstat", self.cfg.base_sha)
                f = re.search(r"(\d+) files? changed", out)
                a = re.search(r"(\d+) insertion", out)
                d = re.search(r"(\d+) deletion", out)
                # git diff ignores untracked files, so count them from status (new files a worker
                # created but didn't commit are most of the point of the number).
                untracked = sum(1 for line in git("status", "--porcelain").splitlines()
                                if line.startswith("??"))
                return (untracked + (int(f.group(1)) if f else 0),
                        int(a.group(1)) if a else 0, int(d.group(1)) if d else 0)
            if self.cfg.snapshot_dir:
                c = snapshot.changes(self.cfg.repo, self.cfg.snapshot_dir)
                return (sum(len(v) for v in c.values()), 0, 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return None

    def _diff_text(self) -> str | None:
        d = self._diff_shortstat()
        if d is None:
            return None
        return f"{d[0]} file{'' if d[0] == 1 else 's'}, +{d[1]} −{d[2]}"

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
            lines.append(bar)
        return lines

    def print_stats(self, state: str | None = None) -> None:
        for line in self.stats_lines(state):
            self.log_fn(line)

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
            d["Diff vs start"] = diff
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
            self.log(f"STALL: {role} looks idle on screen but its turn never reported Stop (interrupted?)")
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
                reply = self.on_mcp(msg.get("tool", ""), msg.get("args", {}))
            else:
                reply = {}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except (ConnectionError, json.JSONDecodeError) as e:
            self.log(f"connection error: {e}")
        finally:
            writer.close()


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
                    tmux.press(pane.target, "Down")
                    await asyncio.sleep(0.2)
                    tmux.press(pane.target, "Enter")
        if not waiting:
            return


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
            bridge.log(bridge.status())
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
    resume = Path(resume_file).read_text() if resume_file else None
    bridge = Bridge(cfg, {WORKER: worker_pane, ADVERSARY: adversary_pane}, resume=resume)
    if os.path.exists(cfg.sock):
        os.remove(cfg.sock)
    server = await asyncio.start_unix_server(bridge.handle_conn, path=cfg.sock)
    bridge.log(f"bridge up · run dir {run_dir}")
    bridge.log("keys: p pause/resume relay · s status · q end the run (shows the stats, then closes everything)")
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
                bridge.check_popups()
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
    # Code sessions, and print the stats. The bridge pane is left up — its launch script waits for
    # a keypress — so the readout can be read; the worker/adversary panes close on their own as
    # their sessions exit, leaving this pane full-size for it.
    window = _window_of(worker_pane)
    if window:
        # The window-resized hook pins this pane to a few rows; drop it so the stats aren't clipped.
        subprocess.run(["tmux", "set-hook", "-u", "-w", "-t", window, "window-resized"], capture_output=True)
    bridge.save_state(final=True)
    await close_agents(bridge, [worker_pane, adversary_pane])
    try:
        bridge.print_stats(None if bridge.finished else "closed (interrupted)")
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
