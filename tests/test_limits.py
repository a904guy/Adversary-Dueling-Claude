import argparse

import pytest

from adversary import limits
from adversary.config import RunConfig, write_launch_files


def cfg_for(tmp_path, **kw):
    run = tmp_path / "20261017-120000-0421"
    run.mkdir()
    return RunConfig(task="t", repo=str(tmp_path), run_dir=str(run), sock="/s", base_sha="abc", **kw)


def test_unlimited_runs_are_launched_as_before(tmp_path):
    cfg = cfg_for(tmp_path)
    assert limits.prefix(cfg) == [] and limits.ensure_slice(cfg) is None
    for script in write_launch_files(cfg).values():
        assert "systemd-run" not in script.read_text()


def test_a_limited_run_gets_its_own_slice_and_every_agent_joins_it(tmp_path, monkeypatch):
    monkeypatch.setattr(limits.shutil, "which", lambda exe: "/usr/bin/" + exe)
    cfg = cfg_for(tmp_path, cpu_quota="100%", memory_max="3G")
    units = tmp_path / "units"
    name = limits.ensure_slice(cfg, directory=units, reload=False)
    assert name == "adversary-run_20261017_120000_0421.slice"     # no dashes: they'd mean nesting
    assert (units / name).read_text().splitlines()[-2:] == ["CPUQuota=100%", "MemoryMax=3G"]
    for script in write_launch_files(cfg).values():
        last = script.read_text().splitlines()[-1]
        assert last.startswith("exec systemd-run ") and f"--slice={name}" in last and "--scope" in last
        assert last.split(" -- ", 1)[1].startswith("claude ")   # systemd-run execs the agent in place


def test_limits_are_settings_resume_can_change(tmp_path):
    from adversary.cli import apply_settings
    cfg = cfg_for(tmp_path, cpu_quota="100%")
    changed = apply_settings(cfg, argparse.Namespace(cpu_quota="200%", memory_max="4G"))
    assert (cfg.cpu_quota, cfg.memory_max) == ("200%", "4G") and "cpu-quota=200%" in changed


def test_without_systemd_a_limited_run_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(limits.shutil, "which", lambda exe: None)
    with pytest.raises(RuntimeError, match="need systemd"):
        limits.ensure_slice(cfg_for(tmp_path, cpu_quota="100%"))


def test_the_pool_defaults_to_one_run_per_cpu_thread(monkeypatch):
    from adversary import cli
    monkeypatch.setattr(limits.os, "sched_getaffinity", lambda pid: set(range(32)))
    assert cli.default_pool() == 32


def test_cpu_auto_keeps_one_thread_free_for_the_os(monkeypatch):
    from adversary import cli
    assert limits.cpu_share(4, total=4) == "75%"            # hti.dev: 4 runs x 75% = 3 cores
    assert limits.cpu_share(32, total=32) == "96%"          # 32 runs share 31 threads
    assert limits.cpu_share(1, total=4) == "300%"           # a single run may use all but one
    monkeypatch.setattr(limits, "threads", lambda: 4)
    monkeypatch.setattr(limits, "total_memory", lambda: 8 << 30)
    assert cli.pool_memory(["--cpu-quota", "auto"], 4) == ["--cpu-quota", "75%", "--memory-max", "1792M"]


def test_memory_is_shared_out_as_ram_minus_a_gigabyte_over_the_pool(monkeypatch):
    from adversary import cli
    gb = 1 << 30
    assert limits.memory_share(4, total=8 * gb) == "1792M"                   # (8 - 1) / 4 GB
    monkeypatch.setattr(limits, "total_memory", lambda: 33 * gb)
    assert cli.pool_memory(["--cpu-quota", "100%"], 32) == ["--cpu-quota", "100%", "--memory-max", "1024M"]
    assert cli.pool_memory(["--memory-max", "auto"], 4) == ["--memory-max", "8192M"]
    assert cli.pool_memory(["--memory-max=auto"], 4) == ["--memory-max=8192M"]
    assert cli.pool_memory(["--cpu-quota", "100%", "--memory-max", "2G"], 4)[-1] == "2G"   # given: kept
    assert cli.pool_memory(["--worker-permission-mode", "auto"], 4) == ["--worker-permission-mode", "auto"]


def test_a_finished_runs_slice_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(limits.shutil, "which", lambda exe: "/usr/bin/" + exe)
    cfg = cfg_for(tmp_path, cpu_quota="100%")
    units = tmp_path / "units"
    name = limits.ensure_slice(cfg, directory=units, reload=False)
    limits.remove_slice(cfg, directory=units, reload=False)
    assert not (units / name).exists()
    limits.remove_slice(cfg, directory=units, reload=False)      # already gone: fine
