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


def test_approved_cards_without_the_agent_label_are_marked_as_from_a_human(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    old = {"id": fake.nid(), "boardId": b["id"], "name": "Suggested by agent", "color": "#6a1b9a"}
    fake.labels[old["id"]] = old                               # from before "Agent" was capitalised
    approved = fake.list_named(b["id"], "Approved To Do")["id"]
    mine, waiting, theirs = (fake.add_card(approved, n, position=65535 * (i + 1))
                             for i, n in enumerate(["mine", "waiting", "agent's"]))
    fake.card_labels.append({"cardId": theirs["id"], "labelId": old["id"]})
    idea = fake.add_card(fake.list_named(b["id"], "Upcoming")["id"], "just an idea")
    m.tick()
    assert fake.column_of(mine["id"]) == "In Progress" and fake.column_of(waiting["id"]) == "Approved To Do"
    assert fake.has_label(mine["id"], "Suggested by Human") and fake.has_label(waiting["id"], "Suggested by Human")
    assert not fake.has_label(theirs["id"], "Suggested by Human") and old["name"] == "Suggested by Agent"
    assert not fake.has_label(idea["id"], "Suggested by Human")   # only once it's approved
    m.tick()
    assert sum(cl["cardId"] == waiting["id"] for cl in fake.card_labels) == 1


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


def board_columns(fake, board):
    return [l["name"] for l in sorted((l for l in fake.lists.values() if l["boardId"] == board["id"]),
                                      key=lambda l: l["position"])]


def test_a_board_with_columns_of_its_own_gets_the_missing_ones_around_them(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    p = next(iter(fake.projects.values()))
    theirs = fake.add_board(p["id"], "Development", lists=["Upcoming", "Doing", "Done"])
    fake.add_card(fake.list_named(theirs["id"], "Upcoming")["id"], "an idea")
    m.tick()
    assert board_columns(fake, theirs) == ["Upcoming", "Doing", "Suggested", "Approved To Do", "In Progress",
                                           "Needs Feedback", "Done"]
    card = fake.add_card(fake.list_named(theirs["id"], "Approved To Do")["id"], "work on it")
    m.tick()
    assert fake.column_of(card["id"]) == "In Progress" and launcher.calls
    m.tick()
    assert len(board_columns(fake, theirs)) == 7                        # nothing added twice


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
    assert board_columns(fake, theirs) == ["Ideas", "Doing", "Suggested", "Approved To Do", "In Progress",
                                           "Needs Feedback", "Done"]
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
    assert fake.column_of(crashed_waiting["id"]) == "In Progress"   # nothing open: it's working again
    assert fake.column_of(parked["id"]) == "Needs Feedback"
    assert Path(m.runs()[crashed_followup["id"]].workspace["path"]).exists()   # rebuilt for it


def test_cards_with_a_live_run_carry_the_active_label(fake, folder, tmp_path):
    m, launcher = make(fake, folder, tmp_path, pool=2)
    m.tick()
    b = next(iter(fake.boards.values()))
    approved = fake.list_named(b["id"], "Approved To Do")["id"]
    one, two = fake.add_card(approved, "one"), fake.add_card(approved, "two")
    m.tick()
    assert fake.has_label(one["id"], "Active") and fake.has_label(two["id"], "Active")   # right away
    (lbl,) = (l for l in fake.labels.values() if l["name"] == "Active")
    assert lbl["color"] == "#2e7d32"
    runs = m.runs()
    m.alive = lambda s: s == runs[one["id"]].session          # two's run died (or finished)
    fake.cards[two["id"]]["listId"] = fake.list_named(b["id"], "Done")["id"]
    m.tick()
    assert fake.has_label(one["id"], "Active") and not fake.has_label(two["id"], "Active")
    assert sum(l["name"] == "Active" for l in fake.labels.values()) == 1   # one per board, reused


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


def test_giving_up_on_a_cut_off_run_hands_it_to_people_once(fake, folder, tmp_path):
    """Live: a run cut off in Needs Feedback failed to resume 3 times; the manager gave up, but the run
    still read as cut off, so the next passes tried again and posted the give-up note again."""
    m, launcher = make(fake, folder, tmp_path, pool=1)
    m.tick()
    b = next(iter(fake.boards.values()))
    card = fake.add_card(fake.list_named(b["id"], "Approved To Do")["id"], "t")
    m.tick()
    cfg = m.runs()[card["id"]]
    cfg.sessions = {"adversary": "a"}
    cfg.save()
    _set_state(cfg, live=True, cursor=0)
    fake.cards[card["id"]]["listId"] = fake.list_named(b["id"], "Needs Feedback")["id"]
    calls = []
    m.launcher = lambda args: (calls.append(args), (_ for _ in ()).throw(RuntimeError("no session")))[1]
    for _ in range(8):
        m.tick()
    assert len(calls) == 3                                     # three tries, then it waits for a person
    notes = [c for c in fake.comments.values() if "couldn't resume" in c["data"]["text"]]
    assert len(notes) == 1 and fake.has_label(card["id"], "Needs attention")
    fake.add_comment(card["id"], "Try again please.")          # a person replies: resumed again
    m.launcher = launcher
    m.tick()
    assert launcher.calls[-1][:3] == ["resume", "--run", cfg.run_dir]


def test_projects_share_one_pool_and_a_broken_one_doesnt_stop_the_rest(fake, folder, tmp_path):
    from adversary.manager import Fleet
    client = fourga.Board(fake.url, CLIENT_ID, SECRET, retries=0)
    state = tmp_path / "state" / "runs"
    state.mkdir(parents=True)
    launcher = Launcher(state)
    logs = []
    mk = lambda **kw: Manager(folder=str(folder), client=client, state_dir=state, log=logs.append, launcher=launcher,
                              alive=lambda s: True, members=["pat@example.com"], **kw)
    a = mk(project="Alpha", pool=4, workspaces=tmp_path / "ws-a", tag="Alpha")
    b = mk(project=None, project_id="999", pool=4, tag="by id")          # not visible to the bot
    fleet = Fleet([a, b], pool=2, log=logs.append)
    a.tick()                                                             # creates Alpha and its board
    board = next(iter(fake.boards.values()))
    for i in range(3):
        fake.add_card(fake.list_named(board["id"], "Approved To Do")["id"], f"t{i}", position=65535 * (i + 1))
    for _ in range(2):
        for m in fleet.managers:
            try:
                m.tick()
            except fourga.BoardError as e:
                logs.append(str(e))
    assert len([c for c in launcher.calls if c[0] == "run"]) == 2      # the shared pool of 2, not Alpha's 4
    assert any("isn't visible to the bot" in l for l in logs)
    # By id: found once the bot can see it, and named from the board.
    p = fake.add_project("Beta", creator=BOT)
    c = mk(project=None, project_id=p["id"], pool=1, workspaces=tmp_path / "ws-c")
    c.tick()
    assert c.project_name == "Beta" and any(x["projectId"] == p["id"] for x in fake.boards.values())


def test_the_manage_config_file(tmp_path):
    from adversary.cli import load_manage_config
    f = tmp_path / "manage.toml"
    f.write_text('pool = "auto"\nmembers = ["pat@example.com"]\nrun_args = ["--cpu-quota", "auto"]\n\n'
                 '[[project]]\nname = "Test suite"\nfolder = "/srv/a"\n\n'
                 '[[project]]\nid = "1887993708561827225"\nfolder = "/srv/b"\npool = 2\n')
    conf = load_manage_config(str(f))
    assert [p.get("name") or p.get("id") for p in conf["project"]] == ["Test suite", "1887993708561827225"]
    assert conf["project"][1]["pool"] == 2 and conf["run_args"] == ["--cpu-quota", "auto"]
    f.write_text('[[project]]\nfolder = "/srv/a"\n')
    with pytest.raises(SystemExit):
        load_manage_config(str(f))                                       # a project needs a name or an id
