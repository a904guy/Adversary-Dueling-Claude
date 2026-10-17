"""`adversary manage`: keep a pool of adversary runs working a 4ga project's cards.

Every few seconds the manager reads the project's boards and:

- creates what's missing: the project (by name), a board, and on a board with no columns the six
  the manager works with (Upcoming, Suggested, Approved To Do, In Progress, Needs Feedback, Done);
- while the pool has room, claims the top card in Approved To Do: moves it to In Progress, builds
  its workspace (a worktree per git repo, see workspace) and starts a run on it (`adversary run
  --card`, detached), which posts to the card and moves it on from there;
- resumes a card's run that closed while the card still needs it: one that crashed while In
  Progress, or one parked in Needs Feedback once a person has replied;
- stops a run whose card was pulled back (to Upcoming or Suggested) or deleted, and removes a
  Done card's workspace once its run has closed.

Only boards with an "Approved To Do" column are worked; Upcoming is never touched. The manager
keeps no state of its own: each run's config records its card, so a restarted manager picks up
where it left off. One manager per project: a lock file enforces it.
"""

import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from adversary import board as fourga
from adversary import cardmode, limits, sessions, workspace
from adversary.config import RunConfig

MAX_FAILS = 3          # launch/resume failures on one card before it's handed to people
FAIL_WINDOW = 1800     # seconds those failures are counted over


@dataclass
class CardView:
    card: dict
    column: str          # normalised list name
    board: dict          # the board's {"item", "included"}
    lists: dict          # normalised name -> list


def _slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip("-") or "project"


