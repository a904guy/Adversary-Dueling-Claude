import json
import os
import subprocess
from pathlib import Path

import pytest

from adversary import board as fourga
from adversary import cardmode
from adversary.config import RunConfig
from adversary.manager import Manager
from fake4ga import BOT, CLIENT_ID, HUMAN, SECRET, Fake4ga

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture
def fake():
    f = Fake4ga()
    yield f
    f.close()


@pytest.fixture
def folder(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "proj" / "app"
    repo.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return tmp_path / "proj"


class Launcher:
    """Stands in for `adversary run|resume`: records the call and saves a run config like run() does."""

    def __init__(self, state: Path):
        self.state, self.calls, self.n = state, [], 0

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "run":
            self.n += 1
            run_dir = self.state / f"20261017-12000{self.n}-0001"
            run_dir.mkdir(parents=True)
            get = lambda flag: args[args.index(flag) + 1]
            RunConfig(task=Path(get("--task-file")).read_text(), repo=get("--repo"), run_dir=str(run_dir), sock="s",
                      base_sha=None, session=f"adversary-{run_dir.name}", card={"id": get("--card")}, managed=True,
                      workspace=json.loads(Path(get("--workspace-info")).read_text())).save()
            return f"adversary: run dir {run_dir}\n"
        return "adversary: resuming\n"


def make(fake, folder, tmp_path, pool=2, alive=lambda s: False):
    client = fourga.Board(fake.url, CLIENT_ID, SECRET, retries=0)
    state = tmp_path / "state" / "runs"
    state.mkdir(parents=True, exist_ok=True)
    launcher = Launcher(state)
    m = Manager("AnySite", str(folder), pool, client, state, workspaces=tmp_path / "ws",
                log=lambda m: None, launcher=launcher, alive=alive, members=["pat@example.com"])
    return m, launcher


def test_creates_project_board_and_columns_then_claims_up_to_the_pool(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=2)
    m.tick()                                                   # nothing exists yet
    (p,) = fake.projects.values()
    (b,) = fake.boards.values()
    names = [l["name"] for l in sorted((l for l in fake.lists.values() if l["boardId"] == b["id"]),
                                       key=lambda l: l["position"])]
    assert p["name"] == "AnySite" and names == fourga.COLUMNS
    assert {"projectId": p["id"], "userId": HUMAN} in fake.managers          # people can see what the bot made
    assert any(x["boardId"] == b["id"] and x["userId"] == HUMAN for x in fake.memberships)
    approved = fake.list_named(b["id"], "Approved To Do")
    cards = [fake.add_card(approved["id"], f"Task {i}", "do it", position=65535 * (i + 1)) for i in range(3)]
    idea = fake.add_card(fake.list_named(b["id"], "Upcoming")["id"], "just an idea")
    fake.add_comment(cards[0]["id"], "use the blue button")
    m.tick()
    assert [fake.column_of(c["id"]) for c in cards] == ["In Progress", "In Progress", "Approved To Do"]
    assert fake.column_of(idea["id"]) == "Upcoming"            # never touched
    run = launcher.calls[0]
    assert run[:2] == ["run", "--task-file"] and "--managed" in run and run[run.index("--card") + 1] == cards[0]["id"]
    task = Path(run[run.index("--task-file") + 1]).read_text()
    assert "# Task 0" in task and "do it" in task and "use the blue button" in task
    ws = Path(run[run.index("--repo") + 1])
    assert (ws / "app" / ".git").is_file()                     # the card's worktree
    assert any("picked this up" in c["data"]["text"] for c in fake.comments.values() if c["userId"] == BOT)


def test_full_pool_waits(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    for i in range(2):
        fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], f"Task {i}", position=65535 * (i + 1))
    m.tick()
    m.alive = lambda s: True                                   # the first run is still going
    m.tick()
    assert len(launcher.calls) == 1


