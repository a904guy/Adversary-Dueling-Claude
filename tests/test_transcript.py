import json
from pathlib import Path

from adversary.transcript import token_usage


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
