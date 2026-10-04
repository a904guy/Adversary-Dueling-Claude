"""The bridge: relays between the worker and adversary Claude Code panes.

Hooks tell the bridge what happened (a turn ended, a prompt was submitted, a
permission is wanted). The bridge speaks by pasting text into a pane. It never
interprets what the agents say. The only structured signals are the
adversary's MCP tool calls (approve_tool, deny_tool, message_worker, hold, changed_files, finish).
"""

import argparse
import asyncio
import json
import os
import re
import shlex
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

from adversary import snapshot, tmux
from adversary.config import RunConfig
from adversary.transcript import Transcript, last_assistant_text, write_report

WORKER, ADVERSARY = "worker", "adversary"
PASTE_SETTLE = 0.4   # seconds between paste and Enter (plus a little per KB)
SUBMIT_RETRY = 2.5   # seconds to wait for UserPromptSubmit before pressing Enter again
SUBMIT_RETRIES = 3
IDLE_SETTLE = 1.0    # seconds after Stop/SessionStart before pasting
MAX_PERM_REMINDERS = 2
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


@dataclass
class PermRequest:
    id: int
    tool: str
    input: dict
    future: asyncio.Future
    reminders: int = 0


class Bridge:
    def __init__(self, cfg: RunConfig, panes: dict[str, str], deliver=None, log=print, screen_idle=None,
                 resume: str | None = None, focus=None, focused=None, popup=None):
        self.cfg = cfg
        self.panes = {role: Pane(role, target) for role, target in panes.items()}
        self.deliver = deliver or self._tmux_deliver
        self.screen_idle = screen_idle or self._tmux_screen_idle
        self.focus = focus or self._tmux_focus
        self.focused = focused or self._tmux_focused
        self.popup = popup or self._tmux_popup
        self._popup_task: asyncio.Task | None = None
        # Per-project record (which help popups were shown), kept with the runs, keyed by folder.
        project = re.sub(r"[^\w.-]+", "-", cfg.origin or cfg.repo).strip("-")
        self.project_file = Path(cfg.run_dir).parent.parent / "projects" / f"{project}.json"
        self.log_fn = log
        self.transcript = Transcript(cfg.run_dir)
        self.perms: dict[int, PermRequest] = {}
        self.next_perm = 1
        self.exchanges = 0
        self.approvals = 0
        self.denials = 0
        self.paused = False
        self.finished: str | None = None
        self.held: str | None = None           # adversary's hold reason: worker parked, nothing to relay
        self.summary = ""
        self.idle_since: float | None = None   # when both agents last went idle together
        self.stalls = 0
        self._flush_tasks: set[asyncio.Task] = set()

        if resume is None:
            self.enqueue(WORKER, cfg.task, "task")
            self.enqueue(ADVERSARY, self._adversary_brief(), "init")
        else:
            # Both sessions come back with their history; the adversary decides what happens next.
            self._load_state()
            self.transcript.add("Run resumed", resume)
            self.enqueue(ADVERSARY, resume, "resume")

    # ── state kept across resumes ───────────────────────────────────────
    STATE_KEYS = ("exchanges", "approvals", "denials", "stalls", "next_perm")

    def _state_path(self) -> Path:
        return Path(self.cfg.run_dir) / "state.json"

    def save_state(self) -> None:
        try:
            self._state_path().write_text(json.dumps({k: getattr(self, k) for k in self.STATE_KEYS}))
        except OSError as e:
            self.log(f"could not save state: {e}")

    def _load_state(self) -> None:
        try:
            state = json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            # Runs from before state.json: keep permission numbers unique at least.
            text = self.transcript.path.read_text()
            state = {"next_perm": max(map(int, re.findall(r"Permission request #(\d+)", text)), default=0) + 1}
        for k in self.STATE_KEYS:
            if isinstance(state.get(k), int):
                setattr(self, k, state[k])

    # ── text sent to the agents ─────────────────────────────────────────
    def _adversary_brief(self) -> str:
        return (
            "The worker has just been given this task (verbatim):\n\n"
            f"<task>\n{self.cfg.task}\n</task>\n\n"
            f"Repository: {self.cfg.repo}\n{self._baseline_text()}\n"
            f"Worker permissions: {'all tool calls pre-approved (skip-permissions)' if self.cfg.skip_permissions else 'routed to you for approval'}\n\n"
            "Your Bash access is limited to these command prefixes, each run as ONE simple command "
            "(no `&&`, `;`, pipes, redirects, loops or `cd`; you are already in the repo): "
            f"{', '.join(self.cfg.adversary_bash())}. Anything else is denied, which does not mean Bash is "
            "unavailable. Use Read/Grep/Glob to inspect files. You MUST run the project's tests yourself "
            "before calling finish.\n\n"
            "Study the repository now so you are ready to review. Your reply to this message is NOT "
            "forwarded; end your turn with a short 'Ready.' once prepared."
        )

    def _baseline_text(self) -> str:
        if self.cfg.base_sha:
            return (f"Base commit before any work: {self.cfg.base_sha} (see changes with "
                    f"`git diff {self.cfg.base_sha}` and `git status`, or the changed_files tool)")
        return ("This folder is NOT a git repository. Its original contents were snapshotted to "
                f"{self.cfg.snapshot_dir}/files before the worker started. Use the changed_files tool to "
                f"list what changed, and `diff -ru {self.cfg.snapshot_dir}/files <file-or-dir>` (or Read "
                "both copies) to see how. Large files and dependency/build dirs were not copied.")

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
        return (f"[{state}] exchange {self.exchanges}/{self.cfg.max_exchanges} · approvals {self.approvals} · "
                f"denials {self.denials} · pending perms {len(self.perms)} · worker {'busy' if w.busy else 'idle'} · "
                f"adversary {'busy' if a.busy else 'idle'}" + (" · worker on hold" if self.held else ""))

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
        if self.paused or pane.busy or pane.expecting_submit or not pane.queue:
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
        pane.busy, pane.reason, pane.expecting_submit, pane.pending = True, reason, True, (header, body, reason)
        self.log(f"→ {role}: {self._short(body)}")
        await self.deliver(role, header, body)

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
            self.log(f"{role}: no submit seen yet, pressing Enter again")
            tmux.press(pane.target, "Enter")

    @staticmethod
    def _is_notification(prompt: str) -> bool:
        return prompt.lstrip().startswith("<task-notification>")

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
    async def on_hook(self, role: str, event: str, payload: dict) -> dict:
        pane = self.panes[role]
        if event == "SessionStart":
            if payload.get("session_id") and self.cfg.sessions.get(role) != payload["session_id"]:
                self.cfg.sessions[role] = payload["session_id"]  # for `adversary resume`
                self.cfg.save()
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
                pane.human_prompts.append(prompt)
                self.log(f"human typed into {role}: {self._short(prompt)}")
                self.transcript.add(f"Human → {role}", prompt)
            return {}
        if event == "Stop":
            pane.busy = False
            text = last_assistant_text(payload)
            self.on_stop(role, pane.reason, text)
            pane.last_reason, pane.reason = pane.reason, None
            self.schedule_flush(role)
            if role == WORKER:
                self._send_direct()
            return {}
        if event == "PermissionRequest" and role == WORKER:
            return await self.on_permission(payload)
        return {}

    def on_stop(self, role: str, reason, text: str) -> None:
        self.panes[role].last_text = text
        self._on_stop(role, reason, text)
        self.save_state()

    def _on_stop(self, role: str, reason, text: str) -> None:
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
        if reason not in ("worker_msg", "resume") or self.finished or self.held:
            return  # on hold: the worker stays idle and this reply goes nowhere
        if self._blank(text):
            self.enqueue(ADVERSARY, "Your last turn produced no message for the worker. Reply to the worker "
                         "(your reply is forwarded verbatim), call hold to leave it idle, or call finish.",
                         "worker_msg")
            return
        if self.exchanges >= self.cfg.max_exchanges:
            self.finish("INCOMPLETE", f"Hit the exchange cap ({self.cfg.max_exchanges}) before the adversary "
                        f"called finish. Last adversary message:\n\n{text}")
            return
        self.exchanges += 1
        self.enqueue(WORKER, text, "adversary_msg")

    def _send_direct(self) -> None:
        """Deliver message_worker texts now, even mid-turn: Claude Code queues a prompt typed
        while it works and folds it into the running turn. Held while a permission request
        is pending (a dialog may own the keyboard) or another delivery awaits its submit."""
        pane = self.panes[WORKER]
        if not pane.direct or self.paused or self.finished or self.perms or pane.expecting_submit:
            return
        body = "\n\n---\n\n".join(pane.direct)
        pane.direct.clear()
        if not pane.busy:
            self.enqueue(WORKER, body, "adversary_msg", priority=True)
            return
        header = self.header(WORKER, "direct")
        pane.expecting_submit, pane.pending = True, (header, body, "direct")  # its submit fires right away
        self.log(f"→ worker (mid-turn): {self._short(body)}")
        task = asyncio.get_running_loop().create_task(self.deliver(WORKER, header, body))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    def _requeue_pending(self, pane: Pane) -> bool:
        """Put back a paste that a person's prompt beat to the submit. Returns whether the
        pane was already mid-turn before that paste."""
        _, body, reason = pane.pending or (None, None, None)
        pane.expecting_submit, pane.pending = False, None
        if reason is None:
            return pane.reason is not None
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
                return {"text": f"Approved #{req.id}. The worker's tool call will run."}
            reason = args.get("reason") or "Denied by supervisor."
            self.denials += 1
            self._resolve(req, self._decision(False, reason), f"denied #{req.id}: {reason}")
            return {"text": f"Denied #{req.id}. The worker sees your reason."}
        if tool == "message_worker":
            text = (args.get("message") or "").strip()
            if not text:
                return {"text": "message is empty.", "error": True}
            if self.finished:
                return {"text": "The run has finished; relaying has stopped.", "error": True}
            self.transcript.add("Adversary → worker (message_worker)", text)
            self.release_hold("the adversary messaged the worker")
            self.panes[WORKER].direct.append(text)
            self._send_direct()
            if self.panes[WORKER].busy:
                return {"text": "Sent. The worker is mid-turn, so it is delivered into its current turn "
                                "(after any pending permission request is decided)."}
            return {"text": "Sent. The worker was idle, so this starts its next turn."}
        if tool == "hold":
            if self.finished:
                return {"text": "The run has finished; relaying has stopped.", "error": True}
            w = self.panes[WORKER]
            if w.busy or w.queue or w.direct:
                return {"text": "The worker is busy or has a message on its way, so its next turn-end will reach "
                                "you anyway. Call hold once it is idle.", "error": True}
            reason = (args.get("reason") or "").strip() or "no reason given"
            self.held = reason
            self.log(f"worker on hold: {reason}")
            self.transcript.add("Worker on hold", reason)
            return {"text": "The worker is on hold. Your reply this turn is not forwarded, and nothing is sent "
                            "to it until you call message_worker (or the human types to it). End your turn."}
        if tool == "changed_files":
            return {"text": self.changes_text()}
        if tool == "finish":
            outcome = "COMPLETE" if args.get("outcome") == "complete" else "CANNOT_COMPLETE"
            path = self.finish(outcome, args.get("summary", ""))
            return {"text": f"Run finished ({outcome}). Relaying has stopped; report written to {path}. "
                            "You do not need to message the worker again."}
        return {"text": f"Unknown tool {tool}", "error": True}

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
        self.save_state()
        return path

    def _baseline_label(self) -> str:
        if self.cfg.base_sha:
            return f"git commit `{self.cfg.base_sha}`"
        return f"folder snapshot `{self.cfg.snapshot_dir}` (no git)"

    def stats(self) -> dict:
        return {"Exchanges": self.exchanges, "Stalls": self.stalls, "Permission approvals": self.approvals,
                "Permission denials": self.denials, "Run dir": f"`{self.cfg.run_dir}`"}

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
        if any(p.busy and not screen[p.role] for p in (w, a)):
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
        if (self._popup_task and not self._popup_task.done()) or not self.panes[ADVERSARY].started:
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
        and closed. False (try again later) when nobody is attached."""
        try:
            session = tmux.tmux("display", "-p", "-t", pane.target, "#{session_name}")
            clients = tmux.tmux("list-clients", "-t", session, "-F",
                                "#{client_name} #{client_width} #{client_height}").splitlines()
        except subprocess.CalledProcessError:
            return False
        if not clients:
            return False
        client, width, height = clients[0].rsplit(" ", 2)
        lines = text.splitlines()
        path = Path(self.cfg.run_dir) / f"popup-{title.replace(' ', '-')}.txt"
        path.write_text(text)
        cmd = f"bash -c {shlex.quote(f'cat {shlex.quote(str(path))}; read -rsn1 -p "  Press any key to close "')}"
        proc = await asyncio.create_subprocess_exec(
            "tmux", "display-popup", "-c", client, "-t", pane.target, "-E", "-b", "rounded",
            "-T", f" adversary · {title} ", "-w", str(min(max(map(len, lines)) + 4, int(width))),
            "-h", str(min(len(lines) + 4, int(height))), cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            self.log(f"could not show the {title} popup: {err.decode().strip()}")
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
            if not pane.busy or pane.reason is not None:
                continue  # session started
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


async def serve(run_dir: str, worker_pane: str, adversary_pane: str, resume_file: str | None = None) -> None:
    cfg = RunConfig.load(run_dir)
    resume = Path(resume_file).read_text() if resume_file else None
    bridge = Bridge(cfg, {WORKER: worker_pane, ADVERSARY: adversary_pane}, resume=resume)
    if os.path.exists(cfg.sock):
        os.remove(cfg.sock)
    server = await asyncio.start_unix_server(bridge.handle_conn, path=cfg.sock)
    bridge.log(f"bridge up · run dir {run_dir}")
    bridge.log("keys: p pause/resume relay · s status · q quit bridge")
    stop = asyncio.Event()
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
