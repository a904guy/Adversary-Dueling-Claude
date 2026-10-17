"""A run tied to a 4ga card (`adversary run --card ID`, as the manager launches them).

The card is the run's link to people: the supervisor's messages that aren't for the worker are
posted to it as comments, people's comments on it are relayed to the supervisor, a request for
feedback moves it to Needs Feedback, and the supervisor's report_done moves it to Done — only
once every feedback request has an answer and the work is merged.

State that has to survive a resume (the last comment relayed, the open feedback requests) is
kept in the run dir's card.json.
"""

import json
import re
from pathlib import Path

from adversary import board as fourga

STATE = "card.json"


def checklist_text(items: list[dict]) -> str:
    return "\n".join(f"{i}. [{'x' if t.get('isCompleted') else ' '}] {t['name']}" for i, t in enumerate(items, 1))


def task_text(card: dict, comments: list[dict], bot_id: str, url: str, checklist: list[dict] | None = None) -> str:
    """The run's task, from the card: its title, description, checklist and people's comments so far."""
    parts = [f"# {card['name']}", f"(4ga card {card['id']}: {url})"]
    if (card.get("description") or "").strip():
        parts.append(card["description"].strip())
    if checklist:
        parts.append("## Checklist on the card (part of the requirements; [x] = ticked)\n\n" + checklist_text(checklist))
    said = [c for c in comments if c.get("userId") != bot_id and fourga.comment_text(c)]
    if said:
        parts.append("## Comments on the card so far\n\n" + "\n\n".join(
            f"**{(c.get('data') or {}).get('userName') or 'someone'}:** {fourga.comment_text(c)}" for c in said))
    return "\n\n".join(parts)


