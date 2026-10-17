import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from adversary import bridge as bridge_mod
from adversary.bridge import ADVERSARY, WORKER, Bridge
from adversary.config import RunConfig


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bridge_mod, "IDLE_SETTLE", 0)
    monkeypatch.setattr(bridge_mod, "RESUME_SETTLE", 0)
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


def transcript_file(tmp_path, name, *, tool_use, background=False):
    """A minimal session JSONL for one turn, so turn_had_tool_use can read it. With background,
    an earlier turn launched a background command that hasn't reported back yet."""
    p = tmp_path / f"t-{name}.jsonl"
    content = ([{"type": "tool_use", "name": "Bash", "input": {}}] if tool_use else []) + [{"type": "text", "text": "x"}]
    bg = [json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_bg1", "name": "Bash",
         "input": {"command": "./long-job.sh", "run_in_background": True}}]}})] if background else []
    p.write_text("\n".join(bg + [
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
        assert {t["name"] for t in replies[1]["result"]["tools"]} == {"approve_tool", "deny_tool", "message_worker", "hold", "read_worker", "list_agents", "changed_files", "finish"}
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
        any(t == "Tests failed: fix test_x." for t, _ in b.panes[WORKER].queue) or \
        "Tests failed: fix test_x." in b.panes[WORKER].direct   # waits on the in-flight paste's submit


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


async def test_direct_message_after_permission_is_deferred_so_the_worker_can_resume(repo, tmp_path, monkeypatch):
    """A message_worker reply queued during a permission wait must not be pasted the instant the
    decision is sent (the worker's prompt box isn't back yet and the paste is lost); it waits
    RESUME_SETTLE so the worker resumes first, then is delivered."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    monkeypatch.setattr(bridge_mod, "RESUME_SETTLE", 0.05)   # real but short, so we can observe the wait
    perm = asyncio.create_task(b.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    await settle()
    b.on_mcp("message_worker", {"message": "Use the fixtures dir."})
    await settle()
    assert (WORKER, "Use the fixtures dir.") not in sent     # held while the permission is pending
    b.on_mcp("approve_tool", {"request_id": 1})
    await settle()
    assert (WORKER, "Use the fixtures dir.") not in sent     # NOT pasted instantly: worker still resuming
    assert b.panes[WORKER].direct == ["Use the fixtures dir."]   # still queued, not lost
    await asyncio.sleep(0.08)
    await settle()
    assert sent[-1] == (WORKER, "Use the fixtures dir.")     # delivered once the worker has resumed
    assert (await perm)["hookSpecificOutput"]["decision"]["behavior"] == "allow"


async def test_direct_message_prompted_just_after_the_approve_is_also_deferred(repo, tmp_path, monkeypatch):
    """The race isn't only for a message queued before the decision: if the human prompts the
    supervisor a beat AFTER it approves, the resulting message_worker still pastes into a worker
    that is mid-resume. The deferral keys off the decision time, so this ordering is covered too."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    monkeypatch.setattr(bridge_mod, "RESUME_SETTLE", 0.05)
    perm = asyncio.create_task(b.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    await settle()
    b.on_mcp("approve_tool", {"request_id": 1})             # approve first; no message queued yet
    await settle()
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "actually, skip the cache test"})
    b.on_mcp("message_worker", {"message": "Skip the cache test."})
    await settle()
    assert (WORKER, "Skip the cache test.") not in sent     # still inside the resume window: deferred
    await asyncio.sleep(0.08)
    await settle()
    assert sent[-1] == (WORKER, "Skip the cache test.")     # delivered once the worker has resumed
    await perm


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


async def _quiet_round(b, sent, tmp_path, worker_text, *, tool_use, name, background=True):
    """One full round: the adversary replies, the worker runs a turn and ends it. By default the
    worker has a background task still out (what the quiet-loop guard is for)."""
    await submit(b, ADVERSARY, sent)                       # adversary picks up the worker's message
    await stop(b, ADVERSARY, f"leaving it be ({name})")    # forwarded to the worker
    await submit(b, WORKER, sent)                          # worker's turn starts (reason adversary_msg)
    await stop_tx(b, WORKER, worker_text, transcript_file(tmp_path, name, tool_use=tool_use, background=background))


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


async def test_text_only_replies_with_nothing_in_the_background_always_go_through(repo, tmp_path):
    """A worker answering the supervisor in words ("I don't understand", "I'll do that after Y")
    is conversation, not a wait loop: with no background work out, every reply is relayed."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "done with step 1")
    for i, text in enumerate(["I don't know what 'the retry shim' refers to.",
                              "Still unclear: there's no retry module in this repo.",
                              "Got it, but I'm finishing the parser first; I'll come back to it."]):
        await _quiet_round(b, sent, tmp_path, text, tool_use=False, name=f"c{i}", background=False)
        assert (ADVERSARY, text) in sent
    assert not b.held and b.quiet_rounds == 0


async def test_a_background_task_that_reported_back_no_longer_counts(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "kicked off the build")
    for name in ("n1", "n2"):
        path = transcript_file(tmp_path, name, tool_use=False, background=True)
        with open(path, "a") as f:   # Claude Code's notification that the background task ended
            f.write(json.dumps({"type": "queue-operation", "content": "<task-notification>\n<tool-use-id>toolu_bg1"
                                "</tool-use-id>\n<status>completed</status>\n</task-notification>"}) + "\n")
        await submit(b, ADVERSARY, sent)
        await stop(b, ADVERSARY, f"ok ({name})")
        await submit(b, WORKER, sent)
        await stop_tx(b, WORKER, f"what next? ({name})", path)
    assert not b.held and (ADVERSARY, "what next? (n2)") in sent


async def test_quiet_hold_names_what_the_worker_is_waiting_on(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "awaiting the job")
    await _quiet_round(b, sent, tmp_path, "still (1)", tool_use=False, name="h1")
    await _quiet_round(b, sent, tmp_path, "still (2)", tool_use=False, name="h2")
    assert b.held and "long-job.sh" in b.held
    assert "long-job.sh" in sent[-1][1]


async def _auto_held(b, sent, tmp_path):
    """Drive the bridge into a quiet-loop hold with a background task still out."""
    await ready(b, sent)
    await stop(b, WORKER, "awaiting the job")
    await _quiet_round(b, sent, tmp_path, "still (1)", tool_use=False, name="k1")
    await _quiet_round(b, sent, tmp_path, "still (2) - also, YAML or TOML for the config?", tool_use=False, name="k2")
    assert b.held


async def test_auto_hold_keeps_the_withheld_message_and_sends_it_with_the_next(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await _auto_held(b, sent, tmp_path)
    assert not any("YAML or TOML" in t for r, t in sent if r == ADVERSARY)   # not relayed at the time
    assert b.held_backlog and "msg kept" in b.status()
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Standing down.")                 # the quiet note's reply goes nowhere
    # The background job finishes: Claude Code wakes the worker, which reports back.
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "<task-notification>\n<status>completed</status>"})
    assert b.held is None
    await stop(b, WORKER, "The job finished; results are in out.txt.")
    got = [t for r, t in sent if r == ADVERSARY][-1]
    assert "YAML or TOML" in got and "The job finished" in got     # the whole chain, in order
    assert got.index("YAML or TOML") < got.index("The job finished")
    assert not b.held_backlog


async def test_hold_check_in_asks_the_supervisor_after_the_deadline(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await _auto_held(b, sent, tmp_path)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "ok")
    n = len(sent)
    assert not b.check_hold(now=time.monotonic() + 30)             # too early
    assert b.check_hold(now=time.monotonic() + bridge_mod.HOLD_CHECK_AFTER + 1)
    await settle()
    note = sent[-1][1]
    assert len(sent) == n + 1 and sent[-1][0] == ADVERSARY and note.startswith("Check-in")
    assert "long-job.sh" in note and "YAML or TOML" in note        # what's running, and what was kept back
    assert b.held and not b.held_backlog                           # still held; backlog handed over
    assert b.hold_check_every == 2 * bridge_mod.HOLD_CHECK_AFTER   # next ask is later, never silent for good
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "I'll keep waiting.")
    assert not any(r == WORKER and t == "I'll keep waiting." for r, t in sent)   # not forwarded


async def test_hold_check_in_says_when_nothing_is_running_anymore(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await _auto_held(b, sent, tmp_path)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "ok")
    b._background_work = lambda: []                                # the job ended without waking the worker
    assert b.check_hold(now=time.monotonic() + bridge_mod.HOLD_CHECK_AFTER + 1)
    await settle()
    assert "Nothing the worker launched in the background is still running" in sent[-1][1]


async def test_hold_tool_check_in_defaults(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "need a decision")
    b._background_work = lambda: []
    r = b.on_mcp("hold", {"reason": "waiting on the human"})
    assert b.held and b.hold_check_at is None and "check on it" not in r["text"]   # waits on a person: no check-in
    b.release_hold("test")
    b._background_work = lambda: ["Bash: make e2e"]
    r = b.on_mcp("hold", {"reason": "waiting on e2e"})
    assert b.hold_check_at is not None and "10 minutes" in r["text"]
    holds = b.holds
    b.on_mcp("hold", {"reason": "renewed", "check_after_minutes": 30})
    assert b.holds == holds                                        # renewing isn't a new hold
    assert 29 * 60 < b.hold_check_at - time.monotonic() <= 30 * 60
    b.on_mcp("hold", {"reason": "no check", "check_after_minutes": 0})
    assert b.hold_check_at is None
    b.on_mcp("hold", {"reason": "x", "check_after_minutes": 5})
    b.release_hold("the worker started a turn")
    assert b.hold_check_at is None and not b.check_hold(now=time.monotonic() + 9999)


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


async def test_popups_stop_retrying_after_tmux_rejects_them(repo, tmp_path):
    """On a tmux too old for some display-popup option, the popup fails. It must not be retried
    every second (the real _tmux_popup sets _popups_unsupported); check_popups then stands down."""
    b, sent = make(repo, tmp_path)
    calls = []

    async def failing_popup(pane, title, text):
        calls.append(title)
        b._popups_unsupported = True          # what _tmux_popup does on a hard failure
        return False                           # not shown, so it is never marked shown
    b.popup = failing_popup
    await start(b)
    for _ in range(5):                         # five "seconds"
        b.check_popups()
        await settle()
    assert calls == ["how this works"]         # attempted once, then disabled -- no spam
    assert b._popups_unsupported and not b.project_file.exists()  # nothing marked shown either


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


def subagent_log(worker_log, agent_id, meta, entries):
    """A subagent transcript (all sidechain) + meta beside a worker log, as Claude Code writes them."""
    worker_log = Path(worker_log)
    d = worker_log.parent / worker_log.stem / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for e in entries:
        if e[0] == "resp":
            lines.append({"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": e[1]}]}})
        elif e[0] == "done":   # its final reply: the subagent has finished
            lines.append({"type": "assistant", "isSidechain": True,
                          "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": e[1]}]}})
        elif e[0] == "tool":
            tid = f"t{len(lines)}"
            lines.append({"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "tool_use", "id": tid, "name": e[1], "input": e[2]}]}})
            lines.append({"type": "user", "isSidechain": True, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": e[3]}]}})
    m = {"requestShape": "background", **meta}
    (d / f"agent-{agent_id}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(m))


async def test_read_worker_reads_a_named_subagent_and_lists_them_otherwise(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    wlog = worker_log(tmp_path / "wsid.jsonl", [("resp", "delegating to a reviewer")])
    b.panes[WORKER].transcript_path = wlog
    subagent_log(wlog, "ag777", {"agentType": "reviewer", "description": "review the auth code"},
                 [("resp", "Checking auth."), ("tool", "Grep", {"pattern": "token"}, "3 hits")])
    plain = b.on_mcp("read_worker", {})["text"]
    assert "delegating to a reviewer" in plain
    assert "Subagents this worker spawned" in plain and "ag777" in plain and "reviewer" in plain
    sub = b.on_mcp("read_worker", {"agent": "ag777"})
    assert not sub.get("error")
    assert "Checking auth." in sub["text"] and "Grep: token" in sub["text"] and "3 hits" in sub["text"]
    assert "reviewer" in sub["text"]
    assert "Checking auth." in b.on_mcp("read_worker", {"agent": "reviewer"})["text"]   # by type too


async def test_list_agents_shows_the_worker_and_each_subagent_with_id_and_status(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    wlog = worker_log(tmp_path / "wsid.jsonl", [("resp", "x")])
    b.panes[WORKER].transcript_path = wlog
    subagent_log(wlog, "agrun1", {"agentType": "tester", "description": "run the suite"},
                 [("tool", "Bash", {"command": "pytest"}, "ok")])              # mid-work: running
    subagent_log(wlog, "agdone2", {"agentType": "reviewer", "requestShape": "foreground"},
                 [("resp", "looking"), ("done", "All good.")])                  # final reply: finished
    out = b.on_mcp("list_agents", {})
    assert not out.get("error")
    txt = out["text"]
    assert "**worker** (main) — busy" in txt
    run_line = next(l for l in txt.splitlines() if "agrun1" in l)
    done_line = next(l for l in txt.splitlines() if "agdone2" in l)
    assert "tester" in run_line and "running" in run_line and "background" in run_line and "run the suite" in run_line
    assert "reviewer" in done_line and "finished" in done_line and "foreground" in done_line
    assert 'read_worker(agent="agrun1")' in run_line


async def test_list_agents_hides_subagents_from_before_this_session_unless_asked(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    wlog = worker_log(tmp_path / "wsid.jsonl", [("resp", "x")])
    b.panes[WORKER].transcript_path = wlog
    subagent_log(wlog, "agold", {"agentType": "reviewer"}, [("done", "ok")])
    old = Path(wlog).parent / "wsid" / "subagents" / "agent-agold.jsonl"
    os.utime(old, (b._started_wall - 3600, b._started_wall - 3600))     # from an earlier session
    txt = b.on_mcp("list_agents", {})["text"]
    assert "agold" not in txt and "no subagents spawned this session" in txt and "1 more" in txt
    assert "agold" in b.on_mcp("list_agents", {"all": True})["text"]


async def test_read_worker_unknown_subagent_lists_what_is_available(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    wlog = worker_log(tmp_path / "wsid.jsonl", [("resp", "x")])
    b.panes[WORKER].transcript_path = wlog
    subagent_log(wlog, "ag1", {"agentType": "reviewer"}, [("resp", "hi")])
    r = b.on_mcp("read_worker", {"agent": "ghost"})
    assert r.get("error") and "reviewer" in r["text"] and "ag1" in r["text"]


async def test_permission_from_a_subagent_names_it_and_routes_the_deny_to_it(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    perm = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"},
         "agent_id": "ag42", "agent_type": "cleaner"}))
    await settle()
    txt = [t for r, t in sent if r == ADVERSARY][-1]
    assert "cleaner" in txt and "ag42" in txt and 'read_worker(agent="ag42")' in txt
    assert "subagent" in txt and "deny" in txt                      # tells the supervisor where the deny goes
    assert b.perms[1].agent_id == "ag42" and b.perms[1].agent_type == "cleaner"
    b.on_mcp("deny_tool", {"request_id": 1, "reason": "too destructive for a subagent"})
    out = await perm                                                # the decision returns to the requesting agent
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert out["hookSpecificOutput"]["decision"]["message"] == "too destructive for a subagent"


async def test_two_outstanding_permissions_resolve_to_their_own_agents(repo, tmp_path):
    """Main worker and a subagent both waiting on a decision: resolving them in the opposite order
    must deliver each decision to the request that asked, never crossed."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    p1 = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
                                       {"tool_name": "Bash", "tool_input": {"command": "ls"}}))          # #1 main
    await settle()
    p2 = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
        {"tool_name": "Bash", "tool_input": {"command": "rm x"}, "agent_id": "agZ", "agent_type": "cleaner"}))  # #2 sub
    await settle()
    assert set(b.perms) == {1, 2}
    b.on_mcp("deny_tool", {"request_id": 2, "reason": "no"})        # resolve the subagent's first
    b.on_mcp("approve_tool", {"request_id": 1})                     # then the main worker's
    o1, o2 = await p1, await p2
    assert o1["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert o2["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "no"}


