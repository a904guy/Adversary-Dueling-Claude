import json
import os
import subprocess
from pathlib import Path

import pytest

from adversary import bridge as bridge_mod
from adversary import workspace
from adversary.bridge import ADVERSARY, WORKER, Bridge
from adversary.config import RunConfig, write_launch_files
from fake4ga import BOT, CLIENT_ID, HUMAN, SECRET, Fake4ga
from test_bridge import settle, start, stop, submit

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bridge_mod, "IDLE_SETTLE", 0)
    monkeypatch.setattr(bridge_mod, "RESUME_SETTLE", 0)
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def fake():
    f = Fake4ga()
    yield f
    f.close()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def board_with_card(fake):
    p = fake.add_project("P")
    b = fake.add_board(p["id"], "P", lists=["Upcoming", "Suggested", "Approved To Do", "In Progress",
                                             "Needs Feedback", "Done"])
    card = fake.add_card(fake.list_named(b["id"], "In Progress")["id"], "Add a button")
    return b, card


def make(fake, tmp_path, card, ws_info=None, **kw):
    env = tmp_path / "4ga.env"
    env.write_text(f"FOURGA_URL={fake.url}\nFOURGA_CLIENT_ID={CLIENT_ID}\nFOURGA_CLIENT_SECRET={SECRET}\n")
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    repo = ws_info["path"] if ws_info else str(tmp_path)
    cfg = RunConfig(task="t", repo=repo, run_dir=str(run), sock=str(tmp_path / "s"), base_sha=None,
                    snapshot_dir=None, card={"id": card["id"]}, board_env=str(env), workspace=ws_info,
                    managed=True, **kw)
    sent = []

    async def deliver(role, header, body):
        sent.append((role, header, body))

    b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=deliver, log=lambda m: None,
               screen_idle=lambda p: False, focus=lambda p: None, focused=lambda p: False,
               has_draft=lambda p: False, pane_mode=lambda p: None)
    b.project_file = tmp_path / "projects.json"
    b._diff_text = lambda: None
    return b, sent


def bot_comments(fake, card):
    return [c["data"]["text"] for c in sorted(fake.comments.values(), key=lambda c: int(c["id"]))
            if c["cardId"] == card["id"] and c["userId"] == BOT]


async def open_turn(b, sent):
    """The supervisor's first turn: its brief goes in, its plan goes to the worker; it's idle after."""
    await submit(b, ADVERSARY, [(r, body) for r, _, body in sent])
    await stop(b, ADVERSARY, "Plan: add the button.")


async def tick(b):
    b._card_next = 0
    await b.card_tick()
    await settle()


async def test_comments_relay_feedback_gates_done_and_done_moves_the_card(fake, tmp_path):
    b_, card = board_with_card(fake)
    fake.add_comment(card["id"], "old comment, already in the task")
    b, sent = make(fake, tmp_path, card)
    await start(b)
    await open_turn(b, sent)
    await tick(b)
    assert not any(h.startswith("A person commented") for _, h, _ in sent)   # existing comments aren't relayed

    # The supervisor asks for feedback: posted, card moves to Needs Feedback, done is refused.
    r = b.on_mcp("request_feedback", {"question": "Blue or green?"})
    assert not r.get("error") and fake.column_of(card["id"]) == "Needs Feedback"
    assert bot_comments(fake, card)[-1].startswith("**Supervisor needs feedback:**")
    r = b.on_mcp("report_done", {"summary": "all done"})
    assert r["error"] and "Blue or green?" in r["text"] and fake.column_of(card["id"]) == "Needs Feedback"

    # A held supervisor's reply isn't for the worker: it goes on the card.
    b.held = "waiting on the people"
    b.panes[ADVERSARY].busy, b.panes[ADVERSARY].reason = True, "worker_msg"
    await stop(b, ADVERSARY, "Waiting for the colour decision.")
    assert bot_comments(fake, card)[-1] == "**Message from Supervisor:**\n\nWaiting for the colour decision."

    # A person answers: relayed to the supervisor, the request closes, the card goes back to In Progress.
    fake.add_comment(card["id"], "Green, please.", user=HUMAN, name="Pat")
    await tick(b)
    relayed = [(h, body) for role, h, body in sent if role == ADVERSARY]
    assert relayed[-1][0].startswith("A person commented on the card") and "Pat:\n\nGreen, please." in relayed[-1][1]
    assert b.card.open == [] and fake.column_of(card["id"]) == "In Progress"
    await tick(b)
    assert len([1 for role, h, _ in sent if h.startswith("A person commented")]) == 1   # not relayed twice

    # Done: summary posted, card in Done, run finished. finish(complete) is steered to report_done.
    assert b.on_mcp("finish", {"outcome": "complete", "summary": "x"})["error"]
    r = b.on_mcp("report_done", {"summary": "Green button added and tested."})
    assert not r.get("error") and fake.column_of(card["id"]) == "Done" and b.finished == "COMPLETE"
    assert bot_comments(fake, card)[-1].startswith("**Done.**\n\nGreen button added and tested.")
    b.panes[ADVERSARY].busy, b.panes[ADVERSARY].reason = True, "human_comment"
    await stop(b, ADVERSARY, "Card is Done — green button added.")     # the same turn's closing words
    assert bot_comments(fake, card)[-1].startswith("**Done.**")         # not posted again


