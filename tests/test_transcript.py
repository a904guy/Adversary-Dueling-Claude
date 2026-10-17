import json

import pytest
from pathlib import Path

from adversary.transcript import find_subagent_log, recent_activity, subagent_logs, token_usage


def _worker_with_subagent(tmp_path, agent_id, agent_type, entries, **meta_extra):
    """A worker log at wsid.jsonl plus a subagent transcript in wsid/subagents/agent-<id>.jsonl
    (all sidechain, as Claude Code writes them) with its .meta.json sidecar."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    wlog = tmp_path / "wsid.jsonl"
    wlog.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "main worker"}]}}) + "\n")
    d = tmp_path / "wsid" / "subagents"
    d.mkdir(parents=True)
    lines = []
    for e in entries:
        if e[0] == "resp":
            lines.append({"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": e[1]}]}})
        elif e[0] == "tool":
            tid = f"t{len(lines)}"
            lines.append({"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "tool_use", "id": tid, "name": e[1], "input": e[2]}]}})
            lines.append({"type": "user", "isSidechain": True, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": e[3]}]}})
    (d / f"agent-{agent_id}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    meta = {"agentType": agent_type, "description": "do the thing", "toolUseId": "toolu_1", "requestShape": "background", **meta_extra}
    (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
    return str(wlog)


def test_recent_activity_can_read_a_sidechain_subagent_log(tmp_path):
    wlog = _worker_with_subagent(tmp_path, "aid123", "reviewer",
                                 [("resp", "Looking."), ("tool", "Bash", {"command": "pytest"}, "ok")])
    main = recent_activity(wlog, 10)
    assert "main worker" in main and "Looking." not in main           # worker read skips sidechain
    sub = next((tmp_path / "wsid" / "subagents").glob("agent-aid123.jsonl"))
    assert recent_activity(sub, 10) == ""                              # all sidechain: skipped by default
    body = recent_activity(sub, 10, skip_sidechain=False)
    assert "Looking." in body and "Bash: pytest" in body and "ok" in body


def test_subagent_logs_and_find(tmp_path):
    wlog = _worker_with_subagent(tmp_path, "abc123def", "security-reviewer", [("resp", "hi")])
    subs = subagent_logs(wlog)
    assert len(subs) == 1 and subs[0][1]["agent_id"] == "abc123def" and subs[0][1]["agentType"] == "security-reviewer"
    assert find_subagent_log(wlog, "abc123def")[1]["agent_id"] == "abc123def"          # exact id
    assert find_subagent_log(wlog, "abc123")[1]["agent_id"] == "abc123def"             # id substring
    assert find_subagent_log(wlog, "SECURITY-reviewer")[1]["agent_id"] == "abc123def"  # type, case-insensitive
    assert find_subagent_log(wlog, "nope") is None
    assert subagent_logs(tmp_path / "no" / "such.jsonl") == []


def _log(path, entries):
    Path(path).write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def test_token_usage_dedupes_entries_that_share_a_message_id(tmp_path):
    """One assistant message is split across several JSONL entries sharing message.id, each
    repeating the same usage; it must be counted once, not once per content block."""
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
             "output_tokens": 5}
    log = _log(tmp_path / "s.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "go"}},          # no usage
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text"}], "usage": usage}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use"}], "usage": usage}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t"}]}},  # no usage
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text"}], "usage": usage}},
    ])
    u = token_usage(log)
    # Two distinct messages (m1, m2), each: in = 10+100+1000 = 1110, out = 5.
    assert u == {"in": 2220, "out": 10, "total": 2230}


def test_token_usage_tolerates_a_missing_or_unreadable_log(tmp_path):
    assert token_usage(tmp_path / "nope.jsonl") == {"in": 0, "out": 0, "total": 0}
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{also bad\n")
    assert token_usage(bad) == {"in": 0, "out": 0, "total": 0}


def test_token_usage_counts_entries_without_an_id(tmp_path):
    """An entry carrying usage but no message.id (older logs) still counts."""
    log = _log(tmp_path / "s.jsonl", [
        {"type": "assistant", "message": {"content": [{"type": "text"}], "usage": {"output_tokens": 7}}},
        {"type": "assistant", "message": {"content": [{"type": "text"}], "usage": {"output_tokens": 3}}},
    ])
    assert token_usage(log) == {"in": 0, "out": 10, "total": 10}


def _bg_log(tmp_path, entries):
    p = tmp_path / "w.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def _bg_launch(tid, cmd, ts=None):
    e = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd, "run_in_background": True}}]}}
    if ts:
        e["timestamp"] = ts
    return e


def test_background_running_tracks_launches_until_notified(tmp_path):
    from adversary.transcript import background_running
    log = _bg_log(tmp_path, [
        _bg_launch("toolu_a", "make build"),
        _bg_launch("toolu_b", "npm run e2e"),
        {"type": "queue-operation", "content": "<task-notification>\n<tool-use-id>toolu_a</tool-use-id>\n"
                                               "<status>failed</status>\n</task-notification>"},
    ])
    out = background_running(log)
    assert len(out) == 1 and "npm run e2e" in out[0]


def test_background_running_ignores_launches_before_since(tmp_path):
    from adversary.transcript import background_running
    log = _bg_log(tmp_path, [_bg_launch("toolu_old", "sleep 999", ts="2020-01-01T00:00:00.000Z")])
    assert background_running(log, since=1_700_000_000) == []      # cut off in an earlier session
    assert len(background_running(log)) == 1


def test_background_running_counts_unfinished_background_subagents_and_teammates(tmp_path):
    from adversary.transcript import background_running
    wlog = _worker_with_subagent(tmp_path, "ag1", "researcher", [("tool", "WebSearch", {"query": "x"}, "r")])
    assert background_running(wlog) == ["researcher (ag1)"]          # mid-work, not notified
    d = tmp_path / "wsid" / "subagents"
    (d / "agent-ag1.jsonl").open("a").write(json.dumps(
        {"type": "assistant", "isSidechain": True, "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}) + "\n")
    assert background_running(wlog) == []                           # final reply: finished
    fg = _worker_with_subagent(tmp_path / "fg", "ag2", "helper", [("resp", "hi")], requestShape="foreground")
    assert background_running(fg) == []                             # foreground runs inside the turn
    tm = _worker_with_subagent(tmp_path / "tm", "ag3", "general", [("resp", "hi")],
                               requestShape=None, teamName="crew", name="dd-X")
    assert background_running(tm) == ["dd-X (ag3)"]                 # a teammate still working


@pytest.mark.parametrize("block", [5, 64, 1 << 20])
def test_logs_are_read_backwards_in_blocks_without_loading_them_whole(tmp_path, monkeypatch, block):
    """A 45-hour run's worker log reached 532 MB; reading it whole and splitting it took ~2.5 GB and
    the OOM killer took the bridge down. Reading backwards in blocks must give the same lines,
    whatever the block size relative to the lines."""
    from adversary import transcript
    monkeypatch.setattr(transcript, "_BLOCK", block)
    log = tmp_path / "s.jsonl"
    entries = [{"type": "user", "message": {"content": "go"}}] + [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": f"reply {i} " + "x" * (i * 7)}]}}
        for i in range(30)]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n\n")
    assert [json.loads(l) for l in transcript._lines_backwards(log)] == list(reversed(entries))
    assert transcript.last_assistant_text({"transcript_path": str(log)}).startswith("reply 29 ")
    assert "reply 29" in transcript.recent_activity(log, 2) and "reply 27" not in transcript.recent_activity(log, 2)
