"""tmux helpers: pane creation, paste delivery and screen capture."""

import subprocess


def tmux(*args: str, input: str | None = None) -> str:
    return subprocess.run(["tmux", *args], input=input, capture_output=True, text=True, check=True).stdout.strip()


def paste(pane: str, text: str, buffer: str = "adversary") -> None:
    """Type `text` into `pane` as one bracketed paste (multi-line stays one message)."""
    tmux("load-buffer", "-b", buffer, "-", input=text)
    tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane)


def type_text(pane: str, text: str) -> None:
    """Type a single line literally (no Enter)."""
    tmux("send-keys", "-t", pane, "-l", text)


def press(pane: str, *keys: str) -> None:
    tmux("send-keys", "-t", pane, *keys)


def capture(pane: str) -> str:
    try:
        return tmux("capture-pane", "-p", "-t", pane)
    except subprocess.CalledProcessError:
        return ""
