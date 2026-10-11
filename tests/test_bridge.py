import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from adversary import bridge as bridge_mod
from adversary.bridge import ADVERSARY, WORKER, Bridge
from adversary.config import RunConfig


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bridge_mod, "IDLE_SETTLE", 0)
    monkeypatch.setattr(bridge_mod, "DRAFT_RECHECK", 0)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    (r / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-qm", "init"], cwd=r, check=True)
    return r


def make(repo, tmp_path, **kw):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    cfg = RunConfig(task="Do the thing.\nAll of it.", repo=str(repo), run_dir=str(run), sock=str(tmp_path / "s"),
                    base_sha=sha, **kw)
    sent, screen_idle, focused = [], set(), []   # targets of panes that look idle on screen
    popups, has_focus, drafts = [], set(), set()  # popups shown; roles whose pane has focus / a draft
    modes = {}                                     # role -> "shell mode" / "copy mode"

    async def popup(pane, title, text):
        popups.append((pane.role, title, text))
        return True

    async def deliver(role, header, body):
        assert header.strip()
        sent.append((role, body))

    b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=deliver, log=lambda m: None,
               screen_idle=lambda pane: pane.target in screen_idle, focus=lambda pane: focused.append(pane.role),
               focused=lambda pane: pane.role in has_focus, popup=popup, has_draft=lambda pane: pane.role in drafts,
               pane_mode=lambda pane: modes.get(pane.role))
    b.test_drafts, b.test_modes = drafts, modes
    b.project_file = tmp_path / "projects" / "repo.json"
    b.test_popups, b.test_has_focus = popups, has_focus
    b.test_screen_idle = screen_idle
    b.test_focused = focused
    return b, sent


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def start(b):
    await b.on_hook(WORKER, "SessionStart", {})
    await b.on_hook(ADVERSARY, "SessionStart", {})
    await settle()


async def submit(b, role, sent):
    """Simulate Claude Code submitting the last pasted text."""
    text = [t for r, t in sent if r == role][-1]
    await b.on_hook(role, "UserPromptSubmit", {"prompt": text})


async def stop(b, role, text):
    await b.on_hook(role, "Stop", {"last_assistant_message": text})
    await settle()


def transcript_file(tmp_path, name, *, tool_use):
    """A minimal session JSONL for one turn, so turn_had_tool_use can read it."""
    p = tmp_path / f"t-{name}.jsonl"
    content = ([{"type": "tool_use", "name": "Bash", "input": {}}] if tool_use else []) + [{"type": "text", "text": "x"}]
    p.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "the adversary's reply"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}}),
    ]) + "\n")
    return str(p)


async def stop_tx(b, role, text, path):
    """Stop with a transcript_path, so the bridge can see whether the turn made a tool call."""
    await b.on_hook(role, "Stop", {"last_assistant_message": text, "transcript_path": path})
    await settle()


async def test_startup_gives_the_task_to_the_adversary_not_the_worker(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    assert not any(r == WORKER for r, t in sent)  # the worker is not given the task directly
    brief = [t for r, t in sent if r == ADVERSARY][0]
    assert "Do the thing.\nAll of it." in brief and b.cfg.base_sha in brief
    # The adversary ends its init turn with the plan, which is forwarded to the worker.
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Here's the plan: step 1, step 2.")
    assert sent[-1] == (WORKER, "Here's the plan: step 1, step 2.") and b.plan_sent
    assert b.exchanges == 0  # the first hand-off is the task reaching the worker, not a loop round-trip


async def test_startup_plan_sent_via_message_worker_is_not_resent(repo, tmp_path):
    """If the adversary sends the plan with message_worker during its init turn and then ends the
    turn with no text, the bridge must not also forward (or nag for) a plan — the worker gets one."""
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, ADVERSARY, sent)
    b.on_mcp("message_worker", {"message": "Plan: do step 1 then step 2."})
    await settle()
    assert sent[-1] == (WORKER, "Plan: do step 1 then step 2.") and b.plan_sent
    n = len(sent)
    await stop(b, ADVERSARY, "")                  # blank end-of-turn after the tool call
    await settle()
    assert len(sent) == n                         # nothing resent, and no "produced no plan" nag
    assert not b.panes[ADVERSARY].queue


async def test_adversary_can_hold_at_init_when_the_task_is_ambiguous(repo, tmp_path):
    """An ambiguous task: the adversary holds and asks the human at init instead of guessing a
    plan. Its question must not leak to the worker, which stays idle until message_worker."""
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, ADVERSARY, sent)              # adversary picks up the brief
    r = b.on_mcp("hold", {"reason": "task doesn't say which database"})
    assert not r.get("error") and b.held
    await stop(b, ADVERSARY, "Should this use Postgres or SQLite?")
    assert not any(role == WORKER for role, t in sent)   # the question did not reach the worker
    assert not b.plan_sent
    b.on_mcp("message_worker", {"message": "Use SQLite. Plan: step 1, step 2."})
    await settle()
    assert b.held is None and b.plan_sent and sent[-1] == (WORKER, "Use SQLite. Plan: step 1, step 2.")


