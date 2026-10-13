import json
import os
import stat
from pathlib import Path

import pytest

from adversary import mcps
from adversary.config import RunConfig, write_launch_files

DEVTOOLS = {"type": "stdio", "command": "npx", "args": ["chrome-devtools-mcp@latest", "--headless", "--isolated"],
            "env": {"SECRET": "s3cret"}}
FIREFOX = {"type": "stdio", "command": "npx", "args": ["firefox-mcp"]}


def _home(repo, user=None, local=None, project=None, chrome=False, **proj_keys):
    home = Path.home()
    cfg = {"mcpServers": user or {}, "claudeInChromeDefaultEnabled": chrome,
           "projects": {str(repo): {"mcpServers": local or {}, **proj_keys}}}
    (home / ".claude.json").write_text(json.dumps(cfg))
    if project is not None:
        (Path(repo) / ".mcp.json").write_text(json.dumps({"mcpServers": project}))
    return home


def test_enabled_servers_merges_scopes_and_respects_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _home(repo, user={"chrome-devtools": DEVTOOLS, "shadowed": {"command": "user"}},
          local={"shadowed": {"command": "local"}, "off": {"command": "x"}},
          project={"approved": {"command": "p1"}, "unapproved": {"command": "p2"}},
          enabledMcpjsonServers=["approved"], disabledMcpServers=["off"])
    got = mcps.enabled_servers(repo)
    assert set(got) == {"chrome-devtools", "shadowed", "approved"}   # unapproved project + disabled left out
    assert got["shadowed"]["command"] == "local"                     # local beats user


def test_plan_gives_nothing_unless_servers_are_named(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _home(repo, user={"chrome-devtools": DEVTOOLS, "firefox": FIREFOX, "adversary": {"command": "evil"}}, chrome=True)
    for specs in ([], None, ["none"]):
        p = mcps.plan(specs, repo)
        assert p.servers == {} and p.allow == [] and not p.chrome
    p = mcps.plan(["chrome-devtools", "firefox", "claude-in-chrome"], repo)
    assert set(p.servers) == {"chrome-devtools", "firefox"}
    assert "mcp__chrome-devtools" in p.allow and "mcp__firefox" in p.allow
    assert p.chrome and "mcp__claude-in-chrome" in p.allow
    with pytest.raises(ValueError):
        mcps.plan(["adversary"], repo, strict=True)                   # a user server can't shadow ours


def test_plan_named_readonly_tools_and_none(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _home(repo, user={"chrome-devtools": DEVTOOLS, "firefox": FIREFOX}, chrome=True)
    ro = mcps.plan(["chrome-devtools:readonly"], repo)
    assert set(ro.servers) == {"chrome-devtools"} and not ro.chrome
    assert "mcp__chrome-devtools__take_screenshot" in ro.allow
    assert not any(t.endswith(("__click", "__fill", "__evaluate_script")) for t in ro.allow)
    picked = mcps.plan(["firefox:open,screenshot"], repo)
    assert picked.allow == ["mcp__firefox__open", "mcp__firefox__screenshot"]
    with pytest.raises(ValueError, match="available: chrome-devtools, firefox, claude-in-chrome"):
        mcps.plan(["nope"], repo, strict=True)
    with pytest.raises(ValueError, match="no read-only preset"):
        mcps.plan(["firefox:readonly"], repo, strict=True)
    assert mcps.plan(["nope"], repo).allow == []                      # lenient at launch: skipped


def test_launch_files_hand_the_servers_to_the_supervisor_privately(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _home(repo, user={"chrome-devtools": DEVTOOLS}, chrome=True)
    run = tmp_path / "run"
    run.mkdir()
    cfg = RunConfig(task="t", repo=str(repo), run_dir=str(run), sock="/s", base_sha="abc")
    scripts = write_launch_files(cfg)                                   # nothing named: just the bridge's own
    assert set(json.loads((run / "adversary-mcp.json").read_text())["mcpServers"]) == {"adversary"}
    assert "--chrome" not in scripts["adversary"].read_text()
    cfg.adversary_mcps = ["chrome-devtools", "claude-in-chrome"]
    scripts = write_launch_files(cfg)
    conf = json.loads((run / "adversary-mcp.json").read_text())["mcpServers"]
    assert set(conf) == {"adversary", "chrome-devtools"} and conf["chrome-devtools"]["env"]["SECRET"] == "s3cret"
    assert stat.S_IMODE(os.stat(run / "adversary-mcp.json").st_mode) == 0o600
    adv = scripts["adversary"].read_text()
    assert "mcp__chrome-devtools" in adv and "--chrome" in adv and "adversary-role.md" in adv
    assert "chrome-devtools" in (run / "adversary-role.md").read_text()
    assert "--chrome" not in scripts["worker"].read_text()              # the worker is unchanged
    cfg.adversary_mcps = ["none"]
    adv = write_launch_files(cfg)["adversary"].read_text()
    assert "mcp__chrome-devtools" not in adv and "--chrome" not in adv