async def test_main_worker_permission_has_no_subagent_note(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    perm = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
                                         {"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    await settle()
    txt = [t for r, t in sent if r == ADVERSARY][-1]
    assert "subagent" not in txt.lower()
    b.on_mcp("approve_tool", {"request_id": 1})
    await perm


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


# ── end-of-run stats ────────────────────────────────────────────────────

async def test_stats_first_run_is_single_column_with_totals(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    b.approvals, b.denials, b.holds, b.nudges, b.peeks = 3, 1, 2, 4, 5
    lines = b.stats_lines()
    text = "\n".join(lines)
    assert not b.is_resume
    assert "session" not in text and "this session" not in text   # one column on a first run
    assert "approval rate: 75% of 4 decided" in text
    # A metric row shows its total in the single value column.
    assert any(l.strip().startswith("Holds") and l.rstrip().endswith("2") for l in lines)


async def test_stats_resume_shows_session_and_total_columns(repo, tmp_path):
    b, _ = make(repo, tmp_path)
    b.total_exchanges, b.approvals, b.holds = 10, 8, 3
    b.save_state()
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None,
                focus=lambda p: None, pane_mode=lambda p: None, screen_idle=lambda p: False,
                resume="Resumed.")
    # Session deltas start at 0 even though the lifetime totals were loaded.
    rows = {label.strip(): (s, t) for label, s, t in b2._metric_rows()}
    assert rows["Exchanges (relayed to worker)"] == (0, 10)
    assert rows["· approved"] == (0, 8) and rows["Holds (worker parked)"] == (0, 3)
    # Do one exchange and one approval this session: the session column moves, the total follows.
    b2.total_exchanges += 1
    b2.approvals += 1
    rows = {label.strip(): (s, t) for label, s, t in b2._metric_rows()}
    assert rows["Exchanges (relayed to worker)"] == (1, 11)
    assert rows["· approved"] == (1, 9)
    text = "\n".join(b2.stats_lines())
    assert b2.is_resume and "session" in text and "total" in text


async def test_counters_increment_and_survive_a_resume(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)                                   # adversary ran its init turn
    await stop(b, WORKER, "done a bit")                    # one worker turn-end
    b.on_mcp("message_worker", {"message": "carry on"})    # a nudge
    await settle()
    b.panes[WORKER].transcript_path = worker_log(tmp_path / "w.jsonl", [("resp", "hi")])
    b.on_mcp("read_worker", {"count": 5})                  # a peek
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "human note"})  # a human interjection
    assert b.worker_turns >= 1 and b.adversary_turns >= 1
    assert b.nudges == 1 and b.peeks == 1 and b.human_prompts_count == 1
    b.save_state(final=True)
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert state["nudges"] == 1 and state["peeks"] == 1 and state["human_prompts_count"] == 1
    assert state["worker_turns"] == b.worker_turns and "runtime_seconds" in state
    # A resumed bridge reloads the lifetime totals.
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None,
                focus=lambda p: None, pane_mode=lambda p: None, screen_idle=lambda p: False, resume="r")
    assert b2.nudges == 1 and b2.peeks == 1 and b2.worker_turns == b.worker_turns


