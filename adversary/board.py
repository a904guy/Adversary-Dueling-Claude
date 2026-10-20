"""A small client for 4ga Boards (https://github.com/RARgames/4gaBoards), dependency-free.

An API client (Settings → API in 4ga) authenticates with X-Client-Id / X-Client-Secret headers
on every request and acts as the user who created it. Use a dedicated bot user: 4ga records only
the user on a comment, so the bot's own comments are told apart from people's by its user id.

Credentials come from the environment, or from an env file (KEY=value lines, default
~/.config/adversary/4ga.env, readable only by you):

    FOURGA_URL=https://boards.example.com
    FOURGA_CLIENT_ID=...
    FOURGA_CLIENT_SECRET=...
    FOURGA_MEMBERS=you@example.com,teammate@example.com   # people the manager keeps on the project
"""

import json
import mimetypes
import os
import time
import uuid
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_ENV = Path.home() / ".config" / "adversary" / "4ga.env"

# The columns the manager works with, in board order. "Upcoming" is for people to think out loud
# in; the manager never touches a card there.
UPCOMING, SUGGESTED, APPROVED, IN_PROGRESS, NEEDS_FEEDBACK, DONE = (
    "Upcoming", "Suggested", "Approved To Do", "In Progress", "Needs Feedback", "Done")
COLUMNS = [UPCOMING, SUGGESTED, APPROVED, IN_PROGRESS, NEEDS_FEEDBACK, DONE]
ACTIVE = [SUGGESTED, APPROVED, IN_PROGRESS, NEEDS_FEEDBACK, DONE]
GAP = 65535   # 4ga's position gap
# The card label the manager keeps on every card with a run attached, so people can see which are.
ACTIVE_LABEL, ACTIVE_COLOR = "Active", "#2e7d32"
# On a card that needs a person to act before it can move on (the manager gave up on it, or its work
# can't be merged without someone).
ATTENTION_LABEL, ATTENTION_COLOR = "Needs attention", "#c62828"
# Where a card came from: the supervisor files its own in Suggested, and any other card that reaches
# Approved To Do was put there by a person, so the manager labels it when it sees it there.
SUGGESTED_LABEL, SUGGESTED_COLOR = "Suggested by Agent", "#6a1b9a"
HUMAN_LABEL, HUMAN_COLOR = "Suggested by Human", "#1565c0"


class BoardError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def load_env(path: str | os.PathLike | None = None) -> dict[str, str]:
    """FOURGA_* settings: the env file's, overridden by the environment's."""
    out: dict[str, str] = {}
    p = Path(path).expanduser() if path else DEFAULT_ENV
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        if path:
            raise BoardError(f"can't read the 4ga credentials file {p}")
    out.update({k: v for k, v in os.environ.items() if k.startswith("FOURGA_")})
    missing = [k for k in ("FOURGA_URL", "FOURGA_CLIENT_ID", "FOURGA_CLIENT_SECRET") if not out.get(k)]
    if missing:
        raise BoardError(f"4ga credentials missing: {', '.join(missing)} (set them in {p} or the environment)")
    return out


# 4ga stores its built-in columns by translation key: a board made from the "Empty" template has one
# list named "common.done", shown as "Done".
_ALIASES = {"common.done": "done"}


def norm(name: str) -> str:
    """Column names compare without case or spacing ("Needs feedback" is "Needs Feedback")."""
    n = " ".join((name or "").lower().split())
    return _ALIASES.get(n, n)