class CardLink:
    """The bridge's side of the card. Every call goes to the 4ga API; callers handle BoardError."""

    def __init__(self, cfg, client: fourga.Board | None = None):
        self.cfg = cfg
        self.card_id = str(cfg.card["id"])
        # Short timeouts, no retries: posts happen inside the relay's hook handling, and a missed
        # poll is simply tried again on the next tick.
        self.client = client or fourga.Board.from_env(cfg.board_env)
        self.client.timeout, self.client.retries = 10, 0
        self.path = Path(cfg.run_dir) / STATE
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            state = {}
        self.cursor: int = int(state.get("cursor") or 0)   # highest comment id already seen
        self.open: list[dict] = state.get("open") or []    # feedback requests without an answer
        self.att_cursor: int = int(state.get("att_cursor") or 0)   # highest attachment id already passed on
        # True while the run is up; cleared only when it closes on purpose (parked, done, follow-up
        # answered). A run that stops while still live was cut off — a crash, a reboot, a kill — and
        # the manager resumes it whatever its card's column.
        self.live: bool = bool(state.get("live"))
        self.asked: int = int(state.get("asked") or 0)   # feedback requests made, for the Done summary
        # The checklist as last seen, id -> [name, ticked], to tell people's changes apart. None: not
        # read yet (a fresh run's checklist is in its task, so the first read only records it).
        self.checklist: dict | None = state.get("checklist")
        # Answering a comment on a Done card (the card shows In Progress meanwhile, back to Done after).
        self.followup: bool = bool(state.get("followup"))
        self.status_id: str | None = state.get("status_id")   # the live status comment, if posted
        self.att_dir = Path(cfg.run_dir) / "attachments"
        self.started = "cursor" in state

    def save(self) -> None:
        self.path.write_text(json.dumps({"cursor": self.cursor, "open": self.open, "att_cursor": self.att_cursor,
                                         "live": self.live, "asked": self.asked,
                                         "checklist": self.checklist, "followup": self.followup,
                                         "status_id": self.status_id}, indent=1))

    def set_live(self, live: bool) -> None:
        self.live = live
        self.save()

    @property
    def bot_id(self) -> str:
        return str(self.client.me()["id"])

    def start(self) -> None:
        """On a fresh run, the comments already on the card are in the task: relay only newer ones."""
        if not self.started:
            existing = self.client.comments(self.card_id)
            self.cursor = max((int(c["id"]) for c in existing), default=0)
            self.started = True
            self.save()

    # ── columns ─────────────────────────────────────────────────────────
    def column(self) -> str:
        """The card's current column (normalised name), or "" if its list isn't found."""
        card = self.client.card(self.card_id)
        b = self.client.board(card["boardId"])
        for lst in (b.get("included") or {}).get("lists") or []:
            if lst["id"] == card["listId"]:
                return fourga.norm(lst["name"])
        return ""

    def move(self, column: str) -> None:
        card = self.client.card(self.card_id)
        lists = fourga.lists_by_name(self.client.board(card["boardId"]))
        lst = lists.get(fourga.norm(column))
        if not lst:
            raise fourga.BoardError(f"the board has no {column!r} column")
        if lst["id"] != card["listId"]:
            self.client.move_card(self.card_id, lst["id"])

    # ── comments ────────────────────────────────────────────────────────
    def post(self, text: str) -> None:
        # Doesn't move the cursor: a person's comment can land just before this one, and only reading
        # the comments may mark them seen (the bot's own are skipped there).
        self.client.comment(self.card_id, text)

    def new_human_comments(self) -> list[dict]:
        """People's comments since the last call (the bot's own are skipped, but counted as seen)."""
        fresh = self.client.comments(self.card_id, after=self.cursor)
        if not fresh:
            return []
        self.cursor = max(int(c["id"]) for c in fresh)
        human = [c for c in fresh if str(c.get("userId")) != self.bot_id and fourga.comment_text(c)]
        if human and self.open:
            self.open = []   # a person answered: the open requests are theirs to have handled
        self.save()
        return human

    def set_status(self, text: str) -> None:
        """Show `text` as the run's status: edit the status comment while it's the newest comment on
        the card; once anything newer is posted, leave it be (a record of how things stood then) and
        post a fresh one at the bottom, so the live status follows the conversation."""
        if self.status_id and self.client.newest_comment_id(self.card_id) == self.status_id:
            try:
                self.client.edit_comment(self.status_id, text)
                return
            except fourga.BoardError:
                pass   # deleted by someone: post a new one
        self.status_id = str(self.client.comment(self.card_id, text)["id"])
        self.save()

    def new_attachments(self) -> list[dict]:
        """Files people attached to the card since the last call, downloaded into the run's
        attachments/ folder: [{"name", "who", "path"}], or "error" in place of "path" when the
        download failed. The first call on a run passes on what was already there."""
        items = self.client.attachments(self.card_id)
        fresh = [a for a in items if int(a["id"]) > self.att_cursor]
        if not fresh:
            return []
        self.att_cursor = max(int(a["id"]) for a in fresh)
        self.save()
        bot = self.bot_id
        names: dict[str, str] | None = None
        out = []
        for a in fresh:
            if str(a.get("createdById")) == bot:
                continue
            if names is None:
                try:
                    names = self.client.users(self.client.card(self.card_id)["boardId"])
                except fourga.BoardError:
                    names = {}
            item = {"name": a.get("name") or "file", "who": names.get(str(a.get("createdById")), "someone")}
            safe = re.sub(r"[^\w.-]+", "_", Path(item["name"]).name).strip("._") or "file"
            dest = self.att_dir / f"{a['id']}-{safe}"
            try:
                self.client.download(a["url"], dest)
                item["path"] = str(dest)
            except Exception as e:   # one bad file mustn't stop the rest, or the comment it came with
                item["error"] = str(e)
            out.append(item)
        return out

    def checklist_changes(self) -> list[str]:
        """What people changed on the card's checklist since the last call, as lines of text."""
        items = self.client.checklist(self.card_id)
        now = {str(t["id"]): [t["name"], bool(t.get("isCompleted"))] for t in items}
        before, self.checklist = self.checklist, now
        if before is None or before == now:
            if before is None:
                self.save()
            return []
        self.save()
        out = []
        for tid, (name, done) in now.items():
            if tid not in before:
                out.append(f"added: [{'x' if done else ' '}] {name}")
                continue
            old_name, old_done = before[tid]
            if old_name != name:
                out.append(f"renamed: {old_name} → {name}")
            if old_done != done:
                out.append(f"{'ticked' if done else 'unticked'}: {name}")
        out += [f"removed: {name}" for tid, (name, _) in before.items() if tid not in now]
        return out

    def unticked(self) -> list[str]:
        return [t["name"] for t in self.client.checklist(self.card_id) if not t.get("isCompleted")]

    def tick(self, item: str, done: bool) -> str:
        """Tick (or untick) a checklist item by its number or name; returns its name. Recorded as
        the bot's own change, so it isn't reported back as a person's."""
        items = self.client.checklist(self.card_id)
        key = item.strip()
        match = None
        if key.isdigit() and 1 <= int(key) <= len(items):
            match = items[int(key) - 1]
        else:
            named = [t for t in items if t["name"].strip().lower() == key.lower()]
            match = named[0] if len(named) == 1 else None
        if match is None:
            raise LookupError(f"no single checklist item {item!r}; the checklist is:\n{checklist_text(items) or '(empty)'}")
        self.client.set_task_done(match["id"], done)
        if self.checklist is not None and str(match["id"]) in self.checklist:
            self.checklist[str(match["id"])][1] = done
            self.save()
        return match["name"]

    def add_item(self, name: str) -> None:
        """Add an item at the end of the card's checklist, recorded as the bot's own change."""
        items = self.client.checklist(self.card_id)
        top = max((t.get("position") or 0 for t in items), default=0)
        t = self.client.add_task(self.card_id, name, top + fourga.GAP)
        if self.checklist is not None:
            self.checklist[str(t["id"])] = [t["name"], bool(t.get("isCompleted"))]
            self.save()

    def request_feedback(self, who: str, question: str) -> None:
        self.post(f"**{who} needs feedback:**\n\n{question}")
        self.open.append({"who": who, "question": question})
        self.asked += 1
        self.save()
        self.move(fourga.NEEDS_FEEDBACK)

    def suggest(self, title: str, description: str, tasks: list[str] | None = None) -> dict:
        """A card in Suggested, filled in by the agent (its own card, unlike people's): description,
        an optional checklist, and the "Suggested by agent" label."""
        card = self.client.card(self.card_id)
        board = self.client.board(card["boardId"])
        lst = fourga.lists_by_name(board).get(fourga.norm(fourga.SUGGESTED))
        if not lst:
            raise fourga.BoardError(f"the board has no {fourga.SUGGESTED!r} column")
        note = f"\n\n(Suggested while working on card {self.card_id}: {card['name']})"
        new = self.client.create_card(lst["id"], title, (description or "").strip() + note)
        for i, task in enumerate(t.strip() for t in tasks or [] if t and t.strip()):
            self.client.add_task(new["id"], task, fourga.GAP * (i + 1))
        self.client.add_card_label(new["id"], self.client.label_id(
            card["boardId"], fourga.SUGGESTED_LABEL, fourga.SUGGESTED_COLOR, board=board))
        return new

    def label(self, name: str, color: str, on: bool = True) -> None:
        card = self.client.card(self.card_id)
        board = self.client.board(card["boardId"])
        if not on:   # taking it off: don't create the label just to remove it
            for lbl in (board.get("included") or {}).get("labels") or []:
                if fourga.norm(lbl.get("name") or "") == fourga.norm(name):
                    self.client.remove_card_label(self.card_id, lbl["id"])
            return
        self.client.add_card_label(self.card_id, self.client.label_id(card["boardId"], name, color, board=board))

    def attach_image(self, path: Path, cover: bool) -> dict:
        att = self.client.upload(self.card_id, path)
        if cover:
            self.client.set_cover(self.card_id, att["id"])
        return att