async def test_holds_counts_every_kind_of_park(repo, tmp_path):
    b, sent = make(repo, tmp_path, max_exchanges=1)
    await ready(b, sent)
    # Manual hold.
    await stop(b, WORKER, "idle")
    await submit(b, ADVERSARY, sent)
    b.on_mcp("hold", {"reason": "waiting"})
    assert b.holds == 1
    b.release_hold("go")
    # Exchange-cap hold.
    b._cap_reached("looping reply")
    assert b.holds == 2
    b.release_hold("go")
    # Quiet-loop hold.
    b._quiet_hold()
    assert b.holds == 3


async def test_stats_runtime_total_accumulates_across_sessions(repo, tmp_path):
    b, _ = make(repo, tmp_path)
    b._prior_runtime = 0
    b._started -= 120                                      # pretend this session has run two minutes
    b.save_state(final=True)
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert state["runtime_seconds"] >= 120
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None,
                focus=lambda p: None, pane_mode=lambda p: None, screen_idle=lambda p: False, resume="r")
    assert b2._prior_runtime >= 120                        # carried forward
    assert b2._runtime_total() >= b2._prior_runtime        # plus this session's elapsed


async def test_print_status_shows_the_status_line_and_the_stats_block(repo, tmp_path):
    out = []
    b, sent = make(repo, tmp_path)
    b.log_fn = out.append                                   # capture both log() and log_fn() lines
    await ready(b, sent)
    b.approvals, b.denials = 3, 1
    b.print_status()
    text = "\n".join(out)
    assert "[running]" in text                              # the one-line status
    assert "adversary run · running" in text                # the full stats block too
    assert "approval rate: 75%" in text


