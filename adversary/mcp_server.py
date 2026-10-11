"""Stdio MCP server giving the adversary its control tools.

Dependency-free: implements just enough JSON-RPC 2.0 / MCP (initialize,
tools/list, tools/call, ping) for Claude Code. Each tool call is relayed to
the bridge over the Unix socket named by $ADVERSARY_SOCK.
"""

import json
import os
import sys

from adversary.ipc import request

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "approve_tool",
        "description": "Approve a pending permission request from the worker agent so its tool call runs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "integer", "description": "The permission request number, e.g. 12 for request #12."},
                "note": {"type": "string", "description": "Optional note for the run log."},
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "deny_tool",
        "description": "Deny a pending permission request from the worker agent. The reason is shown to the worker so it can adjust.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "integer", "description": "The permission request number."},
                "reason": {"type": "string", "description": "Why it is denied and what the worker should do instead."},
            },
            "required": ["request_id", "reason"],
        },
    },
    {
        "name": "message_worker",
        "description": (
            "Send a message to the worker right away, without waiting for it to end its turn. If it is "
            "working, the message is folded into its current turn; if it is idle, it starts a new turn. "
            "Use it for new instructions or work from the human, or corrections it needs now. If you have "
            "already called finish and the human comes back with new work, call this: it picks the run "
            "back up and starts the worker again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "What to tell the worker, in plain language."}},
            "required": ["message"],
        },
    },
    {
        "name": "hold",
        "description": (
            "Leave the idle worker waiting, with nothing sent to it, when it has no work for you right now. "
            "Use it when the worker is waiting on a background subagent or task (its turn just says it is "
            "'awaiting' something), or when the human tells you to leave the worker alone: a reply of yours "
            "would only wake the worker to say it is still waiting. Your reply this turn is not forwarded. "
            "The hold ends on its own when the worker's next real turn arrives (a subagent finishing brings "
            "it back), or when you call message_worker, or the human types to the worker."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "What the worker is waiting for, for the run log."}},
            "required": ["reason"],
        },
    },
    {
        "name": "read_worker",
        "description": (
            "Read the worker's own recent activity: its last N responses and tool calls, with each "
            "tool call's result, oldest first. Only the worker's turn-end messages reach you on their "
            "own, and its file reads and searches (Read/Grep/Glob) never surface as permission requests, "
            "so this is how you see what it actually did. Use it to check a 'done' or 'I verified X' "
            "claim before accepting it, or when a worker message is vague. It shows the worker's own "
            "calls; work it handed to a background subagent shows only as that call and its result, not "
            "the subagent's own steps. Read-only: nothing is sent to the worker."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "How many recent items to show (1–50, default 10)."},
            },
        },
    },
    {
        "name": "changed_files",
        "description": (
            "List files the worker added (A), modified (M) or deleted (D) since the run started. "
            "Works with or without git."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": (
            "End the supervised run. Call with outcome 'complete' only after you have verified every part "
            "of the original task is done and working; call with 'cannot_complete' if progress is impossible."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["complete", "cannot_complete"]},
                "summary": {"type": "string", "description": "What was verified, and anything the human should know."},
            },
            "required": ["outcome", "summary"],
        },
    },
]


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    if method == "initialize":
        return {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "adversary", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            reply = request(
                os.environ["ADVERSARY_SOCK"],
                {"type": "mcp", "tool": params.get("name"), "args": params.get("arguments", {})},
            )
            text, is_error = reply.get("text", ""), bool(reply.get("error"))
        except (OSError, KeyError) as e:
            text, is_error = f"Bridge unreachable: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    if method == "ping":
        return {}
    return None


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if "id" not in msg:  # notification
            continue
        result = handle(msg)
        if result is None:
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}}
        else:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
