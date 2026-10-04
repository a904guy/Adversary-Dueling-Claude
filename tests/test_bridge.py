import asyncio
import json
import subprocess

import pytest

from adversary import bridge as bridge_mod
from adversary.bridge import ADVERSARY, WORKER, Bridge
from adversary.config import RunConfig


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bridge_mod, "IDLE_SETTLE", 0)


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
    popups, has_focus = [], set()                 # popups shown; roles whose pane has focus

    async def popup(pane, title, text):
        popups.append((pane.role, title, text))
        return True

    async def deliver(role, header, body):
        assert header.strip()
        sent.append((role, body))

    b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=deliver, log=lambda m: None,
               screen_idle=lambda pane: pane.target in screen_idle, focus=lambda pane: focused.append(pane.role),
               focused=lambda pane: pane.role in has_focus, popup=popup)
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


async def test_startup_delivers_task_and_brief(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    assert (WORKER, "Do the thing.\nAll of it.") in sent
    brief = [t for r, t in sent if r == ADVERSARY][0]
    assert "Do the thing.\nAll of it." in brief and b.cfg.base_sha in brief


async def test_relay_is_verbatim_both_ways(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")            # init turn: not forwarded
    assert not any(r == WORKER and t == "Ready." for r, t in sent)
    await stop(b, WORKER, "Shall I proceed?\n\n- yes\n- no")
    assert sent[-1] == (ADVERSARY, "Shall I proceed?\n\n- yes\n- no")
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Yes. Finish *everything*.\nDon't stop.")
    assert sent[-1] == (WORKER, "Yes. Finish *everything*.\nDon't stop.")
    assert b.exchanges == 1


async def test_queues_until_idle(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, WORKER, "done")                 # adversary still busy with init
    assert not any(r == ADVERSARY and t == "done" for r, t in sent)
    await stop(b, ADVERSARY, "Ready.")
    assert sent[-1] == (ADVERSARY, "done")


async def test_permission_approve_and_deny(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")

    pending = asyncio.create_task(b.on_hook(WORKER, "PermissionRequest",
                                            {"tool_name": "Bash", "tool_input": {"command": "pytest"}}))
    await settle()
    assert sent[-1][0] == ADVERSARY and "permission #1" in sent[-1][1] and "pytest" in sent[-1][1]
    assert b.on_mcp("approve_tool", {"request_id": 1})["text"].startswith("Approved")
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
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
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
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
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


async def test_exchange_cap(repo, tmp_path):
    b, sent = make(repo, tmp_path, max_exchanges=1)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
    for i in range(2):
        await stop(b, WORKER, f"w{i}")
        await submit(b, ADVERSARY, sent)
        await stop(b, ADVERSARY, f"a{i}")
        if i == 0:
            await submit(b, WORKER, sent)
    assert (WORKER, "a0") in sent and (WORKER, "a1") not in sent
    assert b.finished == "INCOMPLETE"


async def test_human_typing_is_detected_and_reply_still_relayed(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
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
    assert len(sent) == 2


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
        assert {t["name"] for t in replies[1]["result"]["tools"]} == {"approve_tool", "deny_tool", "message_worker", "hold", "changed_files", "finish"}
        assert "CANNOT_COMPLETE" in replies[2]["result"]["content"][0]["text"]
        assert b.finished == "CANNOT_COMPLETE"


async def test_wrapped_paste_is_not_mistaken_for_human(repo, tmp_path):
    """Claude Code rewrites long pastes (<pasted_content>); our submit must still be recognised."""
    b, sent = make(repo, tmp_path)
    await start(b)
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": 'Here is your task: <pasted_content id="x">…</pasted_content>'})
    assert b.panes[WORKER].reason == "task"
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "[Pasted text #1 +40 lines]"})
    assert b.panes[ADVERSARY].reason == "init"


def test_every_delivery_has_a_typed_header():
    for reason in ("task", "adversary_msg", "init", "worker_msg", ("perm", 3), "other"):
        assert Bridge.header(WORKER, reason).strip()


async def test_queued_worker_messages_are_coalesced_with_human_note(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
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
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
    await stop(b, WORKER, "Done.")
    await submit(b, ADVERSARY, sent)
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": NOTE})
    await stop(b, ADVERSARY, "Fix items 1-7.")
    assert sent[-1] == (WORKER, "Fix items 1-7.")
    assert not b.panes[ADVERSARY].human_prompts


async def test_idle_notification_continues_previous_turn(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
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
    await start(b)
    await submit(b, WORKER, sent)
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": NOTE})
    assert not b.panes[WORKER].human_prompts


async def test_human_typing_mid_turn_keeps_reason(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
    await stop(b, WORKER, "Done.")
    await submit(b, ADVERSARY, sent)
    await b.on_hook(ADVERSARY, "UserPromptSubmit", {"prompt": "also check the docs"})
    await stop(b, ADVERSARY, "Check the docs too.")
    assert sent[-1] == (WORKER, "Check the docs too.")


async def test_message_worker_mid_turn_and_idle(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)          # worker is mid-turn on the task
    r = b.on_mcp("message_worker", {"message": "Also add a --verbose flag."})
    await settle()
    assert not r.get("error") and sent[-1] == (WORKER, "Also add a --verbose flag.")
    assert b.panes[WORKER].busy and b.panes[WORKER].reason == "task"
    await submit(b, WORKER, sent)           # Claude Code queues it: UserPromptSubmit fires mid-turn
    assert not b.panes[WORKER].human_prompts and b.panes[WORKER].reason == "task"
    await stop(b, WORKER, "Done, flag added.")
    assert b.panes[ADVERSARY].queue or sent[-1] == (ADVERSARY, "Done, flag added.")
    # idle worker: becomes a normal delivery
    await submit(b, ADVERSARY, sent)
    b.on_mcp("message_worker", {"message": "One more thing."})
    await settle()
    assert sent[-1] == (WORKER, "One more thing.")


async def test_message_worker_waits_for_pending_permission(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    await start(b)
    await submit(b, WORKER, sent)
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
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    assert b.check_stall(now=0) is None                 # both busy
    await stop(b, ADVERSARY, "Ready.")
    b.panes[WORKER].busy = False                        # e.g. a relay was lost: nobody is working
    b.panes[WORKER].last_reason = "task"
    assert b.check_stall(now=100) is None               # idle clock starts
    assert b.check_stall(now=150) is None
    rec = b.check_stall(now=161)
    assert rec and rec["idle_seconds"] == 61 and rec["adversary_last_text"] == "Ready."
    assert b.check_stall(now=500) is None               # once per episode
    lines = (tmp_path / "run" / "stalls.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["worker_last_reason"] == "'task'"
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
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")
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
    await submit(b, WORKER, sent)
    await b.on_hook(WORKER, "SessionStart", {"session_id": "w-1", "source": "compact"})  # mid-turn
    assert b.panes[WORKER].busy


async def test_resume_asks_adversary_and_forwards_its_reply(repo, tmp_path):
    b, sent = make(repo, tmp_path)
    b.exchanges, b.approvals, b.next_perm = 4, 9, 12
    b.save_state()
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None, focus=lambda p: None,
                screen_idle=lambda p: False, resume="Run resumed. Check and reply.")
    await start(b2)
    assert sent == [(ADVERSARY, "Run resumed. Check and reply.")]   # no task re-sent to the worker
    assert (b2.exchanges, b2.approvals, b2.next_perm) == (4, 9, 12)
    await submit(b2, ADVERSARY, sent)
    await stop(b2, ADVERSARY, "Fix item 3.")
    assert sent[-1] == (WORKER, "Fix item 3.") and b2.exchanges == 5


async def test_resume_without_state_keeps_permission_numbers_unique(repo, tmp_path):
    b, _ = make(repo, tmp_path)
    b.transcript.add("Permission request #41: Bash", "x")
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None, focus=lambda p: None,
                screen_idle=lambda p: False, resume="go")
    assert b2.next_perm == 42


async def ready(b, sent):
    await start(b)
    await submit(b, WORKER, sent)
    await submit(b, ADVERSARY, sent)
    await stop(b, ADVERSARY, "Ready.")


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
    await start(b)
    await submit(b, WORKER, sent)                          # worker mid-turn on the task
    b.on_mcp("message_worker", {"message": "Use the fixtures dir."})
    await settle()
    await b.on_hook(WORKER, "UserPromptSubmit", {"prompt": "human: hurry up"})
    w = b.panes[WORKER]
    assert w.reason == "task" and w.direct == ["Use the fixtures dir."]
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
