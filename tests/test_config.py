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