async def test_first_plan_turn_is_exempt_from_the_quiet_loop(repo, tmp_path):
    """The worker's first turn (reason 'plan') often ends with a text-only ack and no tool call;
    that must not count toward the subagent-wait auto-hold."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                          # worker busy on its first (plan) turn
    await stop_tx(b, WORKER, "Understood, starting now.", transcript_file(tmp_path, "p", tool_use=False))
    assert b.quiet_rounds == 0 and not b.held
    assert sent[-1][0] == ADVERSARY and sent[-1][1].endswith("Understood, starting now.")


async def test_relay_is_verbatim_both_ways(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Shall I proceed?\n\n- yes\n- no")
    assert sent[-1] == (ADVERSARY, "Shall I proceed?\n\n- yes\n- no")
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Yes. Finish *everything*.\nDon't stop.")
    assert sent[-1] == (WORKER, "Yes. Finish *everything*.\nDon't stop.")
    assert b.exchanges == 1


async def test_queues_until_idle(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                          # worker busy on its first turn, adversary idle
    await stop(b, WORKER, "A")                    # forwarded to the idle adversary
    await submit(b, ADVERSARY, sent)              # adversary now busy on A
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "go"})  # worker runs again
    await stop(b, WORKER, "done")                 # ends while the adversary is still busy
    assert not any(r == ADVERSARY and "done" in t for r, t in sent)   # queued, not delivered yet
    await stop(b, ADVERSARY, "ack A")             # adversary goes idle; the queued message follows
    assert sent[-1][0] == ADVERSARY and "done" in sent[-1][1]


async def test_permission_approve_and_deny(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)

    pending = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
                                            {"tool_name": "Bash", "tool_input": {"command": "pytest"}}))
    await settle()
    assert sent[-1][0] == ADVERSARY and "permission #1" in sent[-1][1] and "pytest" in sent[-1][1]
    approved = b.on_mcp("approve_tool", {"request_id": 1})["text"]
    assert approved.startswith("Approved")
    # The decision response tells the adversary to end the turn and not narrate/hold,
    # so it doesn't loop saying "Holding." after every approval.
    assert "end your turn" in approved and "hold" in approved.lower()
    out = await pending
    assert out["hookSpecificOutput"]["decision"] == {"behavior": "allow"}

    # The adversary's text after a permission turn is NOT forwarded.
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "approved it")
    assert not any(r == WORKER and t == "approved it" for r, t in sent)

    pending = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
                                            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}))
    await settle()
    b.on_mcp("deny_tool", {"request_id": 2, "reason": "not in scope"})
    out = await pending
    assert out["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "not in scope"}
    assert (b.approvals, b.denials, b.exchanges) == (1, 1, 0)


async def test_permission_reminder_then_autodeny(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    pending = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest", {"tool_name": "Edit", "tool_input": {}}))
    await settle()
    for _ in range(bridge_mod.MAX_PERM_REMINDERS):
        await submit(b, ADVERSARY, sent)
        await stop(b, ADVERSARY, "hmm")
        assert "still pending" in sent[-1][1]
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "hmm")
    out = await pending
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"


async def test_worker_stop_expires_pending_permission(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    pending = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest", {"tool_name": "Bash", "tool_input": {}}))
    await settle()
    await stop(b, WORKER, "human answered the prompt and I finished")
    assert await pending == {}
    assert "No pending" in b.on_mcp("approve_tool", {"request_id": 1})["text"]


async def test_finish_stops_relay_and_writes_report(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "All done!")
    await submit(b, ADVERSARY, sent)
    b.on_mcp("finish", {"outcome": "complete", "summary": "verified: tests pass"})
    await stop(b, ADVERSARY, "Great work.")
    assert not any(r == WORKER and t == "Great work." for r, t in sent)
    report = (tmp_path / "run" / "report.md").read_text()
    assert "COMPLETE" in report and "verified: tests pass" in report
    n = len(sent)
    await stop(b, WORKER, "anything else?")
    assert len(sent) == n


async def test_message_worker_reopens_a_finished_run(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "All done!")
    await submit(b, ADVERSARY, sent)
    b.on_mcp("finish", {"outcome": "complete", "summary": "done"})
    await stop(b, ADVERSARY, "All verified.")
    assert b.finished
    # Human returns with a new task; the adversary starts the worker again.
    r = b.on_mcp("message_worker", {"message": "New task: add a changelog."})
    await settle()
    assert not b.finished
    assert "live again" in r["text"]
    assert sent[-1] == (WORKER, "New task: add a changelog.")
    # The worker's reply now flows back to the adversary again.
    await submit(b, WORKER, sent)
    await stop(b, WORKER, "Changelog added.")
    assert any(r == ADVERSARY and "Changelog added." in t for r, t in sent)


async def test_human_typing_into_worker_reopens_a_finished_run(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "All done!")
    await submit(b, ADVERSARY, sent)
    b.on_mcp("finish", {"outcome": "complete", "summary": "done"})
    await stop(b, ADVERSARY, "All verified.")
    assert b.finished
    # The human restarts the worker by hand in its own pane.
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "actually, also fix the typo"})
    await stop(b, WORKER, "Fixed the typo.")
    assert not b.finished
    assert any(r == ADVERSARY and "Fixed the typo." in t for r, t in sent)


async def test_human_thanking_adversary_does_not_reopen(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    b.finish("COMPLETE", "done")
    n = len(sent)
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "thanks, that's all"})
    await stop(b, ADVERSARY, "You're welcome.")
    assert b.finished  # a parting word to the adversary leaves the run finished
    assert not any(r == WORKER for r, t in sent[n:])


async def test_exchange_cap(repo, tmp_path):
    b, sent = make(repo, tmp_path, max_exchanges=1)
    await ready(b, sent)
    for i in range(2):
        await stop(b, WORKER, f"w{i}")
        await submit(b, ADVERSARY, sent)
        await stop(b, ADVERSARY, f"a{i}")
        if i == 0:
            await submit(b, WORKER, sent)
    assert (WORKER, "a0") in sent and (WORKER, "a1") not in sent
    assert not b.finished and b.held and b.total_exchanges == 1  # a pause, never the end
    assert "not forwarded" in sent[-1][1] and sent[-1][0] == ADVERSARY
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Understood.")                       # reply to the note goes nowhere
    assert (WORKER, "Understood.") not in sent
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "keep going on item 2"})
    assert b.exchanges == 0                                       # the human spoke
    b.on_mcp("message_worker", {"message": "Carry on with item 2."})
    await settle()
    assert sent[-1] == (WORKER, "Carry on with item 2.") and not b.held


async def test_human_typing_is_detected_and_reply_still_relayed(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "first")
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "human: also add docs"})
    assert b.panes[WORKER].reason == "user"
    await stop(b, WORKER, "added docs")
    # adversary is busy with "first"; once it replies, the queued worker message follows
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "ok")
    msg = [t for r, t in sent if r == ADVERSARY][-1]
    assert msg.endswith("added docs") and "> human: also add docs" in msg
    assert "Human → worker" in (tmp_path / "run" / "transcript.md").read_text()


async def test_pause_holds_delivery(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    b.paused = True
    await start(b)
    assert sent == []
    b.toggle_pause()
    await settle()
    assert len(sent) == 1  # only the adversary's brief is queued at start (the worker gets no task)


async def test_socket_roundtrip_hook_and_mcp(repo, tmp_path):
    """The real hook command and MCP server talk to the bridge over its socket."""
    import os
    import sys
    b, sent = make(repo, tmp_path)
    sock = str(tmp_path / "b.sock")
    server = await asyncio.start_unix_server(b.handle_conn, path=sock)
    async with server:
        env = {**os.environ}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "adversary.hook", sock, "worker", "SessionStart",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
        out, _ = await proc.communicate(json.dumps({"hook_event_name": "SessionStart"}).encode())
        assert json.loads(out) == {}
        assert b.panes[WORKER].busy is False or sent  # session marked started

        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "finish", "arguments": {"outcome": "cannot_complete", "summary": "x"}}},
        ]
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "adversary.mcp_server", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, env={**env, "ADVERSARY_SOCK": sock})
        out, _ = await proc.communicate("".join(json.dumps(m) + "\n" for m in msgs).encode())
        replies = [json.loads(l) for l in out.decode().splitlines()]
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert {t["name"] for t in replies[1]["result"]["tools"]} == {"approve_tool", "deny_tool", "message_worker", "hold", "read_worker", "changed_files", "finish"}
        assert "CANNOT_COMPLETE" in replies[2]["result"]["content"][0]["text"]
        assert b.finished == "CANNOT_COMPLETE"


async def test_wrapped_paste_is_not_mistaken_for_human(repo, tmp_path):
    """Claude Code rewrites long pastes (<pasted_content>); our submit must still be recognised."""
    b, sent = make(repo, tmp_path)
    await start(b)                                # the brief is delivered to the adversary
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "[Pasted text #1 +40 lines]"})
    assert b.panes[ADVERSARY].reason == "init"
    await stop(b, ADVERSARY, "the plan")          # forwarded to the worker as its plan
    await b.on_hook(WORKER, "UserPromptSubmit",
                    {"prompt": "Your supervisor's plan for this task <pasted_content id=\"x\">…</pasted_content>"})
    assert b.panes[WORKER].reason == "plan"


def test_every_delivery_has_a_typed_header():
    for reason in ("task", "plan", "adversary_msg", "init", "worker_msg", ("perm", 3), "other"):
        assert Bridge.header(WORKER, reason).strip()


async def test_queued_worker_messages_are_coalesced_with_human_note(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    b.toggle_pause()
    await stop(b, WORKER, "first done")
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "also add a line"})
    await stop(b, WORKER, "added the line")
    b.toggle_pause()
    await settle()
    to_adv = [t for r, t in sent if r == ADVERSARY][-1]
    assert "first done" in to_adv and "added the line" in to_adv and "also add a line" in to_adv
    assert to_adv.index("first done") < to_adv.index("added the line")
    assert len([t for r, t in sent if r == ADVERSARY]) == 2  # brief + one coalesced message


NOTE = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>"


async def test_mid_turn_notification_keeps_reply_relayed(repo, tmp_path):
    """A background task finishing mid-turn must not turn the adversary's review into a 'user' turn."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Done.")
    await submit(b, ADVERSARY, sent)
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": NOTE})
    await stop(b, ADVERSARY, "Fix items 1-7.")
    assert sent[-1] == (WORKER, "Fix items 1-7.")
    assert not b.panes[ADVERSARY].human_prompts