class Manager:
    def __init__(self, project: str | None, folder: str, pool: int, client: fourga.Board, state_dir: Path,
                 run_args: list[str] | None = None, board_env: str | None = None,
                 workspaces: Path | None = None, log=print, launcher=None, alive=None,
                 members: list[str] | None = None, done_window_days: float = 30,
                 project_id: str | None = None, tag: str = ""):
        if not (project or project_id):
            raise ValueError("a project needs a name or an id")
        self.project_name = project or ""   # filled in from the board when given by id
        self.project_id = str(project_id) if project_id else None
        self.key = _slug(self.project_id or self.project_name)   # names its lock and workspaces folder
        self.folder = str(Path(folder).resolve())
        self.pool = pool
        self.client = client
        self.state_dir = Path(state_dir)
        self.run_args = run_args or []
        self.board_env = board_env
        self.workspaces = Path(workspaces) if workspaces else self.state_dir.parent / "workspaces" / self.key
        self.tag = tag                 # shown before each log line when several projects share one log
        self.capacity = None           # set by a Fleet: free slots in the pool all its projects share
        self.live_count = 0            # this project's live runs, as of its last pass
        self.last_error = ""
        self.log_fn = log
        self.launcher = launcher or self._adversary   # runs `adversary run|resume …`; returns its output
        self.alive = alive or _tmux_alive
        self.members = [m.strip() for m in members or [] if m.strip()]   # emails kept on the project
        self.done_window = done_window_days * 86400   # how long after Done a comment still resumes the card
        self._member_ids: dict[str, str] | None = None
        self.fails: dict[str, list[float]] = {}

    def log(self, msg: str) -> None:
        self.log_fn(f"{time.strftime('%H:%M:%S')} " + (f"[{self.tag}] " if self.tag else "") + msg)

    # ── the board ───────────────────────────────────────────────────────
    def member_ids(self) -> dict[str, str]:
        """email -> 4ga user id for the people kept on the project (looked up once)."""
        if self._member_ids is None:
            ids = {}
            for email in self.members:
                uid = self.client.find_user(email)
                if not uid:
                    raise fourga.BoardError(f"no 4ga user with the email {email!r} (they need an account first)")
                ids[email] = uid
            self._member_ids = ids
        return self._member_ids

    def ensure_project(self) -> tuple[dict, list[dict]]:
        data = self.client.projects()
        inc = data.get("included") or {}
        for p in data.get("items") or []:
            if (str(p["id"]) == self.project_id if self.project_id
                    else fourga.norm(p["name"]) == fourga.norm(self.project_name)):
                self.project_name = p["name"]
                boards = [b for b in inc.get("boards") or [] if b["projectId"] == p["id"]]
                managers = {str(m["userId"]) for m in inc.get("projectManagers") or [] if m["projectId"] == p["id"]}
                self._ensure_managers(p, managers)
                return p, boards
        if self.project_id:   # 4ga picks project ids itself: one given by id can only be found
            raise fourga.BoardError(f"project {self.project_id} isn't visible to the bot: in 4ga, add it to the "
                                    "project as a project manager")
        p = self.client.create_project(self.project_name)
        self.log(f"created project {self.project_name!r}")
        self._ensure_managers(p, {str(self.client.me()["id"])})
        return p, []

    def _ensure_managers(self, project: dict, have: set[str]) -> None:
        """People must always be able to see and run the project, not only the bot."""
        for email, uid in self.member_ids().items():
            if uid not in have:
                self.client.add_project_manager(project["id"], uid)
                self.log(f"added {email} as a manager of {project['name']!r}")

    def _ensure_board_members(self, full: dict) -> None:
        have = {str(m["userId"]) for m in (full.get("included") or {}).get("boardMemberships") or []}
        me = str(self.client.me()["id"])
        if me not in have:   # a board someone else made: the bot sees it as a project manager, but can't edit it
            self.client.add_board_member(full["item"]["id"], me)
            self.log(f"joined the board {full['item']['name']!r} as an editor")
        for email, uid in self.member_ids().items():
            if uid not in have:
                self.client.add_board_member(full["item"]["id"], uid)
                self.log(f"added {email} to the board {full['item']['name']!r}")

    def ensure_boards(self) -> list[dict]:
        """The project's worked boards, each as GET /api/boards/:id, after creating what's missing."""
        project, boards = self.ensure_project()
        if not boards:
            b = self.client.create_board(project["id"], self.project_name, fourga.GAP, fourga.COLUMNS)
            self.log(f"created board {b['name']!r} with the columns {', '.join(fourga.COLUMNS)}")
            boards = [b]
        out = []
        for b in sorted(boards, key=lambda b: b.get("position") or 0):
            full = self.client.board(b["id"])
            self._ensure_board_members(full)   # the bot too: only a board's editors may change it
            lists = fourga.lists_by_name(full)
            cards = (full.get("included") or {}).get("cards") or []
            if not cards and set(lists) <= {fourga.norm(fourga.DONE)}:
                # A new board: no columns, or only the Done that 4ga's "Empty" template starts with.
                self._add_columns(b, lists)
                full = self.client.board(b["id"])
                lists = fourga.lists_by_name(full)
            if any(fourga.norm(n) not in lists for n in fourga.ACTIVE):
                self._fill_columns(b, lists)
                full = self.client.board(b["id"])
            out.append(full)
        return out

    def _fill_columns(self, board: dict, lists: dict) -> None:
        """Add the manager's columns a board is missing, each just before the next of them it has
        (Done last), leaving the board's own columns, Upcoming among them, where they are."""
        taken = sorted(l.get("position") or 0 for l in lists.values())
        have = {n: lists[fourga.norm(n)].get("position") or 0 for n in fourga.ACTIVE if fourga.norm(n) in lists}
        added = []
        for i, name in enumerate(fourga.ACTIVE):
            if name in have:
                continue
            later = [have[n] for n in fourga.ACTIVE[i + 1:] if n in have]
            if later:
                nxt = min(later)
                pos = (max((p for p in taken if p < nxt), default=0) + nxt) / 2
            else:
                pos = max(taken, default=0) + fourga.GAP
            self.client.create_list(board["id"], name, pos)
            have[name] = pos
            taken = sorted(taken + [pos])
            added.append(name)
        self.log(f"board {board['name']!r}: added the missing columns {', '.join(added)}")

    def _add_columns(self, board: dict, lists: dict) -> None:
        """Give a new board the manager's columns, in order, keeping a Done it already has."""
        done = lists.get(fourga.norm(fourga.DONE))
        end = done["position"] if done else fourga.GAP * (len(fourga.COLUMNS) + 1)
        before = [n for n in fourga.COLUMNS if n != fourga.DONE or not done]
        step = end / (len(before) + 1)
        for i, name in enumerate(before):
            pos = step * (i + 1) if done else fourga.GAP * (i + 1)
            self.client.create_list(board["id"], name, pos)
        self.log(f"new board {board['name']!r}: added the columns {', '.join(before)}"
                 + (" (keeping its Done)" if done else ""))

    def cards(self, boards: list[dict]) -> dict[str, CardView]:
        out = {}
        for full in boards:
            lists = fourga.lists_by_name(full)
            by_id = {l["id"]: fourga.norm(l["name"]) for l in (full.get("included") or {}).get("lists") or []}
            for c in (full.get("included") or {}).get("cards") or []:
                out[str(c["id"])] = CardView(c, by_id.get(c["listId"], ""), full, lists)
        return out

    # ── runs ────────────────────────────────────────────────────────────
    def runs(self) -> dict[str, RunConfig]:
        """The newest managed run per card in this project's folder."""
        out: dict[str, RunConfig] = {}
        for cfg in sessions.runs(self.state_dir):   # newest first
            if not (cfg.managed and cfg.card and cfg.workspace) or cfg.workspace.get("folder") != self.folder:
                continue
            out.setdefault(str(cfg.card["id"]), cfg)
        return out

    def _adversary(self, args: list[str]) -> str:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}   # a new tmux session, not a window here
        r = subprocess.run([sys.executable, "-m", "adversary.cli", *args], capture_output=True, text=True,
                           env=env, timeout=600, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-1500:] or f"exit {r.returncode}")
        return r.stdout

    def _failed(self, view: CardView, what: str, err: Exception, cfg: RunConfig | None = None) -> None:
        cid = str(view.card["id"])
        now = time.time()
        recent = [t for t in self.fails.get(cid, []) if now - t < FAIL_WINDOW] + [now]
        self.fails[cid] = recent
        self.log(f"card {cid}: {what} failed ({len(recent)}/{MAX_FAILS}): {err}")
        if len(recent) >= MAX_FAILS:
            self.fails.pop(cid, None)
            self._note(cid, f"**Adversary manager:** couldn't {what} this card after {MAX_FAILS} tries; it needs a "
                            f"person.\n\n```\n{str(err)[-1200:]}\n```")
            self._move(view, fourga.NEEDS_FEEDBACK)
            if cfg is not None:
                # Handed to people: no longer "cut off", or the next pass would resume it again (and fail
                # again) without anyone having done anything. A person's comment resumes it.
                state = Path(cfg.run_dir) / cardmode.STATE
                try:
                    data = json.loads(state.read_text())
                    data["live"] = False
                    state.write_text(json.dumps(data, indent=1))
                except (OSError, ValueError):
                    pass
            try:
                self.client.add_card_label(cid, self._label(view.board, fourga.ATTENTION_LABEL, fourga.ATTENTION_COLOR))
            except fourga.BoardError as e:
                self.log(f"card {cid}: couldn't add the {fourga.ATTENTION_LABEL!r} label: {e}")

    def _note(self, card_id: str, text: str) -> None:
        try:
            self.client.comment(card_id, text)
        except fourga.BoardError as e:
            self.log(f"card {card_id}: couldn't comment: {e}")

    def _move(self, view: CardView, column: str) -> bool:
        lst = view.lists.get(fourga.norm(column))
        if not lst:
            return False
        try:
            self.client.move_card(view.card["id"], lst["id"])
            return True
        except fourga.BoardError as e:
            self.log(f"card {view.card['id']}: couldn't move it to {column}: {e}")
            return False

    def claim(self, view: CardView) -> bool:
        """Move the card to In Progress and check it stayed there (4ga has no compare-and-set)."""
        if not self._move(view, fourga.IN_PROGRESS):
            return False
        time.sleep(0.5)
        try:
            now = self.client.card(view.card["id"])
        except fourga.BoardError:
            return False
        return now["listId"] == view.lists[fourga.norm(fourga.IN_PROGRESS)]["id"]

    def start(self, view: CardView) -> str | None:
        """Build the card's workspace and launch its run; returns the run dir."""
        cid = str(view.card["id"])
        try:
            info = workspace.create(self.folder, str(self.workspaces / f"card-{cid}"), cid)
            self.workspaces.mkdir(parents=True, exist_ok=True)
            info_file = self.workspaces / f"card-{cid}.json"
            info_file.write_text(json.dumps(info, indent=1))
            comments = self.client.comments(cid)
            card = self.client.card(cid)
            url = fourga.card_url(self.client.url, cid)
            task_file = self.workspaces / f"card-{cid}.task.md"
            task_file.write_text(cardmode.task_text(card, comments, str(self.client.me()["id"]), url,
                                                    self.client.checklist(cid)))
            args = ["run", "--task-file", str(task_file), "--repo", info["path"], "--card", cid, "--managed",
                    "--workspace-info", str(info_file), "--no-attach"]
            if self.board_env:
                args += ["--board-env", self.board_env]
            out = self.launcher(args + self.run_args)
        except Exception as e:
            self._failed(view, "start a run for", e)
            return None
        m = re.search(r"run dir (\S+)", out)
        run_dir = m.group(1) if m else "?"
        branches = ", ".join(sorted({r["branch"] for r in info["repos"]})) or "no git repos"
        self._note(cid, f"**Adversary manager:** picked this up on {socket.gethostname()} (run `{Path(run_dir).name}`, "
                        f"branch {branches}). The supervisor will post here when it needs you or is done.")
        self.log(f"card {cid} {view.card['name']!r}: started run {run_dir}")
        return run_dir

    def resume(self, view: CardView, cfg: RunConfig, why: str) -> bool:
        try:
            self.launcher(["resume", "--run", cfg.run_dir, "--kill", "--no-attach"])
        except Exception as e:
            self._failed(view, "resume the run for", e, cfg)
            return False
        self.log(f"card {view.card['id']}: resumed run {Path(cfg.run_dir).name} ({why})")
        return True

    @staticmethod
    def _cut_off(cfg: RunConfig) -> bool:
        """The run stopped while it was still live (see CardLink.live): a crash, reboot or kill."""
        try:
            return bool(json.loads((Path(cfg.run_dir) / cardmode.STATE).read_text()).get("live"))
        except (OSError, ValueError):
            return False

    @staticmethod
    def _open_requests(cfg: RunConfig) -> list:
        try:
            return json.loads((Path(cfg.run_dir) / cardmode.STATE).read_text()).get("open") or []
        except (OSError, ValueError):
            return []

    def _recent(self, cfg: RunConfig) -> bool:
        """The card was finished (its report written) within the follow-up window."""
        try:
            done_at = (Path(cfg.run_dir) / "report.md").stat().st_mtime
        except OSError:
            return False
        return time.time() - done_at <= self.done_window

    def followup(self, view: CardView, cfg: RunConfig) -> bool:
        """Resume a Done card's run for a new comment. Its workspace (removed at Done) is rebuilt at
        the same path, where Claude Code keeps the sessions, from the repos' current main; without
        sessions to resume, the card gets a fresh run instead, its comments in the task."""
        cid = str(view.card["id"])
        if not cfg.sessions:
            return self._move(view, fourga.IN_PROGRESS) and self.start(view) is not None
        try:
            info = workspace.create(self.folder, cfg.workspace["path"], cid)
        except Exception as e:
            self._failed(view, "rebuild the workspace for", e, cfg)
            return False
        cfg.workspace = info
        cfg.save()
        (self.workspaces / f"card-{cid}.json").write_text(json.dumps(info, indent=1))
        return self.resume(view, cfg, "a comment on the Done card")

    def stop(self, cfg: RunConfig, why: str) -> None:
        subprocess.run(["tmux", "kill-session", "-t", f"={cfg.session}"], capture_output=True)
        self.log(f"card {cfg.card['id']}: stopped run {Path(cfg.run_dir).name} ({why})")

    def _replied(self, cfg: RunConfig) -> bool:
        """A person commented on the card after the run last looked (its card.json cursor)."""
        try:
            cursor = int(json.loads((Path(cfg.run_dir) / cardmode.STATE).read_text()).get("cursor") or 0)
        except (OSError, ValueError):
            cursor = 0
        bot = str(self.client.me()["id"])
        return any(str(c.get("userId")) != bot and fourga.comment_text(c)
                   for c in self.client.comments(cfg.card["id"], after=cursor))

    # ── the Active label ────────────────────────────────────────────────
    def _label(self, full: dict, name: str, color: str) -> str:
        return self.client.label_id(full["item"]["id"], name, color, board=full)

    def sync_status(self, boards: list[dict], live: set[str], runs: dict[str, RunConfig]) -> None:
        """Make the board show what's running, healing itself after a crash: the Active label on
        exactly the cards with a live run, and each card run's timer running only while it's live."""
        for full in boards:
            try:
                label = self._label(full, fourga.ACTIVE_LABEL, fourga.ACTIVE_COLOR)
                inc = full.get("included") or {}
                has = {str(cl["cardId"]) for cl in inc.get("cardLabels") or [] if cl["labelId"] == label}
                for card in inc.get("cards") or []:
                    cid = str(card["id"])
                    if cid in live and cid not in has:
                        self.client.add_card_label(cid, label)
                    elif cid not in live and cid in has:
                        self.client.remove_card_label(cid, label)
                    if cid in runs:   # only cards the manager works: people may time their own
                        self._sync_timer(card, cid in live)
            except fourga.BoardError as e:
                self.log(f"board {full['item'].get('name')!r}: couldn't update its labels or timers: {e}")

    def _sync_timer(self, card: dict, running: bool) -> None:
        timer = card.get("timer") or {"startedAt": None, "total": 0}
        started, total = timer.get("startedAt"), float(timer.get("total") or 0)
        if running and not started:
            self.client.set_timer(card["id"], fourga.now_iso(), total)
        elif not running and started:
            try:
                since = datetime.fromisoformat(started.replace("Z", "+00:00"))
                total += max(0.0, (datetime.now(timezone.utc) - since).total_seconds())
            except ValueError:
                pass
            self.client.set_timer(card["id"], None, total)

    def _mark_active(self, view: CardView) -> None:
        """A run started or resumed: Active, timer running, and no longer needing attention."""
        try:
            self.client.add_card_label(view.card["id"], self._label(view.board, fourga.ACTIVE_LABEL, fourga.ACTIVE_COLOR))
            self._sync_timer(view.card, True)
            for lbl in (view.board.get("included") or {}).get("labels") or []:   # only if the board has one
                if fourga.norm(lbl.get("name") or "") == fourga.norm(fourga.ATTENTION_LABEL):
                    self.client.remove_card_label(view.card["id"], lbl["id"])
        except fourga.BoardError as e:
            self.log(f"card {view.card['id']}: couldn't update its label or timer: {e}")

    # ── one pass ────────────────────────────────────────────────────────
    def tick(self) -> None:
        boards = self.ensure_boards()
        views = self.cards(boards)
        runs = self.runs()
        live = {cid for cid, cfg in runs.items() if self.alive(cfg.session)}
        # Runs whose card moved out from under them.
        for cid in list(live):
            view = views.get(cid)
            if view is None or view.column in (fourga.norm(fourga.UPCOMING), fourga.norm(fourga.SUGGESTED)):
                self.stop(runs[cid], "its card was deleted" if view is None else f"its card was moved to {view.column}")
                live.discard(cid)
        # A Done card's workspace goes once its run has closed (the branches are merged by then).
        for cid, cfg in runs.items():
            view = views.get(cid)
            if view and view.column == fourga.norm(fourga.DONE) and cid not in live and cfg.workspace \
                    and not cfg.workspace.get("removed") and Path(cfg.workspace["path"]).exists():
                workspace.remove(cfg.workspace)
                for extra in (f"card-{cid}.json", f"card-{cid}.task.md"):
                    (self.workspaces / extra).unlink(missing_ok=True)
                try:
                    limits.remove_slice(cfg)
                except OSError as e:
                    self.log(f"card {cid}: couldn't remove its slice: {e}")
                cfg.workspace["removed"] = True
                cfg.save()
                self.log(f"card {cid}: done; removed its workspace")
        self.sync_status(boards, live, runs)
        self.live_count = len(live)
        free = self.pool - len(live)
        if self.capacity is not None:
            free = min(free, self.capacity())   # the pool is shared with other projects
        if free <= 0:
            return
        # Closed runs whose cards still need them come first: In Progress (crashed or closed), then
        # Needs Feedback with a person's reply. Then new cards from Approved To Do, top first.
        todo: list[tuple[str, CardView, RunConfig | None]] = []
        for cid, view in views.items():
            cfg = runs.get(cid)
            if cid in live:
                continue
            if view.column == fourga.norm(fourga.IN_PROGRESS):
                # A run whose agents never started (it closed at a startup dialog) has nothing to
                # resume: start a fresh one in the same workspace.
                todo.append(("resume" if cfg and cfg.sessions else "start", view, cfg))
        for cid, view in views.items():
            cfg = runs.get(cid)
            if cid not in live and cfg and cfg.sessions and view.column == fourga.norm(fourga.NEEDS_FEEDBACK):
                # Parked waiting on people: resumed by a reply. Cut off while live (it may have been
                # working on what didn't depend on the answer): resumed now.
                try:
                    if self._cut_off(cfg) or self._replied(cfg):
                        todo.append(("resume", view, cfg))
                except fourga.BoardError as e:
                    self.log(f"card {cid}: couldn't read its comments: {e}")
        # Comments on Done cards: the run picks its context back up to answer, or to revise the work.
        # They wait behind new work.
        followups = []
        for cid, view in views.items():
            cfg = runs.get(cid)
            if cid in live or not cfg or view.column != fourga.norm(fourga.DONE) or not self._recent(cfg):
                continue
            try:
                if self._cut_off(cfg) or self._replied(cfg):   # cut off mid-follow-up, or a new comment
                    followups.append(("followup", view, cfg))
            except fourga.BoardError as e:
                self.log(f"card {cid}: couldn't read its comments: {e}")
        approved = sorted((v for cid, v in views.items() if cid not in live and v.column == fourga.norm(fourga.APPROVED)),
                          key=lambda v: (v.board["item"].get("position") or 0, v.card.get("position") or 0))
        todo += [("claim", v, runs.get(str(v.card["id"]))) for v in approved]
        todo += followups
        for action, view, cfg in todo:
            if free <= 0:
                break
            if action == "resume" and cfg is not None:
                ok = self.resume(view, cfg, f"card in {view.column}" + (", cut off" if self._cut_off(cfg) else ""))
                if ok and view.column == fourga.norm(fourga.NEEDS_FEEDBACK) and not self._open_requests(cfg):
                    self._move(view, fourga.IN_PROGRESS)   # nothing left to ask: it's working again
            elif action == "followup" and cfg is not None:
                ok = self.followup(view, cfg)
            elif action == "claim":
                if not self.claim(view):
                    self.log(f"card {view.card['id']}: couldn't claim it (moved meanwhile?)")
                    continue
                ok = (self.resume(view, cfg, "card approved again") if cfg and not cfg.workspace.get("removed")
                      else self.start(view) is not None)
            else:
                ok = self.start(view) is not None
            if ok:
                free -= 1
                self.live_count += 1
                self._mark_active(view)

    def lock(self):
        """Hold this project's lock (one manager per project), or exit if another manager has it."""
        lock_path = self.state_dir.parent / f"manager-{self.key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(lock_path, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"adversary manage: another manager is already running for "
                             f"{self.project_id or self.project_name!r} (lock {lock_path})")
        return lock

    def serve(self, interval: float = 15, once: bool = False) -> None:
        Fleet([self], self.pool, log=self.log_fn).serve(interval, once)


