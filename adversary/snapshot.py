"""Folder snapshot for repos without git: a manifest of file hashes plus a copy of the files.

The copy lets the adversary `diff -ru <snapshot> .` and doubles as a backup; the manifest
answers "what changed?" even when copying was skipped for size.
"""

import difflib
import hashlib
import json
import os
import shutil
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", ".next", "target", ".idea", ".cache"}
MAX_FILE = 5 * 1024 * 1024          # files larger than this are hashed but not copied
MAX_TOTAL = 500 * 1024 * 1024       # stop copying (manifest only) beyond this total


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                yield path


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def take(root: str, dest: str) -> dict:
    """Snapshot `root` into `dest` (dest/files/... + dest/manifest.json). Returns summary stats."""
    root_p, dest_p = Path(root), Path(dest)
    files_dir = dest_p / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    manifest, copied, skipped, total = {}, 0, 0, 0
    for path in _walk(root_p):
        rel = str(path.relative_to(root_p))
        try:
            size = path.stat().st_size
            manifest[rel] = _hash(path)
        except OSError:
            continue
        if size <= MAX_FILE and total + size <= MAX_TOTAL:
            target = files_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
            total += size
        else:
            skipped += 1
    (dest_p / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"files": len(manifest), "copied": copied, "not_copied": skipped, "bytes_copied": total}


def changes(root: str, dest: str) -> dict:
    """Files added / modified / deleted in `root` since the snapshot in `dest`."""
    before = json.loads((Path(dest) / "manifest.json").read_text())
    root_p = Path(root)
    now = {}
    for path in _walk(root_p):
        try:
            now[str(path.relative_to(root_p))] = _hash(path)
        except OSError:
            continue
    return {
        "added": sorted(set(now) - set(before)),
        "modified": sorted(p for p in now.keys() & before.keys() if now[p] != before[p]),
        "deleted": sorted(set(before) - set(now)),
    }


def text_lines(path: Path) -> list[bytes] | None:
    """A text file's lines, or None for a binary, oversized or unreadable one."""
    try:
        if path.stat().st_size > MAX_FILE:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.splitlines()


def line_stats(root: str, dest: str, paths: dict) -> tuple[int, int]:
    """(lines added, lines removed) across `paths` (a changes() result, or part of one): new
    files count in full, deleted ones by their copy, modified ones by a diff against it. Binary
    files, and files whose copy was skipped for size, add nothing."""
    root_p, files = Path(root), Path(dest) / "files"
    ins = dels = 0
    for p in paths.get("added", []):
        ins += len(text_lines(root_p / p) or [])
    for p in paths.get("deleted", []):
        dels += len(text_lines(files / p) or [])
    for p in paths.get("modified", []):
        old, new = text_lines(files / p), text_lines(root_p / p)
        if old is None or new is None:
            continue
        diff = difflib.diff_bytes(difflib.unified_diff, old, new, n=0, lineterm=b"")
        for line in list(diff)[2:]:   # past the ---/+++ header
            if line.startswith(b"+"):
                ins += 1
            elif line.startswith(b"-"):
                dels += 1
    return ins, dels


def format_changes(c: dict) -> str:
    lines = [f"{tag} {p}" for tag, key in (("A", "added"), ("M", "modified"), ("D", "deleted")) for p in c[key]]
    return "\n".join(lines) or "(no changes)"