async def test_idle_notification_continues_previous_turn(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Done.")
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Tests are running, hold on.")
    await submit(b, WORKER, sent)
    await stop(b, WORKER, "Ok.")
    # adversary is now reviewing "Ok."; let it finish, then a notification wakes it while idle
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Still waiting.")
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": NOTE})
    await stop(b, ADVERSARY, "Tests failed: fix test_x.")
    assert "Tests failed: fix test_x." in [t for r, t in sent if r == WORKER] or \
        any(t == "Tests failed: fix test_x." for t, _ in b.panes[WORKER].queue)


async def test_worker_notification_is_not_a_human_amendment(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                    # worker mid-turn on its first (plan) turn
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": NOTE})
    assert not b.panes[WORKER].human_prompts


async def test_human_typing_mid_turn_keeps_reason(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Done.")
    await submit(b, ADVERSARY, sent)
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "also check the docs"})
    await stop(b, ADVERSARY, "Check the docs too.")
    assert sent[-1] == (WORKER, "Check the docs too.")


async def test_message_worker_mid_turn_and_idle(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                    # worker is mid-turn on its first (plan) turn
    r = b.on_mcp("message_worker", {"message": "Also add a --verbose flag."})
    await settle()
    assert not r.get("error") and sent[-1] == (WORKER, "Also add a --verbose flag.")
    assert b.panes[WORKER].busy and b.panes[WORKER].reason == "plan"
    await submit(b, WORKER, sent)           # Claude Code queues it: UserPromptSubmit fires mid-turn
    assert not b.panes[WORKER].human_prompts and b.panes[WORKER].reason == "plan"
    await stop(b, WORKER, "Done, flag added.")
    assert b.panes[ADVERSARY].queue or sent[-1] == (ADVERSARY, "Done, flag added.")
    # idle worker: becomes a normal delivery
    await submit(b, ADVERSARY, sent)
    b.on_mcp("message_worker", {"message": "One more thing."})
    await settle()
    assert sent[-1] == (WORKER, "One more thing.")


async def test_message_worker_waits_for_pending_permission(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                    # worker mid-turn on its first (plan) turn
    perm = asyncio.create_task(b.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    await settle()
    b.on_mcp("message_worker", {"message": "Use the fixtures dir."})
    await settle()
    assert (WORKER, "Use the fixtures dir.") not in sent
    b.on_mcp("approve_tool", {"request_id": 1})
    await settle()
    assert (await perm)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert sent[-1] == (WORKER, "Use the fixtures dir.")


async def test_stall_logged_once_when_both_idle(repo, tmp_path):
    b, sent = make(repo, tmp_path, stall_after=60)
    await ready(b, sent)                                # worker busy on plan, adversary idle
    assert b.check_stall(now=0) is None                 # worker still busy
    b.panes[WORKER].busy = False                        # e.g. a relay was lost: nobody is working
    b.panes[WORKER].last_reason = "plan"
    assert b.check_stall(now=100) is None               # idle clock starts
    assert b.check_stall(now=150) is None
    rec = b.check_stall(now=161)
    assert rec and rec["idle_seconds"] == 61 and rec["adversary_last_text"] == "Plan: do the thing."
    assert b.check_stall(now=500) is None               # once per episode
    lines = (tmp_path / "run" / "stalls.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["worker_last_reason"] == "'plan'"
    assert "STALL" in (tmp_path / "run" / "transcript.md").read_text()
    b.panes[WORKER].busy = True                         # work resumes: re-armed
    assert b.check_stall(now=600) is None
    b.panes[WORKER].busy = False
    b.check_stall(now=700)
    assert b.check_stall(now=761) and b.stalls == 2


async def test_no_stall_when_paused_finished_or_disabled(repo, tmp_path):
    b, sent = make(repo, tmp_path, stall_after=0)
    await start(b)
    b.panes[WORKER].busy = b.panes[ADVERSARY].busy = False
    b.check_stall(now=0)
    assert b.check_stall(now=10_000) is None            # disabled
    b.cfg.stall_after = 60
    b.toggle_pause()
    b.check_stall(now=0)
    assert b.check_stall(now=10_000) is None            # paused
    b.toggle_pause()
    b.finish("COMPLETE", "ok")
    b.check_stall(now=0)
    assert b.check_stall(now=10_000) is None            # finished


async def test_stall_detected_when_turn_ends_without_stop(repo, tmp_path):
    """Esc-interrupting a turn fires no Stop hook: the bridge still thinks the pane is busy."""
    b, sent = make(repo, tmp_path, stall_after=60)
    await ready(b, sent)
    b.check_stall(now=0)
    assert b.check_stall(now=100) is None               # worker busy on screen too
    b.test_screen_idle.add("%1")                         # interrupted: idle prompt, no Stop
    b.check_stall(now=200)
    rec = b.check_stall(now=261)
    assert rec and rec["worker_bridge_thinks_busy"] is True


async def test_session_ids_recorded_and_compaction_keeps_busy(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await b.on_hook(WORKER, "SessionStart", {"session_id": "w-1", "source": "startup"})
    await b.on_hook(ADVERSARY, "SessionStart", {"session_id": "a-1", "source": "startup"})
    await settle()
    assert RunConfig.load(b.cfg.run_dir).sessions == {"worker": "w-1", "adversary": "a-1"}
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "starting"})  # worker now mid-turn
    await b.on_hook(WORKER, "SessionStart", {"session_id": "w-1", "source": "compact"})  # mid-turn
    assert b.panes[WORKER].busy


async def test_a_stub_session_start_does_not_overwrite_a_known_id(repo, tmp_path):
    """On resume we launch with a known id; a throwaway stub session that fires SessionStart
    must not replace it, or the next resume opens an empty session."""
    b, sent = make(repo, tmp_path)
    b.cfg.sessions = {"worker": "real-worker", "adversary": "real-adv"}
    await b.on_hook(WORKER, "SessionStart", {"session_id": "stub", "source": "startup"})
    assert b.cfg.sessions["worker"] == "real-worker"              # the stub didn't win
    # The session that actually runs a turn is recorded (captures a resume that forks the id).
    await b.on_hook(WORKER, "Stop", {"last_assistant_message": "done", "session_id": "forked-worker"})
    assert RunConfig.load(b.cfg.run_dir).sessions["worker"] == "forked-worker"


async def test_resume_asks_adversary_and_forwards_its_reply(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    b.total_exchanges, b.approvals, b.next_perm = 4, 9, 12
    b.save_state()
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None, focus=lambda p: None, pane_mode=lambda p: None,
                screen_idle=lambda p: False, resume="Run resumed. Check and reply.")
    await start(b2)
    assert sent == [(ADVERSARY, "Run resumed. Check and reply.")]   # no task re-sent to the worker
    assert (b2.exchanges, b2.total_exchanges, b2.approvals, b2.next_perm) == (0, 4, 9, 12)  # count restarts
    await submit(b2, ADVERSARY, sent)
    await stop(b2, ADVERSARY, "Fix item 3.")
    assert sent[-1] == (WORKER, "Fix item 3.") and (b2.exchanges, b2.total_exchanges) == (1, 5)


async def test_resume_without_state_keeps_permission_numbers_unique(repo, tmp_path):
    b, _ = make(repo, tmp_path)
    b.transcript.add("Permission request #41: Bash", "x")
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None, focus=lambda p: None, pane_mode=lambda p: None,
                screen_idle=lambda p: False, resume="go")
    assert b2.next_perm == 42


async def ready(b, sent):
    """Reach the steady state where the worker is busy on its first turn and the adversary is idle:
    the adversary picks up the brief and ends its init turn with a plan, which is forwarded to the
    worker and starts it."""
    await start(b)
    await submit(b, ADVERSARY, sent)                  # adversary picks up the supervision brief
    await stop(b, ADVERSARY, "Plan: do the thing.")   # init reply, forwarded to the worker as the plan
    await submit(b, WORKER, sent)                      # worker starts on the plan (reason "plan")


async def test_hold_ends_the_ping_pong(repo, tmp_path):
    b, sent = make(repo, tmp_path, stall_after=60)
    await ready(b, sent)
    await stop(b, WORKER, "Holding. Nothing is running.")
    await submit(b, ADVERSARY, sent)
    r = b.on_mcp("hold", {"reason": "waiting for the human to approve the e2e run"})
    assert not r.get("error") and b.held
    await stop(b, ADVERSARY, "Keep holding.")
    assert not any(r == WORKER and t == "Keep holding." for r, t in sent)
    assert b.exchanges == 0 and not b.panes[ADVERSARY].queue
    b.check_stall(now=0)
    assert b.check_stall(now=10_000) is None              # held is not a stall
    assert "worker on hold" in b.status()
    b.on_mcp("message_worker", {"message": "Run the e2e suite once."})
    await settle()
    assert b.held is None and sent[-1] == (WORKER, "Run the e2e suite once.")


async def test_hold_released_by_human_typing_to_worker(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Done for now.")
    await submit(b, ADVERSARY, sent)
    b.on_mcp("hold", {"reason": "waiting"})
    await stop(b, ADVERSARY, "ok")
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "also add a README"})
    assert b.held is None
    await stop(b, WORKER, "README added.")
    assert sent[-1][0] == ADVERSARY and sent[-1][1].endswith("README added.")


async def test_hold_refused_while_worker_busy(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                                   # worker still on its task
    assert b.on_mcp("hold", {"reason": "x"}).get("error") and b.held is None


async def _quiet_round(b, sent, tmp_path, worker_text, *, tool_use, name):
    """One full round: the adversary replies, the worker runs a turn and ends it."""
    await submit(b, ADVERSARY, sent)                       # adversary picks up the worker's message
    await stop(b, ADVERSARY, f"leaving it be ({name})")    # forwarded to the worker
    await submit(b, WORKER, sent)                          # worker's turn starts (reason adversary_msg)
    await stop_tx(b, WORKER, worker_text, transcript_file(tmp_path, name, tool_use=tool_use))


async def test_quiet_subagent_loop_auto_holds_the_worker(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "awaiting dd-X")                 # worker idle, waiting on a subagent
    await _quiet_round(b, sent, tmp_path, "still awaiting dd-X (1)", tool_use=False, name="r1")
    assert not b.held and b.quiet_rounds == 1
    assert (ADVERSARY, "still awaiting dd-X (1)") in sent  # first no-op was still relayed
    await _quiet_round(b, sent, tmp_path, "still awaiting dd-X (2)", tool_use=False, name="r2")
    assert b.held                                          # second no-op trips the auto-hold
    assert (ADVERSARY, "still awaiting dd-X (2)") not in sent   # not relayed; the loop is cut
    assert sent[-1][0] == ADVERSARY and "on hold" in sent[-1][1]


async def test_worker_doing_real_work_after_a_reply_never_auto_holds(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "working")
    for i in range(3):                                     # three rounds, each with a real tool call
        await _quiet_round(b, sent, tmp_path, f"did work {i}", tool_use=True, name=f"w{i}")
    assert not b.held and b.quiet_rounds == 0
    assert (ADVERSARY, "did work 2") in sent               # every real turn relays normally


async def test_permission_resets_the_quiet_counter(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "awaiting dd-X")
    await _quiet_round(b, sent, tmp_path, "still awaiting (1)", tool_use=False, name="q1")
    assert b.quiet_rounds == 1
    pending = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest", {"tool_name": "Bash", "tool_input": {}}))
    await settle()
    assert b.quiet_rounds == 0                             # a permission request is real work
    b.on_mcp("approve_tool", {"request_id": b.next_perm - 1})
    await pending


async def test_auto_hold_is_released_when_a_subagent_brings_the_worker_back(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "awaiting dd-X")
    await _quiet_round(b, sent, tmp_path, "still (1)", tool_use=False, name="a1")
    await _quiet_round(b, sent, tmp_path, "still (2)", tool_use=False, name="a2")
    assert b.held
    await submit(b, ADVERSARY, sent)                       # adversary reads the stop-replying note
    await stop(b, ADVERSARY, "Standing down.")            # its reply to the note goes nowhere
    assert not any(r == WORKER and t == "Standing down." for r, t in sent)
    teammate = ('Another Claude session sent a message:\n<teammate-message teammate_id="dd-X">\n'
                '{"type":"idle_notification","from":"dd-X"}\n</teammate-message>')
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": teammate})
    assert b.held is None                                  # the worker is back on its own
    await stop(b, WORKER, "Subagent found three issues.")
    assert sent[-1][0] == ADVERSARY and sent[-1][1].endswith("Subagent found three issues.")


async def test_teammate_message_is_a_notification_not_human(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "done")
    b.exchanges = 3
    teammate = ('Another Claude session sent a message:\n<teammate-message teammate_id="dd-X">\n'
                '{"type":"idle_notification","from":"dd-X"}\n</teammate-message>')
    assert Bridge._is_notification(teammate)
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": teammate})
    assert not b.panes[WORKER].human_prompts and b.exchanges == 3


async def test_invisible_replies_count_as_empty(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "\u200b\n")
    assert sent[-1] == (ADVERSARY, "(the worker ended its turn without a message)")
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, " \u200b ")
    assert sent[-1][0] == ADVERSARY and "call hold" in sent[-1][1]
    assert b.exchanges == 0


async def test_typed_prompt_beating_a_delivery_is_the_humans_turn(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Finished step 1.")             # pasted into the adversary pane, not yet submitted
    assert b.panes[ADVERSARY].expecting_submit
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "why is npm using 10GB?"})
    a = b.panes[ADVERSARY]
    assert a.reason == "user" and not a.expecting_submit
    assert "Human → adversary" in (tmp_path / "run" / "transcript.md").read_text()
    await stop(b, ADVERSARY, "It's reserved address space, not real memory.")
    assert not any(r == WORKER and "reserved address space" in t for r, t in sent)
    assert sent[-1] == (ADVERSARY, "Finished step 1.")    # the worker's message is delivered again
    await submit(b, ADVERSARY, sent)
    assert a.reason == "worker_msg"


async def test_typed_prompt_beating_a_mid_turn_message_requeues_it(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                                   # worker mid-turn on its first (plan) turn
    b.on_mcp("message_worker", {"message": "Use the fixtures dir."})
    await settle()
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "human: hurry up"})
    w = b.panes[WORKER]
    assert w.reason == "plan" and w.direct == ["Use the fixtures dir."]
    b._send_direct()
    await settle()
    assert sent[-1] == (WORKER, "Use the fixtures dir.")