async def test_worker_may_only_ask_for_feedback_and_the_supervisor_hears_of_it(fake, tmp_path):
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await start(b)
    await open_turn(b, sent)
    assert b.on_mcp("report_done", {"summary": "x"}, role=WORKER)["error"]
    assert b.on_mcp("message_worker", {"message": "x"}, role=WORKER)["error"]
    assert not b.on_mcp("request_feedback", {"question": "Need the API key"}, role=WORKER).get("error")
    assert bot_comments(fake, card)[-1].startswith("**Worker needs feedback:**")
    await settle()
    assert any(role == ADVERSARY and "Need the API key" in body for role, _, body in sent)


async def test_merge_conflict_at_done_goes_to_the_worker(fake, tmp_path):
    src = tmp_path / "app"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.py").write_text("1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "init")
    info = workspace.create(str(src), str(tmp_path / "ws"), "5")
    (Path(info["path"]) / "a.py").write_text("card\n")
    git(info["path"], "commit", "-qam", "card")
    (src / "a.py").write_text("main\n")
    git(src, "commit", "-qam", "main moved")
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card, ws_info=info)
    await start(b)
    r = b.on_mcp("report_done", {"summary": "done"})
    assert r["error"] and "sent to the worker" in r["text"] and fake.column_of(card["id"]) == "In Progress"
    await settle()
    assert any(role == WORKER and "conflicts with main" in body for role, _, body in sent)
    # The worker resolves it: the next report_done merges and finishes.
    subprocess.run(["git", "merge", "main"], cwd=info["path"], capture_output=True)
    (Path(info["path"]) / "a.py").write_text("main + card\n")
    git(info["path"], "commit", "-qam", "resolve")
    r = b.on_mcp("report_done", {"summary": "done"})
    assert not r.get("error") and (src / "a.py").read_text() == "main + card\n"
    assert "merged into main" in bot_comments(fake, card)[-1]


async def test_managed_run_waiting_on_people_parks_when_idle(fake, tmp_path):
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card)
    stops = []
    b.request_stop = lambda: stops.append(1)
    await start(b)
    for p in b.panes.values():
        p.queue.clear()
        p.busy = False
    b._check_park(0)
    assert not stops                                       # nothing open: never parks
    b.on_mcp("request_feedback", {"question": "?"})
    b._check_park(100)
    b._check_park(100 + bridge_mod.PARK_AFTER - 1)
    assert not stops
    b._check_park(100 + bridge_mod.PARK_AFTER)
    assert stops == [1]