def test_resumes_crashed_and_answered_runs_and_stops_pulled_back_ones(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=3)
    m.tick()
    b = next(iter(fake.boards.values()))
    approved = fake.list_named(b["id"], "Approved To Do")["id"]
    crashed = fake.add_card(approved, "crashed")
    waiting = fake.add_card(approved, "waiting")
    m.tick()
    runs = m.runs()
    assert set(runs) == {crashed["id"], waiting["id"]}
    for cfg in runs.values():                                  # both got going (a session each)
        cfg.sessions = {"worker": "w", "adversary": "a"}
        cfg.save()
    runs = m.runs()
    # One run is parked in Needs Feedback: no reply yet, so it stays closed.
    fake.cards[waiting["id"]]["listId"] = fake.list_named(b["id"], "Needs Feedback")["id"]
    last = max(int(c["id"]) for c in fake.comments.values())
    (Path(runs[waiting["id"]].run_dir) / cardmode.STATE).write_text(json.dumps({"cursor": last, "open": [{}]}))
    launcher.calls.clear()
    m.tick()
    assert [c[:3] for c in launcher.calls] == [["resume", "--run", runs[crashed["id"]].run_dir]]
    launcher.calls.clear()
    fake.add_comment(waiting["id"], "go with option B")         # a person answers
    m.alive = lambda s: s == runs[crashed["id"]].session
    m.tick()
    assert [c[:3] for c in launcher.calls] == [["resume", "--run", runs[waiting["id"]].run_dir]]
    # A card pulled back to Upcoming has its live run stopped.
    stopped = []
    m.stop = lambda cfg, why: stopped.append((cfg.card["id"], why))
    fake.cards[crashed["id"]]["listId"] = fake.list_named(b["id"], "Upcoming")["id"]
    m.tick()
    assert stopped == [(crashed["id"], "its card was moved to upcoming")]


def test_done_card_workspace_is_removed_once_its_run_closed(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    card = fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], "t")
    m.tick()
    cfg = m.runs()[card["id"]]
    assert Path(cfg.workspace["path"]).exists()
    fake.cards[card["id"]]["listId"] = fake.list_named(b["id"], "Done")["id"]
    m.tick()
    assert not Path(cfg.workspace["path"]).exists() and m.runs()[card["id"]].workspace["removed"]
    assert not list((tmp_path / "ws").glob(f"card-{card['id']}*"))   # its info and task files too
    assert (folder / "app" / ".git").is_dir()                  # the real repo is untouched


def test_project_creation_refused_says_what_to_do(fake, folder, tmp_path):
    fake.allow_project_create = False
    m, _ = make(fake, folder, tmp_path)
    with pytest.raises(fourga.BoardError, match="may not create projects"):
        m.tick()


def test_bad_credentials_are_reported(fake):
    with pytest.raises(fourga.BoardError, match="401"):
        fourga.Board(fake.url, "nope", "nope", retries=0).me()


def test_a_run_that_never_started_is_started_again_not_resumed(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    card = fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], "t")
    m.tick()
    launcher.calls.clear()
    m.tick()                                                   # its run closed with no sessions
    assert [c[0] for c in launcher.calls] == ["run"] and fake.column_of(card["id"]) == "In Progress"


def test_people_are_added_to_an_existing_project_and_its_boards(fake, folder, tmp_path):
    p = fake.add_project("AnySite", creator=BOT)
    b = fake.add_board(p["id"], "Later board", lists=fourga.COLUMNS)
    fake.memberships.append({"boardId": b["id"], "userId": BOT, "role": "editor"})
    m, _ = make(fake, folder, tmp_path)
    m.tick()
    m.tick()                                                   # a second pass adds nothing twice
    assert fake.managers.count({"projectId": p["id"], "userId": HUMAN}) == 1
    assert [x for x in fake.memberships if x["userId"] == HUMAN] == [
        {"boardId": b["id"], "userId": HUMAN, "role": "editor"}]


def test_an_unknown_member_email_stops_the_manager(fake, folder, tmp_path):
    m, _ = make(fake, folder, tmp_path)
    m.members = ["nobody@example.com"]
    with pytest.raises(fourga.BoardError, match="no 4ga user"):
        m.tick()