async def test_close_writes_a_stats_file_for_the_launcher_to_reprint(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    b.approvals, b.denials = 3, 1
    lines = b.save_stats_file("ABORTED")
    text = (tmp_path / "run" / "stats.txt").read_text()
    assert "adversary run · ABORTED" in text and "approval rate: 75%" in text
    assert text.splitlines() == lines                      # same block returned for the pane print


def test_dur_formats_are_readable():
    assert Bridge._dur(45) == "45s"
    assert Bridge._dur(125) == "2m"
    assert Bridge._dur(3 * 3600 + 7 * 60) == "3h 7m"


async def test_supervisor_reply_reaches_a_busy_worker_mid_turn(repo, tmp_path):
    """The supervisor's end-of-turn reply goes to the worker immediately, folded into its running
    turn — not parked until the worker finishes."""
    b, sent = make(repo, tmp_path)
    await ready(b, sent)
    await stop(b, WORKER, "Step 1 done.")
    await submit(b, ADVERSARY, sent)
    # Before the supervisor answers, the worker is already busy again (the human, or a notification).
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "<task-notification>\n<status>completed</status>"})
    assert b.panes[WORKER].busy
    await stop(b, ADVERSARY, "Also add a test for the empty case.")
    assert (WORKER, "Also add a test for the empty case.") in sent      # delivered while the worker works
    assert not b.panes[WORKER].queue


