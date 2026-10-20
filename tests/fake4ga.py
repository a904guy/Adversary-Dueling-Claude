"""An in-memory stand-in for the 4ga Boards API routes the manager and bridge use.

It checks what the real server requires (X-Client headers, isGithubConnected on a board,
isCollapsed on a list, a position with every card move) so a missing field fails here.
"""

import itertools
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT, HUMAN = "100", "200"
CLIENT_ID, SECRET = "cid", "secret"


class Fake4ga:
    def __init__(self):
        self.ids = itertools.count(1000)
        self.projects: dict[str, dict] = {}
        self.boards: dict[str, dict] = {}
        self.lists: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.comments: dict[str, dict] = {}
        self.users = {"pat@example.com": HUMAN}
        self.managers: list[dict] = []        # {"projectId", "userId"}
        self.memberships: list[dict] = []     # {"boardId", "userId", "role"}
        self.attachments: dict[str, dict] = {}
        self.labels: dict[str, dict] = {}
        self.tasks: list[dict] = []
        self.card_labels: list[dict] = []     # {"cardId", "labelId"}
        self.files: dict[str, bytes] = {}
        self.allow_project_create = True
        self.calls: list[tuple[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def nid(self) -> str:
        return str(next(self.ids))

    # helpers for tests
    def add_project(self, name, creator=None):
        p = {"id": self.nid(), "name": name}
        self.projects[p["id"]] = p
        if creator:
            self.managers.append({"projectId": p["id"], "userId": creator})
        return p

    def add_board(self, project_id, name, lists=()):
        b = {"id": self.nid(), "projectId": project_id, "name": name, "position": 65535}
        self.boards[b["id"]] = b
        for i, n in enumerate(lists):
            self.add_list(b["id"], n, 65535 * (i + 1))
        return b

    def add_list(self, board_id, name, position):
        lst = {"id": self.nid(), "boardId": board_id, "name": name, "position": position}
        self.lists[lst["id"]] = lst
        return lst

    def list_named(self, board_id, name):
        return next(l for l in self.lists.values() if l["boardId"] == board_id and l["name"] == name)

    def add_card(self, list_id, name, description=None, position=65535):
        lst = self.lists[list_id]
        c = {"id": self.nid(), "listId": list_id, "boardId": lst["boardId"], "name": name,
             "description": description, "position": position}
        self.cards[c["id"]] = c
        return c

    def add_comment(self, card_id, text, user=HUMAN, name="Pat"):
        c = {"id": self.nid(), "cardId": card_id, "userId": user, "data": {"text": text, "userName": name}}
        self.comments[c["id"]] = c
        return c

    def add_attachment(self, card_id, name, data: bytes, user=HUMAN):
        a = {"id": self.nid(), "cardId": card_id, "name": name, "createdById": user}
        a["url"] = f"{self.url}/attachments/{a['id']}/download/{name}"
        self.attachments[a["id"]] = a
        self.files[a["id"]] = data
        return a

    def add_task(self, card_id, name, done=False, user=HUMAN):
        t = {"id": self.nid(), "cardId": card_id, "name": name, "position": 65535 * (len(self.tasks) + 1),
             "isCompleted": done}
        self.tasks.append(t)
        return t

    def has_label(self, card_id, name):
        return any(cl["cardId"] == card_id and self.labels[cl["labelId"]]["name"] == name for cl in self.card_labels)

    def column_of(self, card_id):
        return self.lists[self.cards[card_id]["listId"]]["name"]

    # the HTTP side
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send_bytes(self, data):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _do(self, method):
                if self.headers.get("X-Client-Id") != CLIENT_ID or self.headers.get("X-Client-Secret") != SECRET:
                    return self._send(401, {"code": "E_UNAUTHORIZED"})
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if (self.headers.get("Content-Type") or "").startswith("multipart/form-data"):
                    body = {"__raw__": raw}
                else:
                    body = json.loads(raw) if raw else {}
                path, _, query = self.path.partition("?")
                fake.calls.append((method, path))
                if method == "GET" and (r := re.fullmatch(r"/attachments/(\d+)/download/.+", path)):
                    return self._send_bytes(fake.files[r.group(1)])
                try:
                    code, obj = fake.route(method, path, query, body)
                except KeyError:
                    code, obj = 404, {"code": "E_NOT_FOUND"}
                self._send(code, obj)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PATCH(self):
                self._do("PATCH")

            def do_DELETE(self):
                self._do("DELETE")

        return H

    def route(self, method, path, query, body):
        m = lambda pat: re.fullmatch(pat, path)
        if method == "GET" and path == "/api/users/me":
            return 200, {"item": {"id": BOT, "name": "Claude"}}
        if method == "GET" and path == "/api/projects":
            return 200, {"items": list(self.projects.values()), "included": {
                "boards": list(self.boards.values()), "projectManagers": self.managers}}
        if method == "POST" and path == "/api/users/find-by-email":
            uid = self.users.get(body["query"].lower())
            return 200, {"item": {"id": uid, "email": body["query"]} if uid else None}
        if method == "POST" and (r := m(r"/api/projects/(\d+)/managers")):
            if any(x == {"projectId": r.group(1), "userId": body["userId"]} for x in self.managers):
                return 409, {"code": "E_CONFLICT"}
            self.managers.append({"projectId": r.group(1), "userId": body["userId"]})
            for b in self.boards.values():   # 4ga makes a new manager an editor on the existing boards
                if b["projectId"] == r.group(1) and not any(
                        x["boardId"] == b["id"] and x["userId"] == body["userId"] for x in self.memberships):
                    self.memberships.append({"boardId": b["id"], "userId": body["userId"], "role": "editor"})
            return 200, {"item": {}}
        if method == "POST" and (r := m(r"/api/boards/(\d+)/memberships")):
            if "role" not in body:
                return 422, {"code": "E_MISSING_OR_INVALID_PARAMS"}
            if any(x["boardId"] == r.group(1) and x["userId"] == body["userId"] for x in self.memberships):
                return 409, {"code": "E_CONFLICT"}
            self.memberships.append({"boardId": r.group(1), "userId": body["userId"], "role": body["role"]})
            return 200, {"item": {}}
        if method == "POST" and path == "/api/projects":
            if not self.allow_project_create:
                return 403, {"code": "E_FORBIDDEN"}
            return 200, {"item": self.add_project(body["name"], creator=BOT)}
        if method == "POST" and (r := m(r"/api/projects/(\d+)/boards")):
            if "isGithubConnected" not in body or "position" not in body:
                return 422, {"code": "E_MISSING_OR_INVALID_PARAMS"}
            b = self.add_board(r.group(1), body["name"])
            self.memberships.append({"boardId": b["id"], "userId": BOT, "role": "editor"})
            for lst in body.get("lists") or []:
                if "isCollapsed" not in lst:
                    return 422, {"code": "E_MISSING_OR_INVALID_PARAMS"}
                self.add_list(b["id"], lst["name"], lst["position"])
            return 200, {"item": b}
        if method == "GET" and (r := m(r"/api/boards/(\d+)")):
            b = self.boards[r.group(1)]
            return 200, {"item": b, "included": {
                "boardMemberships": [x for x in self.memberships if x["boardId"] == b["id"]],
                "users": [{"id": HUMAN, "name": "Pat"}, {"id": BOT, "name": "Claude"}],
                "labels": [l for l in self.labels.values() if l["boardId"] == b["id"]],
                "cardLabels": [cl for cl in self.card_labels if self.cards.get(cl["cardId"], {}).get("boardId") == b["id"]],
                "lists": [l for l in self.lists.values() if l["boardId"] == b["id"]],
                "cards": [c for c in self.cards.values() if c["boardId"] == b["id"]]}}
        if method == "POST" and (r := m(r"/api/boards/(\d+)/lists")):
            if not any(x["boardId"] == r.group(1) and x["userId"] == BOT and x["role"] == "editor"
                       for x in self.memberships):
                return 404, {"code": "E_NOT_FOUND"}   # 4ga's answer to a non-editor
            if "isCollapsed" not in body or "position" not in body:
                return 422, {"code": "E_MISSING_OR_INVALID_PARAMS"}
            return 200, {"item": self.add_list(r.group(1), body["name"], body["position"])}
        if method == "GET" and (r := m(r"/api/cards/(\d+)")):
            return 200, {"item": self.cards[r.group(1)], "included": {
                "attachments": [a for a in self.attachments.values() if a["cardId"] == r.group(1)],
                "tasks": [t for t in self.tasks if t["cardId"] == r.group(1)]}}
        if method == "PATCH" and (r := m(r"/api/tasks/(\d+)")):
            t = next(t for t in self.tasks if t["id"] == r.group(1))
            t.update({k: v for k, v in body.items() if k in ("isCompleted", "name")})
            return 200, {"item": t}
        if method == "POST" and (r := m(r"/api/lists/(\d+)/cards")):
            return 200, {"item": self.add_card(r.group(1), body["name"], body.get("description"))}
        if method == "PATCH" and (r := m(r"/api/cards/(\d+)")):
            c = self.cards[r.group(1)]
            if "listId" in body:
                if "position" not in body:
                    return 422, {"code": "E_UNPROCESSABLE_ENTITY", "message": "positionMustBePresent"}
                c["listId"], c["boardId"] = body["listId"], self.lists[body["listId"]]["boardId"]
            if "timer" in body:
                t = body["timer"]
                if not isinstance(t, dict) or set(t) != {"startedAt", "total"} or not isinstance(t["total"], (int, float)):
                    return 422, {"code": "E_MISSING_OR_INVALID_PARAMS"}
            c.update({k: v for k, v in body.items() if k in ("position", "name", "description", "timer",
                                                             "coverAttachmentId")})
            return 200, {"item": c}
        if method == "GET" and (r := m(r"/api/cards/(\d+)/comments")):
            before = int(query.split("=")[1]) if query.startswith("beforeId=") else None
            items = sorted((c for c in self.comments.values() if c["cardId"] == r.group(1)
                            and (before is None or int(c["id"]) < before)), key=lambda c: -int(c["id"]))
            return 200, {"items": items[:50], "included": {}}
        if method == "PATCH" and (r := m(r"/api/comments/(\d+)")):
            c = self.comments[r.group(1)]
            if c["userId"] != BOT:
                return 403, {"code": "E_FORBIDDEN"}
            c["data"]["text"] = body["text"]
            self.edits = getattr(self, "edits", 0) + 1
            return 200, {"item": c}
        if method == "DELETE" and (r := m(r"/api/comments/(\d+)")):
            self.comments.pop(r.group(1))
            return 200, {"item": {}}
        if method == "POST" and (r := m(r"/api/cards/(\d+)/comments")):
            return 200, {"item": self.add_comment(r.group(1), body["text"], user=BOT, name="Claude")}
        if method == "POST" and (r := m(r"/api/cards/(\d+)/tasks")):
            t = {"id": self.nid(), "cardId": r.group(1), "name": body["name"], "position": body.get("position"),
                 "isCompleted": bool(body.get("isCompleted"))}
            self.tasks.append(t)
            return 200, {"item": t}
        if method == "POST" and (r := m(r"/api/cards/(\d+)/attachments")):
            raw = body.get("__raw__", b"")
            name = re.search(rb'filename="([^"]+)"', raw)
            a = self.add_attachment(r.group(1), name.group(1).decode() if name else "file", raw, user=BOT)
            return 200, {"item": a}
        if method == "POST" and (r := m(r"/api/boards/(\d+)/labels")):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", body.get("color") or ""):
                return 422, {"code": "E_MISSING_OR_INVALID_PARAMS"}
            lbl = {"id": self.nid(), "boardId": r.group(1), "name": body["name"], "color": body["color"]}
            self.labels[lbl["id"]] = lbl
            return 200, {"item": lbl}
        if method == "PATCH" and (r := m(r"/api/labels/(\d+)")):
            if r.group(1) not in self.labels:
                return 404, {"code": "E_NOT_FOUND"}
            self.labels[r.group(1)].update({k: body[k] for k in ("name", "color") if k in body})
            return 200, {"item": self.labels[r.group(1)]}
        if method == "POST" and (r := m(r"/api/cards/(\d+)/labels")):
            cl = {"cardId": r.group(1), "labelId": body["labelId"]}
            if cl in self.card_labels:
                return 409, {"code": "E_CONFLICT"}
            self.card_labels.append(cl)
            return 200, {"item": cl}
        if method == "DELETE" and (r := m(r"/api/cards/(\d+)/labels/(\d+)")):
            cl = {"cardId": r.group(1), "labelId": r.group(2)}
            if cl not in self.card_labels:
                return 404, {"code": "E_NOT_FOUND"}
            self.card_labels.remove(cl)
            return 200, {"item": cl}
        return 404, {"code": "E_NOT_FOUND"}
