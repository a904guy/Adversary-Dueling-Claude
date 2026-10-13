import subprocess

from adversary.cli import find_git_subdirs
from adversary.config import RunConfig


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_find_git_subdirs_lists_nested_repos_relative_to_root(tmp_path):
    root = tmp_path / "AnySite"
    root.mkdir()
    _repo(root / "AnyDigits")
    _repo(root / "AnyFork")
    _repo(root / "apps" / "deep")                      # one level nested: still found
    (root / "plain").mkdir()                            # not a repo: ignored
    _repo(root / "node_modules" / "pkg")                # skipped dir: ignored
    found = set(find_git_subdirs(root))
    assert found == {"AnyDigits", "AnyFork", "apps/deep"}


def test_find_git_subdirs_does_not_descend_into_a_repo(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    _repo(root / "outer")
    _repo(root / "outer" / "inner")                     # inside a repo we already found
    assert find_git_subdirs(root) == ["outer"]          # inner is not listed separately


def test_adversary_bash_adds_read_only_git_for_each_subdir():
    cfg = RunConfig(task="t", repo="/x", run_dir="/r", sock="/s", base_sha=None,
                    snapshot_dir="/r/snapshot", git_subdirs=["AnyDigits", "AnyFork"])
    cmds = cfg.adversary_bash()
    # Both path and verb are pinned, and only read-only verbs are present.
    assert "git -C AnyDigits log" in cmds and "git -C AnyDigits status" in cmds
    assert "git -C AnyFork diff" in cmds and "git -C AnyFork show" in cmds
    assert not any(c.startswith("git -C") and c.split()[-1] in ("push", "commit", "reset", "clean")
                   for c in cmds)
    assert "diff" in cmds                                # snapshot mode still gets bare diff


def test_adversary_bash_without_subdirs_is_unchanged():
    cfg = RunConfig(task="t", repo="/x", run_dir="/r", sock="/s", base_sha="abc")
    cmds = cfg.adversary_bash()
    assert not any(c.startswith("git -C") for c in cmds)
    assert "git log" in cmds and "git status" in cmds


def test_adversary_can_call_its_read_only_inspection_tools(tmp_path):
    """dontAsk refuses any tool not on --allowedTools, so each MCP tool must be listed there."""
    from adversary.config import write_launch_files
    from adversary.mcp_server import TOOLS
    cfg = RunConfig(task="t", repo=str(tmp_path), run_dir=str(tmp_path), sock="/s", base_sha="abc")
    script = write_launch_files(cfg)["adversary"].read_text()
    for tool in TOOLS:
        assert f"mcp__adversary__{tool['name']}" in script, tool["name"]


def test_effort_defaults_to_high_for_the_worker_and_xhigh_for_the_adversary(tmp_path):
    from adversary.config import write_launch_files
    cfg = RunConfig(task="t", repo=str(tmp_path), run_dir=str(tmp_path), sock="/s", base_sha="abc")
    scripts = write_launch_files(cfg)
    assert "--effort high" in scripts["worker"].read_text()
    assert "--effort xhigh" in scripts["adversary"].read_text()
    cfg.worker_effort, cfg.adversary_effort = "low", None    # None: leave it to Claude Code
    scripts = write_launch_files(cfg)
    assert "--effort low" in scripts["worker"].read_text()
    assert "--effort" not in scripts["adversary"].read_text()


def test_resume_can_switch_the_worker_permission_mode(tmp_path, monkeypatch):
    """`adversary resume --worker-permission-mode auto` rewrites the saved config and the worker's launch."""
    import argparse
    from adversary import cli
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = RunConfig(task="t", repo=str(tmp_path), run_dir=str(run_dir), sock=str(tmp_path / "s.sock"),
                    base_sha="abc", skip_permissions=True, session="adv-x")
    cfg.save()
    monkeypatch.setattr(cli.shutil, "which", lambda exe: "/bin/" + exe)
    monkeypatch.setattr(cli.sessions, "find_sessions", lambda c: {"worker": "w", "adversary": "a"})
    monkeypatch.setattr(cli.sessions, "last_worker_message", lambda d: "hi")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=1))
    monkeypatch.setattr(cli, "bridge_alive", lambda s: False)
    launched = {}
    monkeypatch.setattr(cli, "launch", lambda c, **k: launched.setdefault("cfg", c))
    ns = argparse.Namespace(run=str(run_dir), repo=".", kill=False, no_attach=True, message=None,
                            worker_effort=None, adversary_effort=None,
                            worker_permission_mode="auto", skip_permissions=False)
    cli.resume(ns)
    saved = RunConfig.load(str(run_dir))
    assert saved.worker_permission_mode == "auto" and saved.skip_permissions is False
    assert launched["cfg"].worker_permission_mode == "auto"


def _parse(*argv):
    import sys
    from adversary import cli
    old = sys.argv
    sys.argv = ["adversary", *argv]
    try:
        captured = {}
        orig = (cli.run, cli.resume)
        cli.run = cli.resume = lambda a: captured.setdefault("a", a)
        cli.main()
        return captured["a"]
    finally:
        sys.argv = old
        cli.run, cli.resume = orig


def test_run_and_resume_take_the_same_setting_flags():
    import argparse
    from adversary import cli
    def flags(cmd):
        p = argparse.ArgumentParser()
        cli.add_settings(p, resume=(cmd == "resume"))
        return {o for act in p._actions for o in act.option_strings}
    assert flags("run") == flags("resume")


def test_resume_keeps_unspecified_settings_and_overlays_given_ones():
    from adversary.cli import apply_settings
    cfg = RunConfig(task="t", repo="/x", run_dir="/r", sock="/s", base_sha="abc", worker_model="sonnet",
                    max_exchanges=30, worker_effort="high", strict_approvals=False, verify_cmds=["make e2e"])
    a = _parse("resume")
    assert apply_settings(cfg, a) == []                       # nothing given: nothing changes
    assert cfg.worker_model == "sonnet" and cfg.max_exchanges == 30 and cfg.worker_effort == "high"
    a = _parse("resume", "--max-exchanges", "50", "--worker-model", "opus", "--strict-approvals",
               "--worker-permission-mode", "auto", "--verify-cmd", "make e2e", "--verify-cmd", "just lint")
    changed = apply_settings(cfg, a)
    assert cfg.max_exchanges == 50 and cfg.worker_model == "opus" and cfg.strict_approvals is True
    assert cfg.worker_permission_mode == "auto" and cfg.verify_cmds == ["make e2e", "just lint"]
    assert "max-exchanges=50" in changed and "verify-cmd=just lint" in changed
    assert apply_settings(cfg, _parse("resume", "--no-strict-approvals")) == ["strict-approvals=False"]


def test_run_defaults_are_unchanged():
    a = _parse("run", "do it")
    assert (a.max_exchanges, a.stall_after, a.worker_effort, a.adversary_effort, a.strict_approvals,
            a.skip_permissions) == (30, 120, "high", "xhigh", False, False)