class Board:
    """The 4ga REST API calls the manager and the bridge need."""

    def __init__(self, url: str, client_id: str, client_secret: str, timeout: float = 20, retries: int = 2):
        self.url = url.rstrip("/")
        self.headers = {"X-Client-Id": client_id, "X-Client-Secret": client_secret,
                        "Content-Type": "application/json", "Accept": "application/json"}
        self.timeout, self.retries = timeout, retries
        self._me: dict | None = None

    @classmethod
    def from_env(cls, path: str | None = None) -> "Board":
        env = load_env(path)
        return cls(env["FOURGA_URL"], env["FOURGA_CLIENT_ID"], env["FOURGA_CLIENT_SECRET"])

    # ── transport ───────────────────────────────────────────────────────
    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code < 500:   # our request is wrong (or not allowed): retrying won't help
                    raise BoardError(f"{method} {path}: HTTP {e.code} {detail}", e.code) from None
                last = BoardError(f"{method} {path}: HTTP {e.code} {detail}", e.code)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last = BoardError(f"{method} {path}: {e}")
            if attempt < self.retries:
                time.sleep(1 + attempt)
        raise last  # type: ignore[misc]

    # ── who we are ──────────────────────────────────────────────────────
    def me(self) -> dict:
        if self._me is None:
            self._me = self.call("GET", "/api/users/me")["item"]
        return self._me

    # ── projects / boards / lists ───────────────────────────────────────
    def projects(self) -> dict:
        return self.call("GET", "/api/projects")

    def create_project(self, name: str) -> dict:
        try:
            return self.call("POST", "/api/projects", {"name": name})["item"]
        except BoardError as e:
            if e.status in (401, 403):
                raise BoardError(f"the bot user may not create projects ({e}). In 4ga, make it an admin, turn on "
                                 "project creation for everyone, or create the project yourself and add the bot "
                                 "as a project manager.", e.status) from None
            raise

    def create_board(self, project_id: str, name: str, position: float, lists: list[str]) -> dict:
        body = {"name": name, "position": position, "isGithubConnected": False,
                "lists": [{"name": n, "position": GAP * (i + 1), "isCollapsed": False,
                           "isCompleted": norm(n) == norm(DONE)} for i, n in enumerate(lists)]}
        return self.call("POST", f"/api/projects/{project_id}/boards", body)["item"]

    def board(self, board_id: str) -> dict:
        """The board with its lists and cards: {"item": board, "included": {"lists", "cards", ...}}."""
        return self.call("GET", f"/api/boards/{board_id}")

    def create_list(self, board_id: str, name: str, position: float) -> dict:
        return self.call("POST", f"/api/boards/{board_id}/lists",
                         {"name": name, "position": position, "isCollapsed": False,
                          "isCompleted": norm(name) == norm(DONE)})["item"]

    # ── people ──────────────────────────────────────────────────────────
    def find_user(self, email: str) -> str | None:
        item = self.call("POST", "/api/users/find-by-email", {"query": email}).get("item")
        return str(item["id"]) if item else None

    def add_project_manager(self, project_id: str, user_id: str) -> None:
        """Make a person a manager of the project (4ga also makes them an editor on its boards)."""
        try:
            self.call("POST", f"/api/projects/{project_id}/managers", {"userId": user_id})
        except BoardError as e:
            if e.status != 409:   # already one
                raise

    def add_board_member(self, board_id: str, user_id: str) -> None:
        try:
            self.call("POST", f"/api/boards/{board_id}/memberships", {"userId": user_id, "role": "editor"})
        except BoardError as e:
            if e.status != 409:
                raise

    # ── labels ──────────────────────────────────────────────────────────
    def label_id(self, board_id: str, name: str, color: str, board: dict | None = None) -> str:
        """The board's label called `name`, created (in `color`) if it has none."""
        board = board or self.board(board_id)
        if lbl := find_label(board, name):
            if lbl.get("name") != name:   # spelt differently (an older name): bring it in line
                try:
                    lbl["name"] = self.rename_label(lbl["id"], name)["name"]
                except BoardError:
                    pass
            return lbl["id"]
        lbl = self.create_label(board_id, name, color)
        board.setdefault("included", {}).setdefault("labels", []).append(lbl)
        return lbl["id"]

    def create_label(self, board_id: str, name: str, color: str) -> dict:
        return self.call("POST", f"/api/boards/{board_id}/labels", {"name": name, "color": color})["item"]

    def rename_label(self, label_id: str, name: str) -> dict:
        return self.call("PATCH", f"/api/labels/{label_id}", {"name": name})["item"]

    def add_card_label(self, card_id: str, label_id: str) -> None:
        try:
            self.call("POST", f"/api/cards/{card_id}/labels", {"labelId": label_id})
        except BoardError as e:
            if e.status != 409:   # already on it
                raise

    def remove_card_label(self, card_id: str, label_id: str) -> None:
        try:
            self.call("DELETE", f"/api/cards/{card_id}/labels/{label_id}")
        except BoardError as e:
            if e.status != 404:   # already off
                raise

    # ── cards ───────────────────────────────────────────────────────────
    def card(self, card_id: str) -> dict:
        return self.call("GET", f"/api/cards/{card_id}")["item"]

    def create_card(self, list_id: str, name: str, description: str | None = None) -> dict:
        body: dict = {"name": name}
        if description:
            body["description"] = description
        return self.call("POST", f"/api/lists/{list_id}/cards", body)["item"]

    def move_card(self, card_id: str, list_id: str, position: float | None = None) -> dict:
        """Move a card to another list (top of it by default). 4ga wants a position with every move."""
        return self.call("PATCH", f"/api/cards/{card_id}",
                         {"listId": list_id, "position": GAP / 2 if position is None else position})["item"]

    def checklist(self, card_id: str) -> list[dict]:
        """The card's checklist ({"id", "name", "isCompleted", "position"}; 4ga calls them tasks), in order."""
        items = (self.call("GET", f"/api/cards/{card_id}").get("included") or {}).get("tasks") or []
        return sorted(items, key=lambda t: t.get("position") or 0)

    def set_task_done(self, task_id: str, done: bool) -> None:
        self.call("PATCH", f"/api/tasks/{task_id}", {"isCompleted": done})

    def attachments(self, card_id: str) -> list[dict]:
        """The card's attachments ({"id", "name", "url", "createdById", ...}), oldest first."""
        items = (self.call("GET", f"/api/cards/{card_id}").get("included") or {}).get("attachments") or []
        return sorted(items, key=lambda a: int(a["id"]))

    def users(self, board_id: str) -> dict[str, str]:
        """user id -> name, for the people on a board."""
        inc = self.board(board_id).get("included") or {}
        return {str(u["id"]): u.get("name") or u.get("username") or "someone" for u in inc.get("users") or []}

    def download(self, url: str, dest: Path, limit: int = 50 * 1024 * 1024) -> int:
        """Save an attachment (its absolute url, or a path on the server) to `dest`; returns bytes written."""
        full = url if url.startswith(("http://", "https://")) else self.url + url
        parts = urllib.parse.urlsplit(full)   # file names can hold spaces and other characters
        full = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(urllib.parse.unquote(parts.path))))
        headers = {k: v for k, v in self.headers.items() if k.startswith("X-Client")}
        req = urllib.request.Request(full, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                dest.parent.mkdir(parents=True, exist_ok=True)
                n = 0
                with dest.open("wb") as f:
                    while chunk := r.read(1 << 20):
                        n += len(chunk)
                        if n > limit:
                            raise BoardError(f"{dest.name} is over {limit // (1 << 20)} MB; not downloaded")
                        f.write(chunk)
                return n
        except urllib.error.HTTPError as e:
            raise BoardError(f"download {full}: HTTP {e.code}", e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise BoardError(f"download {full}: {e}") from None

    # ── card extras: timer, checklist, files ────────────────────────────
    def set_timer(self, card_id: str, started_at: str | None, total: float) -> None:
        """4ga's card timer: running while `started_at` is set; `total` is seconds before that."""
        self.call("PATCH", f"/api/cards/{card_id}", {"timer": {"startedAt": started_at, "total": round(total)}})

    def add_task(self, card_id: str, name: str, position: float) -> dict:
        """A checklist item on the card (4ga calls them tasks)."""
        return self.call("POST", f"/api/cards/{card_id}/tasks", {"name": name, "position": position})["item"]

    def upload(self, card_id: str, path: Path) -> dict:
        """Attach a file to the card."""
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers = {k: v for k, v in self.headers.items() if k.startswith("X-Client")}
        headers.update({"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"})
        req = urllib.request.Request(f"{self.url}/api/cards/{card_id}/attachments", data=body, method="POST",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                return json.loads(r.read())["item"]
        except urllib.error.HTTPError as e:
            raise BoardError(f"upload {path.name}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}", e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            raise BoardError(f"upload {path.name}: {e}") from None

    def set_cover(self, card_id: str, attachment_id: str) -> None:
        self.call("PATCH", f"/api/cards/{card_id}", {"coverAttachmentId": attachment_id})

    # ── comments ────────────────────────────────────────────────────────
    def comments(self, card_id: str, after: int = 0) -> list[dict]:
        """The card's comments with an id above `after`, oldest first (4ga pages 50 at a time,
        newest first; ids grow over time)."""
        out: list[dict] = []
        before = None
        while True:
            q = f"?beforeId={before}" if before else ""
            items = self.call("GET", f"/api/cards/{card_id}/comments{q}").get("items") or []
            fresh = [c for c in items if int(c["id"]) > after]
            out += fresh
            if len(fresh) < len(items) or len(items) < 50:
                break
            before = items[-1]["id"]
        return sorted(out, key=lambda c: int(c["id"]))

    def comment(self, card_id: str, text: str) -> dict:
        return self.call("POST", f"/api/cards/{card_id}/comments", {"text": text})["item"]

    def edit_comment(self, comment_id: str, text: str) -> None:
        self.call("PATCH", f"/api/comments/{comment_id}", {"text": text})

    def delete_comment(self, comment_id: str) -> None:
        try:
            self.call("DELETE", f"/api/comments/{comment_id}")
        except BoardError as e:
            if e.status != 404:   # already gone
                raise

    def newest_comment_id(self, card_id: str) -> str | None:
        items = self.call("GET", f"/api/cards/{card_id}/comments").get("items") or []
        return str(max(items, key=lambda c: int(c["id"]))["id"]) if items else None


def comment_text(c: dict) -> str:
    return ((c.get("data") or {}).get("text") or "").strip()


def find_label(board: dict, name: str) -> dict | None:
    """The board's label called `name` (compared like column names), or None."""
    return next((l for l in (board.get("included") or {}).get("labels") or []
                 if norm(l.get("name") or "") == norm(name)), None)


def lists_by_name(board: dict) -> dict[str, dict]:
    """A board's lists keyed by normalised name (the first wins if two share one)."""
    out: dict[str, dict] = {}
    for lst in sorted((board.get("included") or {}).get("lists") or [], key=lambda l: l.get("position") or 0):
        out.setdefault(norm(lst["name"]), lst)
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def card_url(base: str, card_id: str) -> str:
    return f"{base.rstrip('/')}/cards/{card_id}"
