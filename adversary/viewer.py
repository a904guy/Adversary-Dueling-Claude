"""The live view of a card run: a read-only web page showing the worker, the supervisor and the bridge
as they work, the way the tmux window does.

Each card run's bridge serves it over HTTPS on a port of its own (VIEWER_PORTS, kept across resumes),
with this machine's certificate. People never reach that port directly: the card's link goes to
https://kabana.hti.dev/live/<this host>/<port>/, and kabana's Caddy proxies it here. So the page is
on kabana's own origin, and the browser sends kabana's `accessToken` cookie along. The data
(`history`, `events`) is only served to a token kabana accepts for this card: GET /api/cards/<id>
with it must succeed, which 4ga allows only to the card's board members and the project's managers.
The page itself is public (it holds nothing): a link followed from Slack or email is a cross-site
navigation, which doesn't carry kabana's SameSite=Strict cookie, but the page's own requests do.

Nothing can be sent to the agents from here. Changes go through the card, so its log stays the record.

What's shown comes from the two Claude Code session logs (every message, thinking, tool call and its
result) and the bridge's own log lines, streamed with Server-Sent Events.
"""

import asyncio
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

VIEWER_PORTS = range(47000, 48000)   # kabana's Caddy proxies only these (its /live route's regex)
DEFAULT_BASE = "https://kabana.hti.dev/live"
LINK_LABEL = "Click here for logs:"
PAGE = Path(__file__).parent / "static" / "viewer.html"

KEEP = 6000            # events kept in memory, all panes together (the session logs keep everything)
TEXT_LIMIT = 30000     # characters of one message, input or result sent to the page
POLL = 0.5             # seconds between reads of the session logs
AUTH_TTL = 60.0        # seconds a token's answer from kabana is trusted
CERT_CHECK = 3600.0    # seconds between checks for a renewed certificate
HEARTBEAT = 15.0       # seconds between SSE comments that keep proxies from closing the stream
START_WINDOW = 48 << 20   # bytes from a log's end where reading starts
CHUNK = 4 << 20           # bytes read from a log per poll

# The typed header the bridge puts before each message it delivers (Bridge.header): who it's from.
SOURCES = [
    ("Here is your task", "task", "The task"),
    ("Your supervisor's plan", "supervisor", "Plan from the supervisor"),
    ("Message from your supervisor, sent while you were working", "supervisor", "Supervisor (while working)"),
    ("Message from your supervisor", "supervisor", "Supervisor"),
    ("Your supervision brief", "bridge", "Supervision brief"),
    ("This supervised run was resumed", "bridge", "Run resumed"),
    ("The worker agent ended its turn", "worker", "Worker"),
    ("Permission request from the worker", "permission", "Permission request"),
    ("A person commented on the card", "person", "Card comment"),
    ("A person edited a comment", "person", "Card comment edited"),
    ("Message from the adversary bridge", "bridge", "Bridge"),
]
PASTED = re.compile(r'<pasted_content id="[^"]*">\n?|\n?</pasted_content>')
SKIP = {"api-request", "api-request-blob", "api-request-shape", "attachment", "last-prompt", "mode",
        "permission-mode", "atis-latch", "ai-title", "tools-used", "file-history-snapshot",
        "file-history-delta", "cost-state", "summary"}


def link(base: str, host: str, port: int) -> str:
    return f"{base.rstrip('/')}/{host}/{port}/"


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit:,} more characters not shown)"