def test_card_launch_files_give_each_side_its_tools(tmp_path):
    cfg = RunConfig(task="t", repo=str(tmp_path), run_dir=str(tmp_path), sock="/s", base_sha="abc",
                    card={"id": "9"}, managed=True, workspace={"folder": "/f", "path": str(tmp_path), "repos": [
                        {"path": "", "branch": "adversary/card-9", "target": "main"}]})
    scripts = write_launch_files(cfg)
    adv, worker = scripts["adversary"].read_text(), scripts["worker"].read_text()
    for tool in ("request_feedback", "report_done", "suggest_task"):
        assert f"mcp__adversary__{tool}" in adv
    assert "worker-mcp.json" in worker and "worker-role.md" in worker
    wmcp = json.loads((tmp_path / "worker-mcp.json").read_text())["mcpServers"]["adversary"]
    assert wmcp["env"]["ADVERSARY_ROLE"] == "worker"
    assert "adversary/card-9" in (tmp_path / "worker-role.md").read_text()
    assert "report_done" in (tmp_path / "adversary-role.md").read_text()
    allow = json.loads((tmp_path / "worker-settings.json").read_text())["permissions"]["allow"]
    assert allow == ["mcp__adversary__request_feedback"]


def test_mcp_server_offers_tools_by_role(monkeypatch):
    from adversary import mcp_server
    monkeypatch.delenv("ADVERSARY_ROLE", raising=False)
    monkeypatch.delenv("ADVERSARY_CARD", raising=False)
    plain = {t["name"] for t in mcp_server.tools()}
    assert "report_done" not in plain and "finish" in plain
    monkeypatch.setenv("ADVERSARY_CARD", "1")
    assert {"report_done", "request_feedback", "suggest_task"} <= {t["name"] for t in mcp_server.tools()}
    monkeypatch.setenv("ADVERSARY_ROLE", "worker")
    assert [t["name"] for t in mcp_server.tools()] == ["request_feedback"]


def test_a_run_not_launched_by_the_manager_has_no_card_anything(tmp_path):
    for kw in ({}, {"card": {"id": "9"}}):   # a plain run, and a card without the manager behind it
        cfg = RunConfig(task="t", repo=str(tmp_path), run_dir=str(tmp_path), sock="/s", base_sha="abc", **kw)
        scripts = write_launch_files(cfg)
        adv, worker = scripts["adversary"].read_text(), scripts["worker"].read_text()
        assert "report_done" not in adv and "request_feedback" not in adv and "worker-mcp.json" not in worker
        assert "card" not in (tmp_path / "adversary-role.md").read_text().lower()
        assert "permissions" not in json.loads((tmp_path / "worker-settings.json").read_text())
        b = Bridge(cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=lambda *a: None, log=lambda m: None)
        assert b.card is None


async def test_done_merges_into_repos_another_user_owns(fake, tmp_path, monkeypatch):
    """On the server the agents run as root over repos another user owns: git refuses those as
    "dubious ownership" unless they're safe.directory, merge included."""
    from adversary.config import git_safe_env
    src = tmp_path / "app"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.py").write_text("1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "init")
    info = workspace.create(str(src), str(tmp_path / "ws"), "6")
    (Path(info["path"]) / "a.py").write_text("card\n")
    git(info["path"], "commit", "-qam", "card")
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card, ws_info=info)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for k, v in git_safe_env(b.cfg.safe_dirs(), env={}).items():   # what serve() and the launch scripts set
        monkeypatch.setenv(k, v)
    await start(b)
    r = b.on_mcp("report_done", {"summary": "done"})
    assert not r.get("error"), r["text"]
    assert (src / "a.py").read_text() == "card\n"


async def test_a_reply_that_wakes_a_parked_run_goes_into_its_resume_note(fake, tmp_path):
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card)
    b.card.start()
    b.card.open = [{"who": "Supervisor", "question": "?"}]
    b.panes[ADVERSARY].queue.clear()
    b.panes[ADVERSARY].queue.append(("This supervised run was resumed.", "resume"))
    fake.add_comment(card["id"], "Use option B.", user=HUMAN, name="Pat")
    await tick(b)
    (body, reason), = b.panes[ADVERSARY].queue
    assert reason == "resume" and "Pat:\n\nUse option B." in body


