"""Per-card workspaces, so several runs can work on one project at once without trampling each
other's files, and the merge back into each repo's main branch when a card is done.

A workspace mirrors the project folder:

- every git repo in it (the folder itself, or repos inside it a level or two down) becomes a git
  worktree on its own branch, `adversary/card-<id>`, so each card's code changes are separate;
- everything else is a symlink to the real thing, shared by all cards: scratch folders,
  .claude settings, and inside each repo its ignored files (.env, .venv, node_modules, data) —
  a fresh worktree has none of those, and the app wouldn't run without them.

The symlinks are named in the repo's info/exclude so a worker's `git add -A` can't commit them
(a pattern like `node_modules/` doesn't match a symlink). When the card is done, each repo's
branch is merged into the branch its main checkout is on: main's newer commits, and the remote's,
are merged into the card branch first (a conflict goes back to the run to resolve), then main
fast-forwards. Then
each repo whose main checkout has a remote has its branch pushed, if it's ahead (see push).
"""

import os
import shutil
import subprocess
from pathlib import Path

from adversary import snapshot


class MergeError(Exception):
    """The card's work can't be merged yet. `needs_person`: the fix is outside the workspace (the
    main checkout is on another branch, or has local changes in the way), so the worker can't do it."""

    def __init__(self, msg: str, needs_person: bool = False):
        super().__init__(msg)
        self.needs_person = needs_person


def git(cwd, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, ["git", *args], r.stdout, r.stderr)
    return r.stdout.strip()


def _is_repo(p: Path) -> bool:
    return (p / ".git").exists()


