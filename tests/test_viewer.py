import asyncio
import json
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from adversary import cardmode, viewer
from test_cardmode import board_with_card, fake, make  # noqa: F401  (fake is a fixture)


def test_prompts_are_labelled_by_who_the_bridge_says_they_are_from():
    src, label, body = viewer.classify("Message from your supervisor (pasted below). Follow it as if I wrote it: \n"
                                       '<pasted_content id="ab12">\nDo X.\n</pasted_content>')
    assert (src, label, body) == ("supervisor", "Supervisor", "Do X.")
    assert viewer.classify("Message from your supervisor, sent while you were working (pasted below). x\nY")[0:2] == \
        ("supervisor", "Supervisor (while working)")
    assert viewer.classify("Permission request from the worker (pasted below).\nBash: ls")[0] == "permission"
    assert viewer.classify("A person commented on the card (pasted below).\nhi")[0] == "person"
    assert viewer.classify("just typing in the pane") == ("typed", "Typed in the pane", "just typing in the pane")


def test_session_log_lines_become_events():
    use = {"type": "assistant", "timestamp": "t1", "thinkingDurationMs": 1200, "message": {"content": [
        {"type": "thinking", "thinking": "", "signature": "x"},
        {"type": "text", "text": "Looking."},
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]}}
    evs = viewer.parse_entry(use, "worker")
    assert [e["kind"] for e in evs] == ["thinking", "text", "tool_use"]
    assert evs[0]["ms"] == 1200 and evs[2]["name"] == "Bash" and evs[2]["input"] == {"command": "ls"}
    res = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "is_error": True,
                                                    "content": [{"type": "text", "text": "boom"}]}]}}
    assert viewer.parse_entry(res, "worker") == [{"role": "worker", "ts": None, "kind": "tool_result", "id": "tu1",
                                                  "error": True, "text": "boom"}]
    assert viewer.parse_entry({"type": "api-request-blob", "message": {}}, "worker") == []
    assert viewer.parse_entry({"type": "assistant", "isSidechain": True, "message": {"content": []}}, "worker") == []
    assert viewer.parse_entry({"type": "system", "subtype": "compact_boundary"}, "adversary")[0]["text"] == \
        "Conversation compacted"
    big = viewer.parse_entry({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "w", "name": "Write", "input": {"content": "x" * 100000}}]}}, "worker")
    assert len(big[0]["input"]["content"]) < 31000 and "more characters not shown" in big[0]["input"]["content"]


def test_a_log_is_read_in_whole_lines_and_a_big_one_from_near_its_end(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"a": 1}\n{"b"')
    tail = viewer.LogTail(f)
    assert tail.read() == [{"a": 1}]
    with f.open("a") as fh:
        fh.write(': 2}\n')
    assert tail.read() == [{"b": 2}]
    f.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(1000)))
    late = viewer.LogTail(f, start_window=200, chunk=1 << 20)
    got = late.read()
    assert got and got[-1] == {"n": 999} and got[0]["n"] > 900       # the first, partial line is skipped


def test_the_logs_link_is_added_once_kept_current_and_left_out_of_the_task():
    url = "https://kabana.hti.dev/live/productbuilder.io/47001/"
    assert cardmode.with_logs_link("", url) == f"Click here for logs: {url}"
    first = cardmode.with_logs_link("Fix the page.", url)
    assert first == f"Fix the page.\n\nClick here for logs: {url}"
    assert cardmode.with_logs_link(first, url) is None                 # already there: no edit
    moved = cardmode.with_logs_link(first, url.replace("47001", "47002"))
    assert moved.count("Click here for logs:") == 1 and "47002" in moved
    assert cardmode.without_logs_link(first) == "Fix the page."


class FakePane:
    busy = False


class FakeBridge:
    def __init__(self, log_path):
        self.log_path, self.panes, self.held, self.finished, self.lines = log_path, {"worker": FakePane(),
                                                                                    "adversary": FakePane()}, False, None, []

    def _role_log(self, role):
        return self.log_path if role == "worker" else None

    def status(self):
        return "[running] all quiet"

    def log(self, msg):
        self.lines.append(msg)


async def _get(port, path, cookie=None, ctx=None, keep=False):
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx,
                                                   server_hostname="localhost" if ctx else None)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n".encode() + (f"Cookie: {cookie}\r\n".encode() if cookie else b"")
                 + b"\r\n")
    await writer.drain()
    if keep:
        return reader, writer
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


async def test_the_page_is_public_but_its_data_needs_a_token_kabana_accepts(tmp_path):
    log = tmp_path / "w.jsonl"
    log.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n")
    asked = []

    def check(token):
        asked.append(token)
        return token == "good"
    live = viewer.Viewer(FakeBridge(log), {"worker": "Worker", "adversary": "Supervisor"}, check=check, title="Card")
    port = await live.start(None, None, None, host="127.0.0.1")
    try:
        assert port in viewer.VIEWER_PORTS
        await asyncio.sleep(0.7)                                       # one poll of the log
        status, body = await _get(port, "/live/h/1/")
        assert status == 200 and b"View only" in body
        assert (await _get(port, "/live/h/1"))[0] == 301
        assert (await _get(port, "/live/h/1/history"))[0] == 401       # no cookie
        assert (await _get(port, "/live/h/1/history", "accessToken=bad"))[0] == 401
        status, body = await _get(port, "/live/h/1/history", "other=1; accessToken=good")
        data = json.loads(body)
        assert status == 200 and data["card"]["title"] == "Card" and data["events"][0]["text"] == "hello"
        await _get(port, "/live/h/1/history", "accessToken=good")
        assert asked.count("good") == 1                                # kabana's answer is cached
        reader, writer = await _get(port, "/live/h/1/events?after=1", "accessToken=good", keep=True)
        live.bridge_line("12:00:00 → worker: do it")
        seen = b""
        while b"do it" not in seen:
            seen += await asyncio.wait_for(reader.read(4096), 5)
        assert b"text/event-stream" in seen and b'"role": "bridge"' in seen and b"hello" not in seen
        writer.close()
    finally:
        await live.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl to make a certificate")
async def test_the_view_is_served_over_tls_with_the_given_certificate(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                    "-keyout", str(key), "-out", str(cert), "-days", "1"], check=True, capture_output=True)
    live = viewer.Viewer(FakeBridge(tmp_path / "none.jsonl"), {"worker": "Worker"}, check=lambda t: True)
    port = await live.start(None, str(cert), str(key), host="127.0.0.1")
    try:
        ctx = ssl.create_default_context(cafile=str(cert))
        status, body = await _get(port, "/x/", ctx=ctx)
        assert status == 200 and b"<!doctype html>" in body
    finally:
        await live.close()


async def test_a_card_run_puts_the_link_in_its_description(fake, tmp_path):
    _, card = board_with_card(fake)
    fake.cards[card["id"]]["description"] = "Do the thing."
    b, _ = make(fake, tmp_path, card)
    title = b.card.ensure_logs_link("https://kabana.hti.dev/live/h/47005/")
    assert title == "Add a button"
    assert fake.cards[card["id"]]["description"] == "Do the thing.\n\nClick here for logs: https://kabana.hti.dev/live/h/47005/"