def test_a_comment_on_a_recent_done_card_resumes_its_run_in_a_rebuilt_workspace(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    card = fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], "t")
    m.tick()
    cfg = m.runs()[card["id"]]
    cfg.sessions = {"worker": "w", "adversary": "a"}
    cfg.save()
    (Path(cfg.run_dir) / "report.md").write_text("done")
    fake.cards[card["id"]]["listId"] = fake.list_named(b["id"], "Done")["id"]
    last = max(int(c["id"]) for c in fake.comments.values())
    (Path(cfg.run_dir) / cardmode.STATE).write_text(json.dumps({"cursor": last, "open": []}))
    m.tick()                                                   # done: workspace removed, nothing to resume
    assert not Path(cfg.workspace["path"]).exists()
    launcher.calls.clear()
    m.tick()
    assert launcher.calls == []                                # no new comment: stays closed
    fake.add_comment(card["id"], "Why does divide return 0? Can it raise instead?")
    m.tick()
    assert [c[:3] for c in launcher.calls] == [["resume", "--run", cfg.run_dir]]
    again = m.runs()[card["id"]]
    assert Path(again.workspace["path"]).exists() and not again.workspace.get("removed")
    assert again.workspace["path"] == cfg.workspace["path"]    # same folder: Claude Code finds its sessions
    assert fake.column_of(card["id"]) == "Done"                # a question doesn't reopen it by itself


def test_comments_on_long_done_cards_are_left_alone(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.done_window = 86400
    m.tick()
    b = next(iter(fake.boards.values()))
    card = fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], "t")
    m.tick()
    cfg = m.runs()[card["id"]]
    cfg.sessions = {"worker": "w", "adversary": "a"}
    cfg.save()
    report = Path(cfg.run_dir) / "report.md"
    report.write_text("done")
    os.utime(report, (0, 0))                                   # finished long ago
    fake.cards[card["id"]]["listId"] = fake.list_named(b["id"], "Done")["id"]
    fake.add_comment(card["id"], "a late thought")
    launcher.calls.clear()
    m.tick()
    assert launcher.calls == []


