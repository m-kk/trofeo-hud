"""launchd agent: label naming and legacy-agent retirement on (un)install."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trofeo_hud import agent


def _cp(rc: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["launchctl"], rc, "", "")


@pytest.fixture
def plists(tmp_path, monkeypatch):
    new = tmp_path / f"{agent.LABEL}.plist"
    old = tmp_path / f"{agent.LEGACY_LABEL}.plist"
    monkeypatch.setattr(agent, "PLIST", new)
    monkeypatch.setattr(agent, "LEGACY_PLIST", old)
    return new, old


def test_labels_are_distinct_and_renamed():
    assert agent.LABEL == "io.github.m-kk.trofeo-hud"
    assert agent.LEGACY_LABEL == "com.varlogchris.claude-trofeo-hud"
    assert agent.LABEL != agent.LEGACY_LABEL
    assert agent.PLIST.name == f"{agent.LABEL}.plist"
    assert agent.LEGACY_PLIST.name == f"{agent.LEGACY_LABEL}.plist"


def test_plist_runs_renamed_module(tmp_path):
    d = agent._plist_dict(tmp_path)
    assert d["Label"] == agent.LABEL
    assert d["ProgramArguments"][1:] == ["-m", "trofeo_hud", "run"]


def test_uninstall_boots_out_legacy_then_current(plists, monkeypatch, capsys):
    new, old = plists
    new.write_bytes(b"x")
    old.write_bytes(b"x")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(agent, "_launchctl",
                        lambda *a: (calls.append(a), _cp(0))[1])
    agent.uninstall()
    assert [c[1].rsplit("/", 1)[1] for c in calls] == [
        agent.LEGACY_LABEL, agent.LABEL]
    assert not new.exists() and not old.exists()
    out = capsys.readouterr().out
    assert f"legacy agent {agent.LEGACY_LABEL}" in out
    assert f"stopped and removed {agent.LABEL}" in out


def test_uninstall_quiet_when_nothing_running(plists, monkeypatch, capsys):
    monkeypatch.setattr(agent, "_launchctl", lambda *a: _cp(1))
    agent.uninstall(quiet=True)
    assert capsys.readouterr().out == ""


def test_uninstall_reports_removed_but_not_stopped(plists, monkeypatch, capsys):
    monkeypatch.setattr(agent, "_launchctl", lambda *a: _cp(1))
    agent.uninstall()
    out = capsys.readouterr().out
    assert "legacy" not in out
    assert out.strip() == f"removed {agent.LABEL}"


def test_install_writes_plist_and_bootstraps(plists, monkeypatch, tmp_path):
    new, _ = plists
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(agent, "_launchctl",
                        lambda *a: (calls.append(a), _cp(0))[1])
    agent.install(tmp_path / "logs")
    assert new.exists()
    assert calls[-1][:2] == ("bootstrap", f"gui/{__import__('os').getuid()}")
    assert (tmp_path / "logs").is_dir()


def test_install_raises_when_bootstrap_fails(plists, monkeypatch, tmp_path):
    def fake(*a):
        if a[0] == "bootstrap":
            return subprocess.CompletedProcess(a, 1, "", "nope")
        return _cp(0)
    monkeypatch.setattr(agent, "_launchctl", fake)
    with pytest.raises(SystemExit, match="nope"):
        agent.install(tmp_path / "logs")


def test_launchctl_wrapper_invokes_subprocess(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd) or _cp(0))
    agent._launchctl("print", "gui/1")
    assert seen["cmd"] == ["launchctl", "print", "gui/1"]
    assert isinstance(Path(agent.PLIST), Path)
