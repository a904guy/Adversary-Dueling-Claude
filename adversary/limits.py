"""CPU and memory limits for a run (--cpu-quota, --memory-max), enforced by a systemd slice.

Each run gets its own slice, adversary-run_<id>.slice; its worker, supervisor and bridge are each
started into it with `systemd-run --scope --slice=…`, so everything they launch (Bash commands, MCP
servers, test runners) is held to the run's limits by the kernel and can't escape them. The panes
live under a tmux server that may be shared with other sessions, so the processes join the slice
themselves rather than inheriting it. `systemd-run --scope` execs the command in place, so a
pane's pid is still the agent's.

As root the slice is a system unit (/run/systemd/system); otherwise a user unit
($XDG_RUNTIME_DIR/systemd/user), which needs cgroup delegation for CPU limits. Both live under
/run, so they go away at reboot; a resumed run writes its slice again.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path


def slice_name(cfg) -> str:
    # A "-" in a slice name means nesting, so the run id's dashes become "_": adversary.slice holds them all.
    return f"adversary-run_{re.sub(r'[^A-Za-z0-9_]', '_', Path(cfg.run_dir).name)}.slice"


def _user() -> bool:
    return os.geteuid() != 0


def unit_dir() -> Path:
    if not _user():
        return Path("/run/systemd/system")
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "systemd" / "user"


HEADROOM = 1 << 30   # memory kept back for everything else on the machine when sharing it out


def total_memory() -> int:
    """The machine's RAM in bytes (MemTotal)."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("can't read MemTotal from /proc/meminfo")


def memory_share(pool: int, total: int | None = None) -> str:
    """A run's share of memory when `pool` runs share the machine: (RAM - 1 GB) / pool, as "<n>M"."""
    total = total_memory() if total is None else total
    return f"{max(256 << 20, (total - HEADROOM) // max(1, pool)) >> 20}M"


def threads() -> int:
    """CPU threads this process may use (its affinity, else the machine's count)."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def cpu_share(pool: int, total: int | None = None) -> str:
    """A run's CPU quota when `pool` runs share the machine with one thread kept free for the OS:
    (threads - 1) / pool cores, as a percentage (at least 10%)."""
    total = threads() if total is None else total
    return f"{max(10, (max(1, total - 1) * 100) // max(1, pool))}%"


def limited(cfg) -> bool:
    return bool(cfg.cpu_quota or cfg.memory_max)


def ensure_slice(cfg, directory: Path | None = None, reload: bool = True) -> str | None:
    """Write (or rewrite) the run's slice with its limits; returns its name, or None if unlimited.
    Raises RuntimeError when systemd isn't there to enforce them."""
    if not limited(cfg):
        return None
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        raise RuntimeError("--cpu-quota/--memory-max need systemd (systemd-run and systemctl)")
    name = slice_name(cfg)
    d = directory or unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    lines = ["[Unit]", f"Description=adversary run {Path(cfg.run_dir).name}", "", "[Slice]"]
    if cfg.cpu_quota:
        lines.append(f"CPUQuota={cfg.cpu_quota}")
    if cfg.memory_max:
        lines.append(f"MemoryMax={cfg.memory_max}")
    (d / name).write_text("\n".join(lines) + "\n")
    if reload:
        r = subprocess.run(["systemctl", *(["--user"] if _user() else []), "daemon-reload"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"systemctl daemon-reload failed: {r.stderr.strip()}")
    return name


def remove_slice(cfg, directory: Path | None = None, reload: bool = True) -> None:
    """Drop a finished run's slice unit, so finished runs don't pile up in systemd until reboot."""
    path = (directory or unit_dir()) / slice_name(cfg)
    if not path.exists():
        return
    path.unlink()
    if reload and shutil.which("systemctl"):
        flag = ["--user"] if _user() else []
        subprocess.run(["systemctl", *flag, "stop", slice_name(cfg)], capture_output=True)
        subprocess.run(["systemctl", *flag, "daemon-reload"], capture_output=True)


def prefix(cfg) -> list[str]:
    """What to put before a command so it runs inside the run's slice ([] when unlimited)."""
    if not limited(cfg):
        return []
    return ["systemd-run", *(["--user"] if _user() else []), "--scope", "--quiet", "--collect",
            f"--slice={slice_name(cfg)}", "--"]


def oom_kills(cfg, root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """How many processes the kernel has killed in the run's slice for running out of its memory
    (memory.events counts the whole slice), or None when it can't be read (no limits, no cgroup v2)."""
    if not limited(cfg):
        return None
    name = slice_name(cfg)   # a system slice as root, else under the user's manager
    for path in [root / "adversary.slice" / name / "memory.events",
                 *root.glob(f"user.slice/*/user@*.service/adversary.slice/{name}/memory.events")]:
        if not path.exists():
            continue
        try:
            for line in path.read_text().splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return int(value)
        except (OSError, ValueError):
            return None
    return None
