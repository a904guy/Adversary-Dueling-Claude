"""Tiny newline-delimited JSON client for the bridge's Unix socket.

Used by the hook command and the MCP server, both of which are short-lived
processes spawned by Claude Code.
"""

import json
import socket


def request(sock_path: str, message: dict, timeout: float | None = None) -> dict:
    """Send one JSON message to the bridge and wait for one JSON reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps(message) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode()) if buf.strip() else {}