NEW_TRUST = " Quick safety check\n ❯ No, exit\n   Yes, I trust this folder\n"
OLD_TRUST = " Quick safety check\n ❯ 1. Yes, I trust this folder\n   2. No, exit\n"


@pytest.mark.parametrize("screen,keys", [(NEW_TRUST, ["Down", "Enter"]), (OLD_TRUST, ["Enter"])])
async def test_trust_dialog_is_accepted_whichever_option_comes_first(repo, tmp_path, monkeypatch, screen, keys):
    """Claude Code 2.1.15x lists "Yes, I trust" first with the cursor on it; newer versions put
    "No, exit" first. Down+Enter on the older layout picks "No, exit" and the session quits."""
    b, _ = make(repo, tmp_path)
    pressed = {}
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bridge_mod.tmux, "capture", lambda target: screen)
    monkeypatch.setattr(bridge_mod.tmux, "press", lambda target, key: pressed.setdefault(target, []).append(key))
    monkeypatch.setattr(bridge_mod.asyncio, "sleep", lambda s: real_sleep(0))

    async def come_up():
        while not pressed.get("%1") or not pressed.get("%2"):
            await real_sleep(0)
        for p in b.panes.values():
            p.started = True

    await asyncio.gather(bridge_mod._startup_watch(b), come_up())
    assert pressed == {"%1": keys, "%2": keys}