def test_a_board_someone_makes_gets_the_columns_and_the_bot_joins_it(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    p = next(iter(fake.projects.values()))
    # Made in 4ga's UI with the "Empty" template: one Done list, and the bot isn't a member.
    new = fake.add_board(p["id"], "Sprint 2", lists=["common.done"])
    theirs = fake.add_board(p["id"], "Marketing", lists=["Ideas", "Doing"])
    fake.add_card(fake.list_named(theirs["id"], "Ideas")["id"], "a campaign")
    m.tick()
    names = [l["name"] for l in sorted((l for l in fake.lists.values() if l["boardId"] == new["id"]),
                                       key=lambda l: l["position"])]
    assert names == ["Upcoming", "Suggested", "Approved To Do", "In Progress", "Needs Feedback", "common.done"]
    assert {"boardId": new["id"], "userId": BOT, "role": "editor"} in fake.memberships
    assert [l["name"] for l in fake.lists.values() if l["boardId"] == theirs["id"]] == ["Ideas", "Doing"]
    card = fake.add_card(fake.list_named(new["id"], "Approved To Do")["id"], "work on the new board")
    m.tick()
    assert fake.column_of(card["id"]) == "In Progress" and launcher.calls
    fake.cards[card["id"]]["listId"] = fake.list_named(new["id"], "common.done")["id"]
    assert m.cards(m.ensure_boards())[card["id"]].column == "done"     # its Done is the manager's Done


def _set_state(cfg, **kw):
    path = Path(cfg.run_dir) / cardmode.STATE
    state = json.loads(path.read_text()) if path.exists() else {"cursor": 10 ** 9, "open": []}
    state.update(kw)
    path.write_text(json.dumps(state))


def test_after_a_crash_runs_cut_off_in_any_column_are_resumed_and_parked_ones_wait(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=3)
    m.tick()
    b = next(iter(fake.boards.values()))
    approved = fake.list_named(b["id"], "Approved To Do")["id"]
    parked, crashed_waiting, crashed_followup = (fake.add_card(approved, n) for n in ("parked", "busy", "follow-up"))
    m.tick()
    runs = m.runs()
    for cfg in runs.values():
        cfg.sessions = {"worker": "w", "adversary": "a"}
        cfg.save()
    feedback = fake.list_named(b["id"], "Needs Feedback")["id"]
    for c in (parked, crashed_waiting):
        fake.cards[c["id"]]["listId"] = feedback
    fake.cards[crashed_followup["id"]]["listId"] = fake.list_named(b["id"], "Done")["id"]
    (Path(runs[crashed_followup["id"]].run_dir) / "report.md").write_text("done")
    _set_state(runs[parked["id"]], live=False)                  # closed on purpose, waiting on people
    _set_state(runs[crashed_waiting["id"]], live=True)          # the server went down under it
    _set_state(runs[crashed_followup["id"]], live=True)         # cut off while answering a follow-up
    launcher.calls.clear()
    m.tick()                                                   # the manager comes back up: nothing is alive
    resumed = {c[2] for c in launcher.calls if c[0] == "resume"}
    assert resumed == {runs[crashed_waiting["id"]].run_dir, runs[crashed_followup["id"]].run_dir}
    assert Path(m.runs()[crashed_followup["id"]].workspace["path"]).exists()   # rebuilt for it


def test_cards_with_a_live_run_carry_the_active_label(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=2)
    m.tick()
    b = next(iter(fake.boards.values()))
    approved = fake.list_named(b["id"], "Approved To Do")["id"]
    one, two = fake.add_card(approved, "one"), fake.add_card(approved, "two")
    m.tick()
    assert fake.has_label(one["id"], "Active") and fake.has_label(two["id"], "Active")   # right away
    (lbl,) = fake.labels.values()
    assert lbl["color"] == "#2e7d32"
    runs = m.runs()
    m.alive = lambda s: s == runs[one["id"]].session          # two's run died (or finished)
    fake.cards[two["id"]]["listId"] = fake.list_named(b["id"], "Done")["id"]
    m.tick()
    assert fake.has_label(one["id"], "Active") and not fake.has_label(two["id"], "Active")
    assert len(fake.labels) == 1                                # one label per board, reused


def test_the_timer_runs_while_a_run_is_live_and_failures_get_needs_attention(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    card = fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], "t")
    mine = fake.add_card(fake.list_named(b["id"], "Upcoming")["id"], "a person's own")
    fake.cards[mine["id"]]["timer"] = {"startedAt": "2026-01-01T00:00:00.000Z", "total": 0}
    m.tick()
    timer = fake.cards[card["id"]]["timer"]
    assert timer["startedAt"] and timer["total"] == 0           # running from the moment it started
    fake.cards[card["id"]]["timer"] = {"startedAt": "2020-01-01T00:00:00.000Z", "total": 30}
    m.tick()                                                   # its run is gone: the timer stops
    stopped = fake.cards[card["id"]]["timer"]
    assert stopped["startedAt"] is None and stopped["total"] > 30
    assert fake.cards[mine["id"]]["timer"]["startedAt"]        # a card the manager doesn't run is left alone
    # Three failed resumes: handed to people, marked Needs attention; a later good start clears it.
    cfg = m.runs()[card["id"]]
    cfg.sessions = {"worker": "w", "adversary": "a"}
    cfg.save()
    real = m.launcher
    m.launcher = lambda args: (_ for _ in ()).throw(RuntimeError("tmux exploded"))
    fake.cards[card["id"]]["listId"] = fake.list_named(b["id"], "In Progress")["id"]
    for _ in range(3):
        m.tick()
    assert fake.column_of(card["id"]) == "Needs Feedback" and fake.has_label(card["id"], "Needs attention")
    m.launcher = real
    fake.cards[card["id"]]["listId"] = fake.list_named(b["id"], "In Progress")["id"]
    m.tick()
    assert not fake.has_label(card["id"], "Needs attention") and fake.has_label(card["id"], "Active")