async def test_follow_up_on_a_done_card_answers_on_the_card_or_reopens_for_work(fake, tmp_path):
    b_, card = board_with_card(fake)
    fake.cards[card["id"]]["listId"] = fake.list_named(b_["id"], "Done")["id"]
    run = tmp_path / "run"
    run.mkdir()
    (run / "card.json").write_text(json.dumps({"cursor": 0, "open": []}))
    (run / "transcript.md").write_text("# Adversary run transcript\n")
    fake.add_comment(card["id"], "Why return 0 rather than raise?", user=HUMAN, name="Pat")
    b, sent = make(fake, tmp_path, card)
    b2 = Bridge(b.cfg, {WORKER: "%1", ADVERSARY: "%2"}, deliver=b.deliver, log=lambda m: None, resume="Resumed.",
                screen_idle=lambda p: False, focus=lambda p: None, focused=lambda p: False,
                has_draft=lambda p: False, pane_mode=lambda p: None)
    b2.project_file = tmp_path / "projects.json"
    await b2.card_tick(now=0)                                  # before the agents start, as serve() does
    assert b2.card_followup and b2.held
    (body, reason), = b2.panes[ADVERSARY].queue
    assert reason == "resume" and "already reported Done" in body and "Why return 0 rather than raise?" in body
    await start(b2)
    await submit(b2, ADVERSARY, [(r, x) for r, _, x in sent])
    await stop(b2, ADVERSARY, "Because Pat asked for it on the card; raising would break their callers.")
    assert bot_comments(fake, card)[-1].startswith("**Message from Supervisor:**\n\nBecause Pat asked")
    assert not any(role == WORKER for role, _, _ in sent)      # the question didn't wake the worker
    assert fake.column_of(card["id"]) == "In Progress"         # an agent is on it while it answers
    b2.on_mcp("message_worker", {"message": "Make divide raise ZeroDivisionError instead."})
    assert fake.column_of(card["id"]) == "In Progress" and not b2.card_followup and not b2.held
    assert json.loads((run / "card.json").read_text())["followup"] is False   # real work now


async def test_an_answered_follow_up_closes_after_a_short_idle(fake, tmp_path):
    b_, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card)
    stops = []
    b.request_stop = lambda: stops.append(1)
    b.card_followup = True
    for p in b.panes.values():
        p.busy = False
        p.queue.clear()
    b._check_park(0)
    b._check_park(bridge_mod.DONE_IDLE - 1)
    assert not stops
    b._check_park(bridge_mod.DONE_IDLE)
    assert stops == [1]
    assert fake.column_of(card["id"]) == "Done"                # only a question: back to Done


async def test_files_on_the_card_at_start_go_out_with_the_brief(fake, tmp_path):
    _, card = board_with_card(fake)
    fake.add_attachment(card["id"], "mockup.png", b"\x89PNG fake")
    fake.add_attachment(card["id"], "bot-log.txt", b"mine", user=BOT)       # the bot's own: not relayed
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)                                    # before the agents start, as serve() does
    (body, reason), = b.panes[ADVERSARY].queue
    assert reason == "init" and "mockup.png (from Pat)" in body and "bot-log.txt" not in body
    path = next(line.split(": ", 1)[1] for line in body.splitlines() if "mockup.png" in line)
    assert Path(path).read_bytes() == b"\x89PNG fake" and Path(path).parent == Path(b.cfg.run_dir) / "attachments"
    await start(b)
    await open_turn(b, sent)
    await tick(b)
    assert not any(h.startswith("A person commented") for _, h, _ in sent)   # not sent again


async def test_a_comment_and_its_attachment_arrive_together(fake, tmp_path):
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await start(b)
    await open_turn(b, sent)
    await tick(b)
    fake.add_comment(card["id"], "Here's the error I get.", user=HUMAN, name="Pat")
    fake.add_attachment(card["id"], "trace log.txt", b"Traceback ...")
    await tick(b)
    msgs = [body for role, h, body in sent if h.startswith("A person commented")]
    assert len(msgs) == 1 and "Here's the error I get." in msgs[0] and "trace log.txt (from Pat)" in msgs[0]
    path = Path(b.cfg.run_dir) / "attachments"
    assert [p.read_bytes() for p in path.iterdir()] == [b"Traceback ..."]
    await submit(b, ADVERSARY, [(r, x) for r, _, x in sent])
    await stop(b, ADVERSARY, "Thanks, looking at the trace.")
    fake.add_attachment(card["id"], "more.png", b"img")                         # a file on its own
    await tick(b)
    msgs = [body for role, h, body in sent if h.startswith("A person commented")]
    assert len(msgs) == 2 and "more.png" in msgs[1] and "Files only" in msgs[1]


