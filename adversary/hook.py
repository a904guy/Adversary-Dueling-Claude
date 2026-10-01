"""Claude Code hook command: forwards a hook event to the bridge.

Invoked as ``python -m adversary.hook <sock> <role> <event>`` with the hook's
JSON payload on stdin. Prints the hook output JSON the bridge returns.

If the bridge is unreachable the hook prints ``{}`` so the agent behaves like
normal Claude Code (e.g. a permission prompt falls back to the on-screen UI).
"""

import json
import sys

from adversary.ipc import request


def main() -> None:
    sock, role, event = sys.argv[1:4]
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    try:
        reply = request(sock, {"type": "hook", "role": role, "event": event, "payload": payload})
    except OSError:
        reply = {}
    print(json.dumps(reply.get("output", {})))


if __name__ == "__main__":
    main()