async def test_managed_run_that_never_comes_up_closes(repo, tmp_path, monkeypatch):
    b, _ = make(repo, tmp_path, managed=True)
    stops = []
    b.request_stop = lambda: stops.append(1)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bridge_mod.tmux, "capture", lambda target: "something unexpected")
    monkeypatch.setattr(bridge_mod.asyncio, "sleep", lambda s: real_sleep(0))
    await bridge_mod._startup_watch(b)
    assert stops == [1]


@pytest.mark.parametrize("absorbed", [True, False])
async def test_a_mid_turn_message_whose_submit_never_shows_does_not_block_the_worker(repo, tmp_path, absorbed):
    """Seen live on Claude Code 2.1.153: a message typed in mid-turn was folded into the running turn
    without a UserPromptSubmit, the bridge kept waiting for one, and every later message to the worker
    (a merge conflict to fix) was held back for good."""
    b, sent = make(repo, tmp_path)
    await start(b)
    b.panes[WORKER].busy = True
    b.on_mcp("message_worker", {"message": "Ignore the task-list reminder."})
    await settle()
    assert b.panes[WORKER].expecting_submit                    # typed in; no submit seen
    log = tmp_path / "worker.jsonl"
    said = "Ignore the task-list reminder." if absorbed else "something else"
    log.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": said}}) + "\n")
    await b.on_hook(WORKER, "Stop", {"last_assistant_message": "done", "transcript_path": str(log)})
    await settle()
    after = len(sent)
    if absorbed:
        assert not b.panes[WORKER].expecting_submit            # settled: nothing is held back any more
        b.on_mcp("message_worker", {"message": "Fix the merge conflict."})
        await settle()
        assert [t for r, t in sent[after:] if r == WORKER] == ["Fix the merge conflict."]
    else:
        assert [t for r, t in sent[after - 1:] if r == WORKER][-1] == "Ignore the task-list reminder."   # resent