def test_both_sides_may_read_the_downloaded_files(tmp_path):
    cfg = RunConfig(task="t", repo=str(tmp_path), run_dir=str(tmp_path), sock="/s", base_sha="abc",
                    card={"id": "9"}, managed=True)
    scripts = write_launch_files(cfg)
    for role in ("worker", "adversary"):
        assert f"--add-dir {tmp_path / 'attachments'}" in scripts[role].read_text()


async def test_a_run_is_marked_live_until_it_closes_on_purpose(fake, tmp_path):
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card)
    b.request_stop = lambda: None
    b.card.set_live(True)                                      # as serve() does at start
    state = lambda: json.loads((Path(b.cfg.run_dir) / "card.json").read_text())
    assert state()["live"] is True
    await start(b)
    b.on_mcp("request_feedback", {"question": "?"})
    for p in b.panes.values():
        p.busy = False
        p.queue.clear()
        p.direct.clear()
    b._check_park(0)
    b._check_park(bridge_mod.PARK_AFTER)                       # parked: closed on purpose
    assert state()["live"] is False


async def test_suggested_cards_get_their_own_description_checklist_and_label(fake, tmp_path):
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card)
    r = b.on_mcp("suggest_task", {"title": "Add a dark mode", "description": "Users asked twice.",
                                  "tasks": ["Pick colours", "Add the toggle"]})
    assert not r.get("error") and "2 checklist items" in r["text"]
    new = next(c for c in fake.cards.values() if c["name"] == "Add a dark mode")
    assert fake.column_of(new["id"]) == "Suggested" and "Users asked twice." in new["description"]
    assert [t["name"] for t in fake.tasks if t["cardId"] == new["id"]] == ["Pick colours", "Add the toggle"]
    assert fake.has_label(new["id"], "Suggested by Agent")
    assert card["description"] is None and not [t for t in fake.tasks if t["cardId"] == card["id"]]   # theirs: untouched


async def test_attach_image_uploads_and_sets_the_cover(fake, tmp_path):
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card)
    shot = tmp_path / "result.png"
    shot.write_bytes(b"\x89PNG screenshot")
    assert b.on_mcp("attach_image", {"path": str(tmp_path / "notes.txt")})["error"]   # images only
    r = b.on_mcp("attach_image", {"path": str(shot), "cover": True})
    assert not r.get("error"), r["text"]
    (att,) = [a for a in fake.attachments.values() if a["cardId"] == card["id"]]
    assert att["name"] == "result.png" and b"\x89PNG screenshot" in fake.files[att["id"]]
    assert fake.cards[card["id"]]["coverAttachmentId"] == att["id"]


async def test_done_comment_has_the_numbers_and_commits(fake, tmp_path):
    src = tmp_path / "app"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.py").write_text("1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "init")
    info = workspace.create(str(src), str(tmp_path / "ws"), "3")
    for msg in ("Add the button", "Test the button"):
        (Path(info["path"]) / "a.py").write_text(msg)
        git(info["path"], "commit", "-qam", msg)
    _, card = board_with_card(fake)
    b, _ = make(fake, tmp_path, card, ws_info=info)
    b._diff_text = lambda: "1 file, +1 −1"
    await b.card_tick(now=0)                                    # as serve() does before the agents start
    await start(b)
    b.on_mcp("request_feedback", {"question": "Blue?"})
    fake.add_comment(card["id"], "Yes", user=HUMAN)
    await tick(b)
    assert not b.on_mcp("report_done", {"summary": "Button added."}).get("error")
    done = bot_comments(fake, card)[-1]
    assert "**Merged:**" in done and "**Commits:**" in done
    assert done.index("Add the button") < done.index("Test the button")
    assert "1 feedback request" in done and "diff 1 file, +1 −1" in done and "ran " in done