class Fleet:
    """Several projects' managers in one process, sharing one pool of runs: each pass, every project
    may start runs only while the pool as a whole has room (and within its own cap)."""

    def __init__(self, managers: list[Manager], pool: int, log=print):
        self.managers, self.pool, self.log_fn = managers, pool, log
        for m in managers:
            m.capacity = self.free

    def free(self) -> int:
        return self.pool - sum(m.live_count for m in self.managers)

    def serve(self, interval: float = 15, once: bool = False) -> None:
        locks = [m.lock() for m in self.managers]   # noqa: F841 (held for the life of the process)
        me = self.managers[0].client.me()
        for m in self.managers:
            try:
                m.member_ids()   # fail now, not mid-loop, on an email with no 4ga account
            except fourga.BoardError as e:
                raise SystemExit(f"adversary manage: {e}")
            m.log(f"managing {m.project_id or m.project_name!r} as {me.get('name') or me['id']} · its cap "
                  f"{m.pool} · folder {m.folder} · workspaces {m.workspaces} · members {', '.join(m.members)}")
        if len(self.managers) > 1:
            self.log_fn(f"{time.strftime('%H:%M:%S')} pool {self.pool} shared by {len(self.managers)} projects")
        while True:
            for m in self.managers:
                try:
                    m.tick()
                    m.last_error = ""
                except Exception as e:   # keep managing; a run or the board can fail transiently
                    msg = f"board: {e}" if isinstance(e, fourga.BoardError) else f"error: {e!r}"
                    if msg != m.last_error:   # a project that's stuck says so once, not every pass
                        m.log(msg)
                        m.last_error = msg
            if once:
                return
            time.sleep(interval)


def _tmux_alive(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", f"={session}"], capture_output=True).returncode == 0
