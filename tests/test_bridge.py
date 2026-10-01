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
    sent = []

    async def deliver(role, header, body):
        assert header.strip()
        sent.append((role, body))

    return Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=deliver, log=lambda m: None), sent


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
        assert {t["name"] for t in replies[1]["result"]["tools"]} == {"approve_tool", "deny_tool", "changed_files", "finish"}
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