async def test_a_merge_only_a_person_can_fix_needs_attention(fake, tmp_path):
    src = tmp_path / "app"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.py").write_text("1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "init")
    info = workspace.create(str(src), str(tmp_path / "ws"), "4")
    (Path(info["path"]) / "a.py").write_text("card\n")
    git(info["path"], "commit", "-qam", "card")
    git(src, "checkout", "-q", "-b", "someone-elses-branch")   # the real checkout moved off main
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card, ws_info=info)
    await start(b)
    r = b.on_mcp("report_done", {"summary": "done"})
    assert r["error"] and "needs a person" in r["text"]
    assert fake.column_of(card["id"]) == "Needs Feedback" and fake.has_label(card["id"], "Needs attention")
    assert not any(role == WORKER for role, _, _ in sent)          # not the worker's to fix


async def test_a_reply_posted_just_before_the_bots_own_comment_is_not_skipped(fake, tmp_path):
    """Live: the person replied a moment before the supervisor's hold note went on the card; marking
    the bot's own post as 'seen' jumped past the reply, and it never reached the supervisor."""
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    await open_turn(b, sent)
    b.on_mcp("request_feedback", {"question": "Round to 2 places?"})
    fake.add_comment(card["id"], "No rounding.", user=HUMAN, name="Pat")   # lands first...
    b.card.post("**Supervisor:**\n\nHolding for the answer.")              # ...then the bot posts
    await tick(b)
    assert any(h.startswith("A person commented") and "No rounding." in body for _, h, body in sent)
    assert b.card.open == []


async def test_the_checklist_is_in_the_task_people_changes_are_relayed_and_ticks_are_ours(fake, tmp_path):
    from adversary import board as fourga_mod
    from adversary import cardmode
    _, card = board_with_card(fake)
    colours = fake.add_task(card["id"], "Pick colours", done=True)
    toggle = fake.add_task(card["id"], "Add the toggle")
    client = fourga_mod.Board(fake.url, CLIENT_ID, SECRET, retries=0)
    task = cardmode.task_text(fake.cards[card["id"]], [], BOT, "u", client.checklist(card["id"]))
    assert "1. [x] Pick colours\n2. [ ] Add the toggle" in task
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    await open_turn(b, sent)
    await tick(b)
    assert not any(h.startswith("A person commented") for _, h, _ in sent)   # the starting list isn't news
    # The supervisor ticks what it verified: by number or name, and it isn't reported back to it.
    assert b.on_mcp("tick_checklist", {"item": "2"})["text"] == "Ticked: Add the toggle"
    assert b.on_mcp("tick_checklist", {"item": "nope"})["error"]
    await tick(b)
    assert not any(h.startswith("A person commented") for _, h, _ in sent)
    # A person edits it: renamed, unticked, added, removed — relayed in one message with any comment.
    toggle["name"] = "Add the toggle to settings"
    colours["isCompleted"] = False
    fake.add_task(card["id"], "Write docs")
    fake.add_comment(card["id"], "Updated the checklist.", user=HUMAN, name="Pat")
    await tick(b)
    (msg,) = [body for _, h, body in sent if h.startswith("A person commented")]
    for line in ("renamed: Add the toggle → Add the toggle to settings", "unticked: Pick colours",
                 "added: [ ] Write docs", "Updated the checklist."):
        assert line in msg
    fake.tasks.remove(colours)
    await submit(b, ADVERSARY, [(r, x) for r, _, x in sent])
    await stop(b, ADVERSARY, "Noted.")
    await tick(b)
    assert "removed: Pick colours" in [body for _, h, body in sent if h.startswith("A person commented")][-1]