def _clip_value(value, limit: int = TEXT_LIMIT):
    """A tool input with every long string shortened, so one huge Write can't swamp the page."""
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, dict):
        return {k: _clip_value(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_value(v, limit) for v in value]
    return value


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text") or "")
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def classify(prompt: str) -> tuple[str, str, str]:
    """(source, label, body) for a prompt the agent received: the bridge's header names who it's
    from; anything else was typed in the pane by a person."""
    for prefix, source, label in SOURCES:
        if prompt.startswith(prefix):
            body = prompt.split("\n", 1)[1] if "\n" in prompt else ""
            return source, label, PASTED.sub("", body).strip()
    return "typed", "Typed in the pane", PASTED.sub("", prompt).strip()


def parse_entry(entry: dict, role: str) -> list[dict]:
    """The page's events for one session-log line (none for bookkeeping lines)."""
    kind = entry.get("type")
    if kind in SKIP or entry.get("isSidechain"):
        return []
    ts = entry.get("timestamp")
    base = {"role": role, "ts": ts}
    if kind == "system":
        sub = entry.get("subtype") or ""
        if sub == "compact_boundary":
            return [{**base, "kind": "notice", "text": "Conversation compacted"}]
        if sub in ("turn_duration", "stop_hook_summary"):
            return []
        text = entry.get("content") or sub
        return [{**base, "kind": "notice", "text": _clip(str(text), 2000)}] if text else []
    if kind not in ("user", "assistant"):
        return []
    msg = entry.get("message") or {}
    content = msg.get("content")
    out = []
    if kind == "user":
        if isinstance(content, str):
            if entry.get("isMeta") or entry.get("isCompactSummary"):
                label = "Summary of the conversation so far" if entry.get("isCompactSummary") else "Claude Code"
                return [{**base, "kind": "meta", "label": label, "text": _clip(content)}]
            source, label, body = classify(content)
            return [{**base, "kind": "prompt", "source": source, "label": label, "text": _clip(body)}]
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                out.append({**base, "kind": "tool_result", "id": block.get("tool_use_id"),
                            "error": bool(block.get("is_error")), "text": _clip(_result_text(block.get("content")))})
            elif block.get("type") == "text":
                source, label, body = classify(block.get("text") or "")
                if body:
                    out.append({**base, "kind": "prompt", "source": source, "label": label, "text": _clip(body)})
        return out
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text" and (block.get("text") or "").strip():
            out.append({**base, "kind": "text", "text": _clip(block["text"])})
        elif t == "thinking":
            thought = (block.get("thinking") or "").strip()
            out.append({**base, "kind": "thinking", "text": _clip(thought),
                        "ms": entry.get("thinkingDurationMs")})
        elif t == "tool_use":
            out.append({**base, "kind": "tool_use", "id": block.get("id"), "name": block.get("name"),
                        "input": _clip_value(block.get("input"))})
    return out


class LogTail:
    """Reads a JSONL file's new complete lines since the last read, a chunk at a time, so a session
    log of hundreds of MB never holds up the bridge's loop. A big log is read from near its end:
    only the last KEEP events are kept anyway."""

    def __init__(self, path: Path, start_window: int = START_WINDOW, chunk: int = CHUNK):
        self.path, self.rest, self.chunk = path, b"", chunk
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        self.offset = max(0, size - start_window)
        self.skip = self.offset > 0   # the first line read is only the end of one

    def read(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(self.chunk)
        except OSError:
            return []
        self.offset += len(data)
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()
        if self.skip and lines:
            lines, self.skip = lines[1:], False
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def kabana_check(base_url: str, card_id: str, token: str, timeout: float = 10) -> bool:
    """Whether kabana lets this token read the card (board member or project manager)."""
    req = urllib.request.Request(f"{base_url.rstrip('/')}/api/cards/{card_id}",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return False


def cookie(header: str, name: str) -> str | None:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name and v:
            return urllib.request.unquote(v)
    return None


class Viewer:
    def __init__(self, bridge, roles: dict[str, str], check=None, card_url: str = "", title: str = ""):
        self.bridge = bridge
        self.roles = roles   # role -> pane name, in the order shown
        self.check = check   # token -> bool (blocking: run in a thread)
        self.card_url, self.title = card_url, title
        self.events: deque[dict] = deque(maxlen=KEEP)
        self.seq = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.tails: dict[str, LogTail] = {}
        self.auth: dict[str, tuple[bool, float]] = {}
        self.state: dict = {}
        self.server: asyncio.AbstractServer | None = None
        self.ctx: ssl.SSLContext | None = None
        self._cert: tuple[str, str] | None = None
        self._cert_mtime = 0.0
        self._cert_checked = 0.0
        self._task: asyncio.Task | None = None

    # ── events ──────────────────────────────────────────────────────────
    def emit(self, ev: dict) -> None:
        self.seq += 1
        ev = {"seq": self.seq, **ev}
        self.events.append(ev)
        for q in list(self.subscribers):
            if q.qsize() < 2000:
                q.put_nowait(ev)

    def bridge_line(self, line: str) -> None:
        """A line the bridge printed in its pane."""
        try:
            self.emit({"role": "bridge", "kind": "log", "text": _clip(line.rstrip(), 4000), "ts": time.time()})
        except Exception:
            pass   # the view must never break the relay

    def poll(self) -> None:
        for role in self.roles:
            path = self.bridge._role_log(role)
            if path is None:
                continue
            tail = self.tails.get(role)
            if tail is None or tail.path != path:
                tail = self.tails[role] = LogTail(path)
            for entry in tail.read():
                for ev in parse_entry(entry, role):
                    self.emit(ev)
        state = self._state()
        if state != self.state:
            self.state = state
            for q in list(self.subscribers):
                q.put_nowait({"kind": "state", **state})

    def _state(self) -> dict:
        b = self.bridge
        return {"status": b.status(), "busy": {r: bool(b.panes[r].busy) for r in self.roles},
                "held": bool(b.held), "finished": b.finished or None}

    async def _poll_loop(self) -> None:
        while True:
            try:
                self.poll()
            except Exception as e:   # a bad line or a log that moved: carry on
                self.bridge.log(f"viewer: {e}")
            await asyncio.sleep(POLL)

    # ── serving ─────────────────────────────────────────────────────────
    def _load_cert(self, cert: str, key: str) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        return ctx

    def _renew(self) -> None:
        """Load a renewed certificate (Let's Encrypt replaces it every couple of months) for new connections."""
        if not self._cert or time.monotonic() - self._cert_checked < CERT_CHECK:
            return
        self._cert_checked = time.monotonic()
        try:
            mtime = Path(self._cert[0]).stat().st_mtime
            if mtime != self._cert_mtime:
                self.ctx.load_cert_chain(*self._cert)
                self._cert_mtime = mtime
        except (OSError, ssl.SSLError) as e:
            self.bridge.log(f"viewer: couldn't reload the certificate: {e}")

    async def start(self, port: int | None, cert: str | None, key: str | None, host: str = "0.0.0.0") -> int:
        """Listen (TLS when given a certificate) on `port`, else the first free one in VIEWER_PORTS.
        Returns the port. Raises OSError when none is free."""
        if cert and key:
            self.ctx = self._load_cert(cert, key)
            self._cert, self._cert_mtime = (cert, key), Path(cert).stat().st_mtime
        tries = ([port] if port else []) + [p for p in VIEWER_PORTS if p != port]
        last: OSError | None = None
        for p in tries:
            try:
                self.server = await asyncio.start_server(self.handle, host, p, ssl=self.ctx)
                break
            except OSError as e:
                last = e
        else:
            raise last or OSError("no free port")
        self._task = asyncio.create_task(self._poll_loop())
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.server:
            self.server.close()

    async def _allowed(self, headers: dict) -> bool:
        token = cookie(headers.get("cookie", ""), "accessToken")
        if not token or not self.check:
            return False
        now = time.monotonic()
        hit = self.auth.get(token)
        if hit and hit[1] > now:
            return hit[0]
        ok = await asyncio.to_thread(self.check, token)
        if len(self.auth) > 500:
            self.auth.clear()
        self.auth[token] = (ok, now + AUTH_TTL)
        return ok

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            self._renew()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = (lines[0].split(" ") + ["", "", ""])[:3]
            headers = {}
            for line in lines[1:]:
                k, _, v = line.partition(":")
                if k:
                    headers[k.strip().lower()] = v.strip()
            path, _, query = target.partition("?")
            if method not in ("GET", "HEAD"):
                return await self._send(writer, 405, b"read-only\n", "text/plain")
            if path.endswith("/history"):
                if not await self._allowed(headers):
                    return await self._send(writer, 401, b'{"error":"sign in"}', "application/json")
                return await self._send(writer, 200, json.dumps(self._history()).encode(), "application/json")
            if path.endswith("/events"):
                if not await self._allowed(headers):
                    return await self._send(writer, 401, b'{"error":"sign in"}', "application/json")
                after = int(headers.get("last-event-id") or _query(query, "after") or 0)
                return await self._stream(writer, after)
            if not path.endswith("/"):
                return await self._send(writer, 301, b"", "text/plain", {"Location": path + "/"})
            return await self._send(writer, 200, PAGE.read_bytes(), "text/html; charset=utf-8")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _history(self) -> dict:
        return {"events": list(self.events), "state": self._state(), "roles": self.roles,
                "card": {"url": self.card_url, "title": self.title}}

    async def _send(self, writer, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        reason = {200: "OK", 301: "Moved Permanently", 401: "Unauthorized", 405: "Method Not Allowed"}[status]
        hdrs = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Connection": "close",
                **(extra or {})}
        writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode() + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode()
                     + b"\r\n" + body)
        await writer.drain()

    async def _stream(self, writer, after: int) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"X-Accel-Buffering: no\r\nConnection: keep-alive\r\n\r\nretry: 3000\n\n")
        q: asyncio.Queue = asyncio.Queue()
        for ev in self.events:   # whatever came after what the page already has
            if ev["seq"] > after:
                q.put_nowait(ev)
        self.subscribers.add(q)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), HEARTBEAT)
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                    await writer.drain()
                    continue
                data = json.dumps(ev)
                writer.write((f"id: {ev['seq']}\n" if "seq" in ev else "") .encode() + f"data: {data}\n\n".encode())
                await writer.drain()
        finally:
            self.subscribers.discard(q)


def _query(query: str, name: str) -> str | None:
    for part in query.split("&"):
        k, _, v = part.partition("=")
        if k == name:
            return v
    return None
