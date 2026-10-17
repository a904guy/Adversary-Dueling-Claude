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
            "properties": {
                "reason": {"type": "string", "description": "What the worker is waiting for, for the run log."},
                "check_after_minutes": {"type": "number", "description": "Ask me to check on the hold if the "
                                        "worker hasn't come back by then. Default: 10 when it is waiting on "
                                        "background work, otherwise never. 0 turns the check-in off."},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "read_worker",
        "description": (
            "Read recent activity: the last N responses and tool calls, with each tool call's result, "
            "oldest first. Only the worker's turn-end messages reach you on their own, and its file "
            "reads and searches (Read/Grep/Glob) never surface as permission requests, so this is how "
            "you see what it actually did. Use it to check a 'done' or 'I verified X' claim before "
            "accepting it, or when a worker message is vague. "
            "With no `agent`, reads the main worker, and lists any subagents it spawned. Work handed to "
            "a subagent shows in the worker's log only as that one call and its result, NOT the "
            "subagent's own steps — to see those, pass `agent` with the subagent's id (the id shown in "
            "its permission request, or in the spawned-subagents list) or its type name. Read-only: "
            "nothing is sent to the worker or the subagent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "How many recent items to show (1–50, default 10)."},
                "agent": {"type": "string", "description": "A subagent to read instead of the main worker: "
                          "its agent id (from its permission request) or its type name. Omit for the worker."},
            },
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List the agents working on this run: the main worker (busy or idle) and every subagent it "
            "spawned this session, each with its agent id, type, task, foreground/background, and whether "
            "it is still running or finished. Pass an id to read_worker(agent=...) to read that agent's "
            "own steps. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"all": {"type": "boolean", "description": "Also list subagents from earlier "
                           "sessions of this run (before a resume). Default false."}},
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

# On a card run (ADVERSARY_CARD=1) the supervisor also gets these; the worker's own copy of this
# server (ADVERSARY_ROLE=worker) offers request_feedback alone.
CARD_TOOLS = [
    {
        "name": "request_feedback",
        "description": (
            "Ask the people on the card for a decision or something you can't work out yourself (a product "
            "choice, credentials, access). Posts your question on the card and moves it to Needs Feedback; "
            "their reply comes back to you. The card can't be reported done while a request is unanswered. "
            "Keep working on whatever doesn't depend on the answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "What you need, with the context a "
                                        "person needs to answer it without reading the code."}},
            "required": ["question"],
        },
    },
    {
        "name": "report_done",
        "description": (
            "Finish the card: call it only after you have verified every requirement yourself, as for "
            "finish(complete). Merges the card's branches into their targets, posts your summary on the "
            "card and moves it to Done. Refused while a feedback request is unanswered, or when the work "
            "isn't committed or doesn't merge cleanly — the reply says what to fix."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was done and verified, for the people reading "
                            "the card."},
                "leave_unticked": {"type": "string", "description": "Only if a checklist item genuinely shouldn't "
                                   "be done: why. Without it, report_done is refused while items are unticked."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "suggest_task",
        "description": "File follow-up work you noticed, outside this card's scope, as a new card in the "
                       "Suggested column for people to approve, labelled as suggested by an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A short card title."},
                "description": {"type": "string", "description": "What and why, enough to act on."},
                "tasks": {"type": "array", "items": {"type": "string"},
                          "description": "Optional checklist of steps for the card."},
            },
            "required": ["title", "description"],
        },
    },
    {
        "name": "tick_checklist",
        "description": (
            "Tick an item on the card's checklist once you have verified it yourself (not on the worker's word), "
            "so the card shows progress. done=false unticks it. The checklist is the people's: this is the only "
            "change you may make to it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "The item's number (1 = first) or its exact name."},
                "done": {"type": "boolean", "description": "Default true; false unticks."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "add_checklist_item",
        "description": (
            "Add an item to the end of the card's checklist. On a card without one, lay out your plan's main "
            "steps as you hand off; on any card, add a step you discover. You can't rename or remove items."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The step, short (one line)."}},
            "required": ["name"],
        },
    },
    {
        "name": "attach_image",
        "description": (
            "Attach an image to the card, e.g. a screenshot of the finished result. With cover=true it becomes "
            "the card's cover, so the board shows what was built. PNG, JPEG, GIF or WebP, up to 10 MB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The image file (absolute, or relative to the repo)."},
                "cover": {"type": "boolean", "description": "Make it the card's cover image. Default false."},
            },
            "required": ["path"],
        },
    },
]


def tools() -> list[dict]:
    if os.environ.get("ADVERSARY_ROLE") == "worker":
        return [t for t in CARD_TOOLS if t["name"] == "request_feedback"]
    return TOOLS + (CARD_TOOLS if os.environ.get("ADVERSARY_CARD") else [])


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    if method == "initialize":
        return {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "adversary", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": tools()}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            reply = request(
                os.environ["ADVERSARY_SOCK"],
                {"type": "mcp", "tool": params.get("name"), "args": params.get("arguments", {}),
                 "role": os.environ.get("ADVERSARY_ROLE") or "adversary"},
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