async def test_the_supervisor_can_add_checklist_items_without_them_echoing_back(fake, tmp_path):
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    await open_turn(b, sent)
    for step in ("Write the tests", "Implement multiply"):
        assert not b.on_mcp("add_checklist_item", {"name": step}).get("error")
    assert [t["name"] for t in fake.tasks if t["cardId"] == card["id"]] == ["Write the tests", "Implement multiply"]
    await tick(b)
    assert not any(h.startswith("A person commented") for _, h, _ in sent)   # its own additions aren't news
    assert b.on_mcp("tick_checklist", {"item": "Write the tests"})["text"] == "Ticked: Write the tests"


async def test_several_comments_between_checks_arrive_together_once(fake, tmp_path):
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    await open_turn(b, sent)
    fake.add_comment(card["id"], "First thought.", user=HUMAN, name="Pat")
    fake.add_comment(card["id"], "Bot noise.", user=BOT, name="Claude")
    fake.add_comment(card["id"], "Second thought.", user=HUMAN, name="Sam")
    await tick(b)
    await tick(b)
    (msg,) = [body for _, h, body in sent if h.startswith("A person commented")]
    assert msg.index("Pat:\n\nFirst thought.") < msg.index("Sam:\n\nSecond thought.") and "Bot noise." not in msg


async def test_done_is_refused_while_checklist_items_are_unticked(fake, tmp_path):
    _, card = board_with_card(fake)
    fake.add_task(card["id"], "Implement it")
    fake.add_task(card["id"], "Commit on the branch")
    b, _ = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    r = b.on_mcp("report_done", {"summary": "done"})
    assert r["error"] and "Implement it" in r["text"] and "Commit on the branch" in r["text"]
    b.on_mcp("tick_checklist", {"item": "1"})
    r = b.on_mcp("report_done", {"summary": "done", "leave_unticked": "The manager commits; nothing to do."})
    assert not r.get("error") and fake.column_of(card["id"]) == "Done"
    assert "**Left unticked:** Commit on the branch — The manager commits" in bot_comments(fake, card)[-1]


async def test_the_conversation_between_the_agents_is_posted_with_its_direction(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_mod, "CARD_PART_MAX", 60)
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    await open_turn(b, sent)                                    # the plan goes to the worker
    assert bot_comments(fake, card)[-1] == "**Message from Supervisor to Worker:**\n\nPlan: add the button."
    await submit(b, WORKER, [(r, x) for r, _, x in sent])
    await stop(b, WORKER, "Added the button; tests pass.")
    assert bot_comments(fake, card)[-1] == "**Message from Worker to Supervisor:**\n\nAdded the button; tests pass."
    before = len(bot_comments(fake, card))
    b.on_mcp("message_worker", {"message": "Also add a tooltip.\n\n" + "Make it short. " * 6})
    parts = bot_comments(fake, card)[before:]
    n = len(parts)
    assert n >= 2 and parts[0].startswith(f"**Message from Supervisor to Worker (part 1 of {n}):**")
    assert parts[-1].startswith(f"**Message from Supervisor to Worker (part {n} of {n}):**")
    squash = lambda t: "".join(t.split())
    assert squash("".join(p.split(":**\n\n", 1)[1] for p in parts)) == \
        squash("Also add a tooltip." + "Make it short. " * 6)              # nothing lost
    await submit(b, ADVERSARY, [(r, x) for r, _, x in sent])
    await stop(b, ADVERSARY, "Looks right; one more thing: add a test for the tooltip.")   # a reply to the worker
    assert bot_comments(fake, card)[-1].startswith("**Message from Supervisor to Worker:**\n\nLooks right")


def test_split_message_keeps_paragraphs_and_code_blocks_whole():
    from adversary.bridge import split_message
    assert split_message("short", 100) == ["short"]
    text = "Intro paragraph.\n\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(40)) + "\n```\n\nAfter."
    parts = split_message(text, 120)
    assert all(len(p) <= 120 for p in parts) and len(parts) > 2
    assert parts[0].startswith("Intro paragraph.") and parts[0].endswith("```")   # cut code block closed...
    for p in parts[1:]:
        assert p.startswith("```python")                                   # ...and reopened in the next part
    assert parts[-1].endswith("After.")
    assert split_message("a" * 30 + "\n\n" + "b" * 30, 50) == ["a" * 30, "b" * 30]   # a paragraph break wins
    joined = "\n".join(parts)
    for i in range(40):
        assert f"x{i} = {i}" in joined                                     # nothing lost