async def test_adversary_pane_focused_once_loaded(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await b.on_hook(WORKER, "SessionStart", {})
    assert b.test_focused == []
    await b.on_hook(ADVERSARY, "SessionStart", {})
    await b.on_hook(ADVERSARY, "SessionStart", {"source": "compact"})   # compaction: no focus change
    assert b.test_focused == [ADVERSARY]


async def test_help_popups_once_per_project(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    b.check_popups()
    await settle()
    assert b.test_popups == []                             # adversary not loaded yet
    await start(b)
    b.check_popups()
    await settle()
    assert [(r, t) for r, t, _ in b.test_popups] == [(ADVERSARY, "how this works")]
    assert "RIGHT" in b.test_popups[0][2]
    b.check_popups()
    await settle()
    assert len(b.test_popups) == 1                         # intro shown once; worker not focused
    b.test_has_focus.add(WORKER)
    b.check_popups()
    await settle()
    assert b.test_popups[-1][:2] == (WORKER, "the worker pane")
    b.check_popups()
    await settle()
    assert len(b.test_popups) == 2
    assert set(json.loads(b.project_file.read_text())["shown"]) == {"intro", "worker_tip"}
    b2, _ = make(repo, tmp_path)                           # a later run in the same project
    b2.project_file = b.project_file
    b2.test_has_focus.add(WORKER)
    await start(b2)
    b2.check_popups()
    await settle()
    assert b2.test_popups == []


async def test_popup_retried_until_shown(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    calls = []

    async def no_client(pane, title, text):
        calls.append(title)
        return False                                       # nobody attached yet
    b.popup = no_client
    await start(b)
    for _ in range(2):
        b.check_popups()
        await settle()
    assert calls == ["how this works", "how this works"] and not b.project_file.exists()


async def test_delivery_waits_for_the_persons_draft(repo, tmp_path):
    b, sent = make(repo, tmp_path, stall_after=60)
    await ready(b, sent)
    b.test_drafts.add(ADVERSARY)                           # someone is typing in the adversary pane
    await stop(b, WORKER, "Step 1 done.")
    for _ in range(3):
        await asyncio.sleep(0.01)                          # rechecks keep finding the draft
    assert not any(r == ADVERSARY and t == "Step 1 done." for r, t in sent)
    assert b.panes[ADVERSARY].waiting_on == "a draft" and "waiting on you in the adversary pane (a draft)" in b.status()
    b.panes[WORKER].busy = b.panes[ADVERSARY].busy = False
    b.check_stall(now=0)
    assert b.check_stall(now=10_000) is None               # someone typing is not a stall
    b.test_drafts.clear()                                  # sent or cleared
    await asyncio.sleep(0.01)
    await settle()
    assert sent[-1] == (ADVERSARY, "Step 1 done.") and not b.panes[ADVERSARY].waiting_on


async def test_mid_turn_message_waits_for_draft_in_worker_pane(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                                   # worker mid-turn on its first (plan) turn
    b.test_drafts.add(WORKER)
    b.on_mcp("message_worker", {"message": "Use the fixtures dir."})
    await asyncio.sleep(0.01)
    assert (WORKER, "Use the fixtures dir.") not in sent
    b.test_drafts.clear()
    await asyncio.sleep(0.01)
    await settle()
    assert sent[-1] == (WORKER, "Use the fixtures dir.")


def test_draft_file_is_read_from_the_run_dir(repo, tmp_path):
    b, _ = make(repo, tmp_path)
    pane = b.panes[WORKER]
    assert b._draft_file_says(pane) is False               # no report: no draft
    (tmp_path / "run" / "draft-worker.json").write_text('{"draft": true, "at": 1}')
    assert b._draft_file_says(pane) is True
    (tmp_path / "run" / "draft-worker.json").write_text("not json")
    assert b._draft_file_says(pane) is False


def test_launch_scripts_load_the_draft_mod(repo, tmp_path):
    from adversary.config import write_launch_files
    b, _ = make(repo, tmp_path)
    (tmp_path / "run" / "draft-adversary.json").write_text('{"draft": true}')
    scripts = write_launch_files(b.cfg)
    for role, script in scripts.items():
        text = script.read_text()
        assert "--plugin-dir" in text and "draftmod" in text
        assert f"ADVERSARY_DRAFT_FILE={tmp_path / 'run' / f'draft-{role}.json'}" in text
    assert not (tmp_path / "run" / "draft-adversary.json").exists()   # stale report removed


async def test_shell_command_turn_is_not_routed_as_the_delivery(repo, tmp_path):
    """The 14:17 / 14:19 incident: the bridge's paste ran as a `!` shell command (no
    UserPromptSubmit), and the adversary's reply to the person was relayed to the worker."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "URLs fixed and staged.")
    assert sent[-1] == (ADVERSARY, "URLs fixed and staged.") and b.panes[ADVERSARY].expecting_submit
    # no UserPromptSubmit: the box was in shell mode and ran it; the model replies to the person
    await stop(b, ADVERSARY, "The worker's report got pasted into your shell.")
    assert not any(r == WORKER and "pasted into your shell" in t for r, t in sent)
    assert b.exchanges == 0 and b.panes[ADVERSARY].last_reason is None
    assert sent[-1] == (ADVERSARY, "URLs fixed and staged.")       # delivered again, as a prompt this time
    await submit(b, ADVERSARY, sent)
    assert b.panes[ADVERSARY].reason == "worker_msg"
    await stop(b, ADVERSARY, "Good. Wait for the human to push.")
    assert sent[-1] == (WORKER, "Good. Wait for the human to push.")


async def test_waits_while_pane_is_in_shell_or_copy_mode(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    b.test_modes[ADVERSARY] = "shell mode"
    await stop(b, WORKER, "done")
    await asyncio.sleep(0.01)
    assert not any(r == ADVERSARY and t == "done" for r, t in sent)
    assert "(shell mode)" in b.status()
    b.test_modes[ADVERSARY] = "copy mode"
    await asyncio.sleep(0.01)
    assert "(copy mode)" in b.status()
    del b.test_modes[ADVERSARY]
    await asyncio.sleep(0.01)
    await settle()
    assert sent[-1] == (ADVERSARY, "done")


async def test_failed_delivery_is_retried_not_stuck(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_mod, "DELIVERY_RETRY", 0)
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    real, fails = b.deliver, [1]

    async def flaky(role, header, body):
        if fails:
            fails.pop()
            raise subprocess.CalledProcessError(1, ["tmux", "send-keys"], stderr="can't find pane")
        await real(role, header, body)
    b.deliver = flaky
    await stop(b, WORKER, "done")
    a = b.panes[ADVERSARY]
    await asyncio.sleep(0.01)
    await settle()
    assert sent[-1] == (ADVERSARY, "done") and a.expecting_submit and a.reason is None
    await submit(b, ADVERSARY, sent)
    assert a.reason == "worker_msg"


async def test_old_state_at_the_cap_resumes_with_room(repo, tmp_path):
    """RootPilot's state.json: {"exchanges": 30, ...} from before the count restarted on resume."""
    b, sent = make(repo, tmp_path)
    (tmp_path / "run" / "state.json").write_text('{"exchanges": 30, "approvals": 1008, "denials": 27, "stalls": 0, "next_perm": 1316}')
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None, focus=lambda p: None,
                pane_mode=lambda p: None, screen_idle=lambda p: False, resume="Resumed.")
    assert (b2.exchanges, b2.total_exchanges, b2.approvals) == (0, 30, 1008)
    await start(b2)
    await submit(b2, ADVERSARY, sent)
    await stop(b2, ADVERSARY, "Next: the kill-switch image.")
    assert sent[-1] == (WORKER, "Next: the kill-switch image.") and not b2.finished


async def test_quit_terminates_then_kills_stragglers():
    polite = subprocess.Popen(["sleep", "30"])
    stubborn = subprocess.Popen(["sh", "-c", "trap '' TERM; exec sleep 30"])
    logged = []
    try:
        await asyncio.sleep(0.2)  # let sh install the trap before the SIGTERM
        await bridge_mod.stop_processes([polite.pid, stubborn.pid], grace=1, log=logged.append)
        await asyncio.sleep(0.2)
        assert not bridge_mod._alive(polite.pid) and not bridge_mod._alive(stubborn.pid)
        # Only the one ignoring SIGTERM needed SIGKILL.
        assert logged == [f"pid {stubborn.pid} didn't exit within 1s; killing it"]
    finally:
        for p in (polite, stubborn):
            p.kill()


def worker_log(path, entries):
    """A worker session JSONL. Each entry is ("prompt"|"resp", text), ("tool", name, input, result),
    ("tool_noresult", name, input), or a raw dict passed straight through (thinking, meta, ...)."""
    lines = []
    for e in entries:
        if isinstance(e, dict):
            lines.append(e)
        elif e[0] == "prompt":
            lines.append({"type": "user", "message": {"role": "user", "content": e[1]}})
        elif e[0] == "resp":
            lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": e[1]}]}})
        elif e[0] in ("tool", "tool_noresult"):
            tid = f"tu-{len(lines)}"
            lines.append({"type": "assistant",
                          "message": {"content": [{"type": "tool_use", "id": tid, "name": e[1], "input": e[2]}]}})
            if e[0] == "tool":
                lines.append({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": e[3]}]}})
    Path(path).write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return str(path)


async def test_read_worker_shows_responses_and_tool_calls_oldest_first(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    log = worker_log(tmp_path / "w.jsonl", [
        ("prompt", "do it"),
        ("resp", "Reading the file."),
        ("tool", "Read", {"file_path": "/x/calc.py"}, "1  def add(): ..."),
        ("tool", "Grep", {"pattern": "TODO", "path": "/x/src"}, "no matches"),
        ("tool", "Bash", {"command": "pytest -q", "description": "run tests"}, "3 passed"),
        ("resp", "All tests pass."),
    ])
    b.panes[WORKER].transcript_path = log
    out = b.on_mcp("read_worker", {"count": 10})
    assert not out.get("error")
    t = out["text"]
    assert (t.index("Reading the file.") < t.index("Read: /x/calc.py") < t.index("Grep: TODO")
            < t.index("Bash: pytest -q") < t.index("All tests pass."))
    assert "Grep: /x/src" not in t                               # the pattern wins over the path
    assert "3 passed" in t and "1  def add" in t                 # each tool call shows its result


async def test_read_worker_count_clamps_and_limits(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    log = worker_log(tmp_path / "w.jsonl", [("resp", f"line {i}") for i in range(20)])
    b.panes[WORKER].transcript_path = log
    few = b.on_mcp("read_worker", {"count": 3})["text"]
    assert few.count("**Worker:**") == 3 and "line 19" in few and "line 16" not in few   # newest 3
    big = b.on_mcp("read_worker", {"count": 999})["text"]
    assert big.count("**Worker:**") == 20                        # clamped to what's there, ceiling 50
    assert b.on_mcp("read_worker", {"count": 0})["text"].count("**Worker:**") == 1   # floored to 1
    assert b.on_mcp("read_worker", {"count": "nope"})["text"].count("**Worker:**") == 10  # default on junk


async def test_read_worker_marks_a_tool_call_with_no_result_yet(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    log = worker_log(tmp_path / "w.jsonl", [("tool_noresult", "Bash", {"command": "sleep 5"})])
    b.panes[WORKER].transcript_path = log
    assert "(no result yet)" in b.on_mcp("read_worker", {})["text"]


async def test_read_worker_truncates_big_input_and_output(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    log = worker_log(tmp_path / "w.jsonl", [
        ("tool", "Write", {"file_path": "x", "content": "Z" * 5000}, "Y" * 5000),
        ("resp", "Q" * 5000),
    ])
    b.panes[WORKER].transcript_path = log
    t = b.on_mcp("read_worker", {})["text"]
    assert "… (+" in t and "chars)" in t
    assert len(t) < 4000                                         # not the full 15k of raw content


async def test_read_worker_skips_thinking_and_meta(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    log = worker_log(tmp_path / "w.jsonl", [
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "secret reasoning"}]}},
        {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "injected reminder"}]}},
        ("resp", "visible answer"),
    ])
    b.panes[WORKER].transcript_path = log
    t = b.on_mcp("read_worker", {})["text"]
    assert "visible answer" in t and "secret reasoning" not in t and "injected reminder" not in t


async def test_read_worker_falls_back_to_the_recorded_session_log(repo, tmp_path, monkeypatch):
    from adversary import sessions
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", tmp_path / "projects")
    folder = tmp_path / "projects" / __import__("re").sub(r"[^A-Za-z0-9]", "-", str(repo))
    folder.mkdir(parents=True)
    worker_log(folder / "wsid.jsonl", [("resp", "from the recorded log")])
    b, sent = make(repo, tmp_path)
    b.cfg.sessions = {"worker": "wsid"}
    b.panes[WORKER].transcript_path = None                       # no live path seen yet
    assert "from the recorded log" in b.on_mcp("read_worker", {})["text"]


async def test_read_worker_errors_when_no_log_is_available(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    b.panes[WORKER].transcript_path = None
    b.cfg.sessions = {}
    r = b.on_mcp("read_worker", {})
    assert r.get("error") and "isn't available" in r["text"]


async def test_read_worker_is_read_only(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "awaiting dd-X")
    await _quiet_round(b, sent, tmp_path, "still (1)", tool_use=False, name="ro1")
    b.panes[WORKER].transcript_path = worker_log(tmp_path / "w.jsonl", [("resp", "hi")])
    before = (b.quiet_rounds, b.held, b.exchanges, len(sent), list(b.panes[WORKER].direct))
    b.on_mcp("read_worker", {"count": 5})
    assert (b.quiet_rounds, b.held, b.exchanges, len(sent), list(b.panes[WORKER].direct)) == before


async def test_read_worker_uses_the_live_transcript_path_from_hooks(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    log = worker_log(tmp_path / "live.jsonl", [("resp", "live turn output")])
    await stop_tx(b, WORKER, "done step", log)                  # Stop payload carries the path
    assert b.panes[WORKER].transcript_path == log
    assert "live turn output" in b.on_mcp("read_worker", {})["text"]
