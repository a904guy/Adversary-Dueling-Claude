import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Keep tests off the real ~/.claude.json and settings (MCP servers, Claude in Chrome)."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
