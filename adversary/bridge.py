"""The bridge: relays between the worker and adversary Claude Code panes.

Hooks tell the bridge what happened (a turn ended, a prompt was submitted, a
permission is wanted). The bridge speaks by pasting text into a pane. It never
interprets what the agents say. The only structured signals are the
adversary's MCP tool calls (approve_tool, deny_tool, changed_files, finish).
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import termios
import tty
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from adversary import snapshot, tmux
from adversary.config import RunConfig
from adversary.transcript import Transcript, last_assistant_text, write_report

WORKER, ADVERSARY = "worker", "adversary"
PASTE_SETTLE = 0.4   # seconds between paste and Enter (plus a little per KB)
SUBMIT_RETRY = 2.5   # seconds to wait for UserPromptSubmit before pressing Enter again
SUBMIT_RETRIES = 3
IDLE_SETTLE = 1.0    # seconds after Stop/SessionStart before pasting
MAX_PERM_REMINDERS = 2


@dataclass
class Pane:
    role: str
    target: str
    busy: bool = True              # until SessionStart says the UI is up
    queue: deque = field(default_factory=deque)   # (body, reason)
    reason: object = None          # why the current turn is running
    expecting_submit: bool = False  # we pasted; the next UserPromptSubmit is ours
    human_prompts: list = field(default_factory=list)  # typed by the human since last relay


@dataclass
class PermRequest:
    id: int
    tool: str
    input: dict
    future: asyncio.Future
    reminders: int = 0


class Bridge:
    def __init__(self, cfg: RunConfig, panes: dict[str, str], deliver=None, log=print):
        self.cfg = cfg
        self.panes = {role: Pane(role, target) for role, target in panes.items()}
        self.deliver = deliver or self._tmux_deliver
        self.log_fn = log
        self.transcript = Transcript(cfg.run_dir)
        self.perms: dict[int, PermRequest] = {}
        self.next_perm = 1
        self.exchanges = 0
        self.approvals = 0
        self.denials = 0
        self.paused = False
        self.finished: str | None = None
        self.summary = ""
        self._flush_tasks: set[asyncio.Task] = set()

        self.enqueue(WORKER, cfg.task, "task")
        self.enqueue(ADVERSARY, self._adversary_brief(), "init")

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
                f"adversary {'busy' if a.busy else 'idle'}")

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
        if self.paused or pane.busy or not pane.queue:
            return
        body, reason = pane.queue.popleft()
        human = self.panes[WORKER].human_prompts
        if role == ADVERSARY and reason == "worker_msg" and human:
            said = "\n\n".join(f"> {p}" for p in human)
            body = ("NOTE: the human typed these instructions directly to the worker. They come from the person "
                    "you stand in for, so they amend the original task and take precedence over it where the two "
                    f"conflict. Review against the task as amended:\n\n{said}\n\n---\n\n{body}")
            human.clear()
        pane.busy, pane.reason, pane.expecting_submit = True, reason, True
        self.log(f"→ {role}: {self._short(body)}")
        await self.deliver(role, self.header(role, reason), body)

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
            "worker_msg": "The worker agent ended its turn with this message (pasted below):",
            "perm": "Permission request from the worker (pasted below). Decide it with your tools:",
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
    def _short(text: str, n: int = 90) -> str:
        one = " ".join(text.split())
        return one if len(one) <= n else one[: n - 1] + "…"

    # ── hook events ─────────────────────────────────────────────────────
    async def on_hook(self, role: str, event: str, payload: dict) -> dict:
        pane = self.panes[role]
        if event == "SessionStart":
            self.log(f"{role} session started")
            pane.busy = False
            self.schedule_flush(role)
            return {}
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            pane.busy = True
            if pane.expecting_submit:
                pane.expecting_submit = False
            else:
                pane.reason = "user"
                pane.human_prompts.append(prompt)
                self.log(f"human typed into {role}: {self._short(prompt)}")
                self.transcript.add(f"Human → {role}", prompt)
            return {}
        if event == "Stop":
            pane.busy = False
            text = last_assistant_text(payload)
            self.on_stop(role, pane.reason, text)
            pane.reason = None
            self.schedule_flush(role)
            return {}
        if event == "PermissionRequest" and role == WORKER:
            return await self.on_permission(payload)
        return {}

    def on_stop(self, role: str, reason, text: str) -> None:
        if role == WORKER:
            self.transcript.add("Worker", text or "(no text)")
            self.log(f"worker ended turn: {self._short(text or '(no text)')}")
            # Any permission still pending is moot now: the worker's turn is over.
            for req in list(self.perms.values()):
                self._resolve(req, {}, f"request #{req.id} expired (worker turn ended)")
            if self.finished:
                return
            self.enqueue(ADVERSARY, text or "(the worker ended its turn without a message)", "worker_msg")
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
        if reason != "worker_msg" or self.finished:
            return
        if not text.strip():
            self.enqueue(ADVERSARY, "Your last turn produced no message for the worker. Reply to the worker "
                         "(your reply is forwarded verbatim), or call finish.", "worker_msg")
            return
        if self.exchanges >= self.cfg.max_exchanges:
            self.finish("INCOMPLETE", f"Hit the exchange cap ({self.cfg.max_exchanges}) before the adversary "
                        f"called finish. Last adversary message:\n\n{text}")
            return
        self.exchanges += 1
        self.enqueue(WORKER, text, "adversary_msg")

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
        return path

    def _baseline_label(self) -> str:
        if self.cfg.base_sha:
            return f"git commit `{self.cfg.base_sha}`"
        return f"folder snapshot `{self.cfg.snapshot_dir}` (no git)"

    def stats(self) -> dict:
        return {"Exchanges": self.exchanges, "Permission approvals": self.approvals,
                "Permission denials": self.denials, "Run dir": f"`{self.cfg.run_dir}`"}

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.log("relay PAUSED (press p to resume)" if self.paused else "relay resumed")
        if not self.paused:
            for role in self.panes:
                self.schedule_flush(role, delay=0)

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


async def serve(run_dir: str, worker_pane: str, adversary_pane: str) -> None:
    cfg = RunConfig.load(run_dir)
    bridge = Bridge(cfg, {WORKER: worker_pane, ADVERSARY: adversary_pane})
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
    a = ap.parse_args()
    asyncio.run(serve(a.run_dir, a.worker_pane, a.adversary_pane))


if __name__ == "__main__":
    main()