async def test_a_plan_written_as_the_reply_to_a_comment_doesnt_strand_the_worker(fake, tmp_path):
    """Live: the worker was on hold for a feedback answer; the person answered; the supervisor wrote
    the worker's plan as its reply to that comment, which goes on the card, not to the worker — and
    the run sat still. Now it's told the worker is idle and to use message_worker."""
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    await submit(b, ADVERSARY, [(r, x) for r, _, x in sent])
    b.on_mcp("request_feedback", {"question": "What should modulo(x, 0) do?"})
    b.on_mcp("hold", {"reason": "waiting for the answer"})
    await stop(b, ADVERSARY, "Holding for the answer.")
    fake.add_comment(card["id"], "Raise ZeroDivisionError.", user=HUMAN, name="Pat")
    await tick(b)
    relay = [body for _, h, body in sent if h.startswith("A person commented")][-1]
    assert "message_worker" in relay                            # told up front how to start the worker
    await submit(b, ADVERSARY, [(r, x) for r, _, x in sent])
    await stop(b, ADVERSARY, "Got it. Worker: implement modulo raising ZeroDivisionError.")
    assert not any(role == WORKER for role, _, _ in sent)       # (a reply to a person never reaches it)
    nudge = [body for role, _, body in sent if role == ADVERSARY][-1]
    assert "still idle" in nudge and "message_worker" in nudge  # ...so the supervisor is told, once
    await submit(b, ADVERSARY, [(r, x) for r, _, x in sent])
    await stop(b, ADVERSARY, "Understood.")
    assert [body for role, _, body in sent if role == ADVERSARY][-1] == nudge   # not nagged again


async def test_the_live_comment_shows_both_agents_working_first_edited_while_newest(fake, tmp_path):
    _, card = board_with_card(fake)
    b, sent = make(fake, tmp_path, card)
    await b.card_tick(now=0)
    await start(b)
    wlog, slog = tmp_path / "worker.jsonl", tmp_path / "sup.jsonl"
    b._worker_log = lambda: wlog
    b._role_log = lambda role: slog
    def says(log, text, cmd):
        with log.open("a") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-17T16:00:00Z", "message": {"content": [
                {"type": "text", "text": text}, {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
    live = lambda: [t for t in bot_comments(fake, card) if t.startswith("**Live**")]
    for p in b.panes.values():
        p.busy = False
    says(wlog, "Starting.", "ls")
    b._update_status(10)
    assert live() == []                                        # nobody working: nothing to show
    b.panes[WORKER].busy = True
    b._update_status(20)
    assert len(live()) == 1 and "**Worker** ● working" in live()[0] and "> Starting." in live()[0]
    assert "Now: Bash: ls" in live()[0]
    fake.edits = 0
    b._update_status(30)                                       # nothing new, too soon: left alone
    assert fake.edits == 0
    # The worker hands over; the supervisor reviews: it goes first, the worker shows its last words.
    says(wlog, "isqrt done; 18 tests pass.", "python3 -m pytest -q")
    b.panes[WORKER].busy, b.panes[WORKER].idle_at = False, 1_760_000_000
    b.panes[ADVERSARY].busy = True
    says(slog, "Checking the edge cases myself.", "python3 -m pytest -q -k isqrt")
    b._update_status(40)
    body = live()[0]
    assert fake.edits == 1 and body.index("**Supervisor** ● working") < body.index("**Worker** · idle since")
    assert "> Checking the edge cases myself." in body and "Now: Bash: python3 -m pytest -q -k isqrt" in body
    assert "> isqrt done; 18 tests pass." in body and body.count("Now:") == 1   # only the one working
    fake.add_comment(card["id"], "Looks good so far.", user=HUMAN, name="Pat")
    says(slog, "All good; reporting done.", "git log -1")
    b._update_status(50)                                       # something new since: a new one below
    assert len(live()) == 2 and "Checking the edge cases" in live()[0] and bot_comments(fake, card)[-1] == live()[-1]