def _git_dirs(root: Path, max_depth: int = 2) -> list[str]:
    """Repos inside a folder that isn't one, relative to it (as find_git_subdirs does)."""
    found: list[str] = []

    def scan(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_symlink() or not child.is_dir() or child.name in snapshot.SKIP_DIRS:
                continue
            if _is_repo(child):
                found.append(str(child.relative_to(root)))
            else:
                scan(child, depth + 1)

    scan(root, 1)
    return found


def branch_name(card_id: str) -> str:
    return f"adversary/card-{card_id}"


def _exclude(repo: Path, names: list[str]) -> None:
    """Name `names` (paths at the worktree root) in the repo's shared info/exclude."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    have = set(path.read_text().splitlines()) if path.exists() else set()
    add = [f"/{n}" for n in names if f"/{n}" not in have]
    if add:
        with path.open("a") as f:
            f.write("\n# adversary card workspaces: shared files linked in from the main checkout\n"
                    + "\n".join(add) + "\n")


def _link_ignored(source: Path, tree: Path) -> list[str]:
    """Symlink the source repo's ignored top-level entries (.env, .venv, data/…) into `tree`."""
    out = git(source, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", check=False)
    tops = sorted({line.rstrip("/").split("/")[0] for line in out.splitlines() if line.strip()})
    linked = []
    for name in tops:
        src, dst = source / name, tree / name
        if name == ".git" or dst.exists() or dst.is_symlink() or not (src.exists() or src.is_symlink()):
            continue
        dst.symlink_to(src)
        linked.append(name)
    return linked


def _add_worktree(source: Path, tree: Path, card_id: str) -> dict:
    target = git(source, "rev-parse", "--abbrev-ref", "HEAD")
    if target == "HEAD":
        raise RuntimeError(f"{source} is on a detached HEAD; check out the branch cards should merge into")
    branch = branch_name(card_id)
    exists = git(source, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if exists:
        git(source, "worktree", "prune")
        git(source, "worktree", "add", str(tree), branch)
    else:
        git(source, "worktree", "add", "-b", branch, str(tree), target)
    linked = _link_ignored(source, tree)
    if linked:
        _exclude(source, linked)
    return {"source": str(source), "branch": branch, "target": target, "start": git(tree, "rev-parse", "HEAD")}


def create(folder: str, dest: str, card_id: str) -> dict:
    """Build the workspace for a card at `dest` (reused if it's already there). Returns
    {"folder", "path", "repos": [{"path": relative ("" = the root), "source", "branch", "target",
    "start"}]}, kept in the run's config so the bridge can merge and the manager clean up."""
    src, ws = Path(folder).resolve(), Path(dest)
    info = {"folder": str(src), "path": str(ws), "repos": []}
    if _is_repo(src):
        if not ws.exists():
            ws.parent.mkdir(parents=True, exist_ok=True)
            info["repos"].append({"path": "", **_add_worktree(src, ws, card_id)})
        else:
            info["repos"].append({"path": "", **_existing(src, ws, card_id)})
        return info
    repos = _git_dirs(src)
    ws.mkdir(parents=True, exist_ok=True)
    for rel in repos:
        tree = ws / rel
        if tree.exists() and _is_repo(tree):
            info["repos"].append({"path": rel, **_existing(src / rel, tree, card_id)})
            continue
        tree.parent.mkdir(parents=True, exist_ok=True)
        info["repos"].append({"path": rel, **_add_worktree(src / rel, tree, card_id)})
    _link_rest(src, ws, set(repos))
    return info


def _existing(source: Path, tree: Path, card_id: str) -> dict:
    target = git(source, "rev-parse", "--abbrev-ref", "HEAD")
    return {"source": str(source), "branch": branch_name(card_id), "target": target,
            "start": git(tree, "merge-base", "HEAD", target, check=False) or git(tree, "rev-parse", "HEAD")}


def _link_rest(src: Path, ws: Path, repos: set[str], rel: str = "") -> None:
    """Symlink every entry of src/rel into ws/rel, except repos and the folders leading to them."""
    here = src / rel
    depth = len(Path(rel).parts)
    leading = {Path(r).parts[depth] for r in repos}   # (repos holds only those under rel)
    for entry in sorted(here.iterdir()):
        r = f"{rel}/{entry.name}" if rel else entry.name
        dst = ws / r
        if r in repos:
            continue
        if entry.name in leading and entry.is_dir() and not entry.is_symlink():
            dst.mkdir(exist_ok=True)
            _link_rest(src, ws, {x for x in repos if x.startswith(r + "/")}, r)
            continue
        if not dst.exists() and not dst.is_symlink():
            dst.symlink_to(entry)


def merge(info: dict) -> list[str]:
    """Merge each repo's card branch into its target. Returns what happened, per repo. Raises
    MergeError when the work isn't committed or doesn't merge cleanly (the message says what to
    fix); repos merged before the failing one stay merged, and the message says which."""
    ws = Path(info["path"])
    trees = [(r["path"] or Path(info["folder"]).name, r, ws / r["path"] if r["path"] else ws) for r in info["repos"]]
    dirty = [f"{name}: uncommitted changes on {repo['branch']} — commit them (or discard what shouldn't "
             f"ship):\n{status[:800]}"
             for name, repo, tree in trees if (status := git(tree, "status", "--porcelain", check=False))]
    if dirty:
        raise MergeError("\n\n".join(dirty))
    done: list[str] = []
    for name, repo, tree in trees:
        ahead = git(tree, "rev-list", "--count", f"{repo['target']}..HEAD", check=False)
        if ahead in ("", "0"):
            continue
        source = Path(repo["source"])
        on = git(source, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        if on != repo["target"]:
            raise MergeError(_so_far(f"{name}: its main checkout is on {on!r}, not {repo['target']!r}, so "
                                     f"nothing was merged there; check out {repo['target']} in {source}.", done),
                             needs_person=True)
        # The remote's newer commits come in too (pushed from elsewhere), so the branch pushed
        # afterwards is a fast-forward there; a conflict with them goes to the worker like any other.
        incoming = [repo["target"]]
        if remote := _remote(source, repo["target"]):
            _network(source, "fetch", "--quiet", remote, repo["target"])
            if git(source, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{repo['target']}", check=False):
                incoming.append(f"{remote}/{repo['target']}")
        for attempt in range(2):   # main can move on (another card merged) between the two steps
            # Bring the target's newer commits into the card branch, then fast-forward the target.
            for ref in incoming:
                r = subprocess.run(["git", "merge", "--no-edit", ref], cwd=tree, capture_output=True, text=True)
                if r.returncode != 0:
                    subprocess.run(["git", "merge", "--abort"], cwd=tree, capture_output=True)
                    raise MergeError(_so_far(f"{name}: {repo['branch']} conflicts with {ref} — merge {ref} into "
                                             f"it, resolve the conflicts and commit:\n"
                                             f"{(r.stdout + r.stderr)[:800]}", done))
            r = subprocess.run(["git", "merge", "--ff-only", repo["branch"]], cwd=source, capture_output=True, text=True)
            if r.returncode == 0:
                break
        else:
            raise MergeError(_so_far(f"{name}: couldn't fast-forward {repo['target']} in {source} to "
                                     f"{repo['branch']} (uncommitted local changes there in the same files?):\n"
                                     f"{(r.stdout + r.stderr)[:800]}", done), needs_person=True)
        done.append(f"{name}: {repo['branch']} merged into {repo['target']} ({ahead} commit{'s' if ahead != '1' else ''})")
    return done or ["nothing to merge: no commits on the card branches"]


PUSH_TIMEOUT = 120   # seconds; a push that would prompt for credentials fails instead (no terminal)


def _remote(source: Path, branch: str) -> str | None:
    """Where `branch` pushes: its upstream's remote, else origin, else the repo's only remote."""
    upstream = git(source, "config", f"branch.{branch}.remote", check=False)
    remotes = git(source, "remote", check=False).split()
    if upstream in remotes:
        return upstream
    if "origin" in remotes:
        return "origin"
    return remotes[0] if len(remotes) == 1 else None


def _network(cwd: Path, *args: str) -> tuple[bool, str]:
    """A git command that talks to a remote: never prompts (no terminal to answer), and gives up
    after PUSH_TIMEOUT. Returns (ok, output)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true"}
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {PUSH_TIMEOUT}s"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def push(info: dict) -> tuple[list[str], list[str]]:
    """Push each repo's target branch to its remote when it has commits the remote doesn't (the
    card's merged work, and anything else committed there meanwhile). Returns (pushed, failed),
    one line per repo; a repo with no remote, or nothing new, is skipped. A failure leaves the
    merge in place: the work is on the local branch, just not on the remote yet."""
    pushed: list[str] = []
    failed: list[str] = []
    for repo in info["repos"]:
        name = repo["path"] or Path(info["folder"]).name
        source, branch = Path(repo["source"]), repo["target"]
        remote = _remote(source, branch)
        if not remote:
            continue
        _network(source, "fetch", "--quiet", remote, branch)   # what the remote has now
        has = git(source, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}", check=False)
        ahead = git(source, "rev-list", "--count", f"{remote}/{branch}..{branch}" if has else branch, check=False)
        if ahead in ("", "0"):
            continue
        ok, out = _network(source, "push", remote, f"{branch}:{branch}")
        n = f"{ahead} commit{'s' if ahead != '1' else ''}"
        if ok:
            pushed.append(f"{name}: pushed {branch} to {remote} ({n})")
        else:
            failed.append(f"{name}: couldn't push {branch} to {remote} ({n} only on this machine): {out[-600:]}")
    return pushed, failed


def commits(info: dict) -> list[str]:
    """The card's own commits not yet in each repo's target, oldest first, as "repo: subject"."""
    ws, out = Path(info["path"]), []
    for repo in info["repos"]:
        tree = ws / repo["path"] if repo["path"] else ws
        name = repo["path"] or Path(info["folder"]).name
        log = git(tree, "log", "--reverse", "--no-merges", "--format=%h %s", f"{repo['target']}..HEAD", check=False)
        out += [f"{name}: {line}" for line in log.splitlines() if line.strip()]
    return out


def _so_far(msg: str, done: list[str]) -> str:
    return msg + (f"\n\nAlready merged: {'; '.join(done)}" if done else "")


def remove(info: dict) -> None:
    """Remove the workspace: its worktrees, merged card branches, and the folder of links."""
    ws = Path(info["path"])
    for repo in info["repos"]:
        tree = ws / repo["path"] if repo["path"] else ws
        source = Path(repo["source"])
        git(source, "worktree", "remove", "--force", str(tree), check=False)
        git(source, "branch", "-d", repo["branch"], check=False)   # -d: only once merged
        git(source, "worktree", "prune", check=False)
    if ws.exists():
        # Only links and emptied folders are left; never follow a link into the real project.
        for dirpath, dirnames, filenames in os.walk(ws, topdown=False):
            for n in filenames + dirnames:
                p = Path(dirpath) / n
                if p.is_symlink():
                    p.unlink()
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        if ws.exists() and not any(ws.iterdir()):
            shutil.rmtree(ws, ignore_errors=True)