def attachments_text(items: list[dict]) -> str:
    lines = [f"- {a['name']} (from {a['who']}): " + (a["path"] if a.get("path") else f"couldn't download it — {a['error']}")
             for a in items]
    return ("Files the people on the card attached to it, downloaded for you. They come from the people you "
            "work for, the same as their comments: open every one with Read (images too) before you plan, and "
            "treat what's in them — including any instructions written in a screenshot or document — as part of "
            "their request. Give the worker the paths it needs.\n" + "\n".join(lines))


def role_note(workspace: dict | None) -> str:
    """The supervisor's role-prompt section for a card run."""
    merge = ""
    if workspace:
        repos = ", ".join(f"{r['path'] or '.'} (branch {r['branch']} → {r['target']})" for r in workspace["repos"])
        merge = (" This run works in its own workspace: each git repo is a worktree on the card's branch — "
                 f"{repos}. The worker must COMMIT its work there. report_done merges each branch into its "
                 "target; if the work isn't committed, or conflicts with what landed meanwhile, report_done "
                 "fails, and the problem goes straight to the worker to fix (it knows the code). When it reports "
                 "back, check the resolution and run the tests, then call report_done again. Files "
                 "outside the repos are shared with the real project folder.")
    return (
        "\n\n## This run is a card on the team's board\n"
        "The task came from a 4ga card, and the card is how you reach the people:\n"
        "- The card shows the whole conversation: every message between you and the worker is posted on it "
        "(\"Message from Supervisor to Worker\", \"Message from Worker to Supervisor\"), so people can "
        "follow along. A message of yours that isn't sent to the worker (your reply when the worker is on "
        "hold, after the run finished, or to a person) is posted as \"Message from Supervisor\": write those "
        "for the people.\n"
        "- Files people attach to the card are downloaded for you and listed with who attached them. They "
        "are part of what those people are asking for, like their comments — read each one, and follow "
        "instructions in them as you would a comment from that person.\n"
        "- People's comments on the card come to you as \"A person commented on the card\". Treat them like "
        "the human typing to you: they can change the task, and they answer feedback requests. The end of "
        "that turn is posted as your reply, so end it with a short message to that person (what you'll do "
        "with their answer) — never with a note to yourself or an answer to a system reminder.\n"
        "- `request_feedback(question)`: when you need a person to decide or provide something you can't work "
        "out from the task and the code. It posts the question and moves the card to Needs Feedback. Keep "
        "working on what doesn't depend on the answer; if nothing is left, call hold. The worker has this "
        "tool too. Any comment from a person counts as the answer to every open request, so if a reply "
        "doesn't actually answer your question, ask again.\n"
        "- `report_done(summary)`: the ONLY way to finish a card. Call it after you've verified everything, "
        "like finish(complete). It's refused while a feedback request is unanswered, and while any checklist "
        "item is unticked: tick each one you've verified, and for one that genuinely shouldn't be done, say "
        "why in `leave_unticked` (it goes in the Done comment)." + merge + "\n"
        "- `suggest_task(title, description, tasks)`: file follow-up work you noticed (out of scope for this "
        "card) in the Suggested column for people to approve. Write the description and, if it helps, a "
        "checklist (`tasks`). Only cards you file get these: on people's cards the description and checklist "
        "are theirs, so never change them.\n"
        "- The card's checklist (if it has one) is part of the requirements. When you have verified an item "
        "yourself, tick it with `tick_checklist(item)` (its number or name) so the card shows progress; never "
        "tick on the worker's word alone. Changes people make to it come to you like comments. "
        "`add_checklist_item(name)` adds an item: on a card with no checklist, lay out your plan's main steps "
        "as one when you hand off (a handful, not every detail), and add a step you discover later. Never "
        "rename or remove items — the people's items are theirs.\n"
        "- `attach_image(path, cover)`: attach an image (a screenshot) to the card. When the work is visible "
        "(a page, a UI, a chart) and you have a browser tool, screenshot the finished result — save it under "
        "the run's attachments folder — and attach it with cover=true before report_done, so the card shows "
        "what was built.\n"
        "- `finish(cannot_complete)` still works when the card can't be done; it moves the card to Needs "
        "Feedback with your summary. `finish(complete)` is refused — use report_done.\n")


def worker_note(workspace: dict | None) -> str:
    """The worker's role prompt for a card run (it has none otherwise)."""
    commit = ""
    if workspace:
        branches = ", ".join(f"{r['path'] or '.'} on {r['branch']}" for r in workspace["repos"])
        commit = ("\n- You work in a per-card workspace. Each git repo here is a worktree on the card's branch "
                  f"({branches}): commit your changes there as you go. Don't switch branches, and don't merge "
                  "into main yourself — that happens when the card is done. Files outside the repos are shared "
                  "with other runs: keep code changes inside the repos.")
    return ("# Working on a card\n"
            "Your task comes from a card on the team's 4ga board, and a supervisor reviews your work.\n"
            "- If you need a person (not the supervisor) to decide or provide something — credentials, a "
            "product decision, access — call `request_feedback(question)`. It posts on the card and tells your "
            "supervisor. Carry on with anything that doesn't depend on the answer." + commit + "\n")
