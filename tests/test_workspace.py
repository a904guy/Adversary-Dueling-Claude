import os
import subprocess
from pathlib import Path

import pytest

from adversary import workspace

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture(autouse=True)
def git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, files: dict[str, str], ignore: str = "") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    if ignore:
        (path / ".gitignore").write_text(ignore)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit_all(tree, msg):
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", msg)


def test_folder_of_repos_gets_worktrees_and_shared_links(tmp_path):
    proj = tmp_path / "AnySite"
    app = make_repo(proj / "AnyDigits", {"app.py": "v1\n"}, ignore=".env\nnode_modules/\n")
    (app / ".env").write_text("SECRET=1\n")
    (app / "node_modules").mkdir()
    make_repo(proj / "libs" / "AnyFork", {"lib.py": "x\n"})
    (proj / ".work").mkdir()
    (proj / ".work" / "shot.png").write_text("img")
    (proj / "CLAUDE.md").write_text("rules")

    info = workspace.create(str(proj), str(tmp_path / "ws"), "42")
    ws = Path(info["path"])
    assert {r["path"] for r in info["repos"]} == {"AnyDigits", "libs/AnyFork"}
    assert all(r["branch"] == "adversary/card-42" and r["target"] == "main" for r in info["repos"])
    assert (ws / "AnyDigits" / ".git").is_file()                       # a worktree
    assert (ws / ".work").is_symlink() and (ws / "CLAUDE.md").is_symlink()
    assert (ws / "libs").is_dir() and not (ws / "libs").is_symlink()   # leads to a repo: a real folder
    assert (ws / "AnyDigits" / ".env").is_symlink() and (ws / "AnyDigits" / "node_modules").is_symlink()
    assert git(ws / "AnyDigits", "status", "--porcelain") == ""        # the links don't show as new files
    assert workspace.create(str(proj), str(tmp_path / "ws"), "42")["repos"] == info["repos"]   # reusable


def test_merge_brings_the_card_into_main_even_after_main_moved(tmp_path):
    src = make_repo(tmp_path / "app", {"a.py": "1\n", "b.py": "1\n"})
    info = workspace.create(str(src), str(tmp_path / "ws"), "7")
    ws = Path(info["path"])
    (ws / "a.py").write_text("card\n")
    with pytest.raises(workspace.MergeError, match="uncommitted"):
        workspace.merge(info)
    commit_all(ws, "card work")
    (src / "b.py").write_text("main moved\n")                          # another card landed meanwhile
    commit_all(src, "other card")
    done = workspace.merge(info)
    assert "merged into main" in done[0]
    assert (src / "a.py").read_text() == "card\n" and (src / "b.py").read_text() == "main moved\n"
    workspace.remove(info)
    assert not ws.exists() and "adversary/card-7" not in git(src, "branch")


def test_merge_conflict_goes_back_to_the_run(tmp_path):
    src = make_repo(tmp_path / "app", {"a.py": "1\n"})
    info = workspace.create(str(src), str(tmp_path / "ws"), "8")
    ws = Path(info["path"])
    (ws / "a.py").write_text("card\n")
    commit_all(ws, "card")
    (src / "a.py").write_text("main\n")
    commit_all(src, "main")
    with pytest.raises(workspace.MergeError, match="conflicts with main"):
        workspace.merge(info)
    assert (src / "a.py").read_text() == "main\n"                      # main untouched
    assert git(ws, "status", "--porcelain") == ""                      # merge aborted cleanly


def test_nothing_to_merge_and_remove_keeps_real_files(tmp_path):
    proj = tmp_path / "proj"
    make_repo(proj / "repo", {"a": "1"})
    (proj / "notes.txt").write_text("keep me")
    info = workspace.create(str(proj), str(tmp_path / "ws"), "9")
    assert workspace.merge(info) == ["nothing to merge: no commits on the card branches"]
    workspace.remove(info)
    assert (proj / "notes.txt").read_text() == "keep me" and not (tmp_path / "ws").exists()
