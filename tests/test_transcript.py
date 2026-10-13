import json
from pathlib import Path

from adversary.transcript import find_subagent_log, recent_activity, subagent_logs, token_usage


def _worker_with_subagent(tmp_path, agent_id, agent_type, entries, **meta_extra):
    """A worker log at wsid.jsonl plus a subagent transcript in wsid/subagents/agent-<id>.jsonl
    (all sidechain, as Claude Code writes them) with its .meta.json sidecar."""
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
