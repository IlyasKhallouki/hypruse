import json
import tomllib
from pathlib import Path

import pytest

import hypruse
from hypruse import cli


def test_version_sources_agree():
    # RELEASING.md requires bumping BOTH pyproject.toml (the PyPI build)
    # and __init__.py (what `hypruse --version` prints); a release cut with
    # only one bumped would report the previous version forever
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert hypruse.__version__ == version


def test_merge_adds_entry_preserving_others():
    cfg = {"mcpServers": {"platform": {"type": "http", "url": "https://x"}}, "other": 1}
    merged, changed = cli.merge_desktop_config(cfg)
    assert changed
    assert merged["mcpServers"]["platform"]["url"] == "https://x"
    assert merged["mcpServers"]["hypruse"]["command"] == "uvx"
    assert merged["other"] == 1


def test_merge_never_touches_existing_hypruse_entry():
    mine = {"command": "uv", "args": ["run", "--directory", "/src", "hypruse"]}
    cfg = {"mcpServers": {"hypruse": mine}}
    merged, changed = cli.merge_desktop_config(cfg)
    assert not changed
    assert merged["mcpServers"]["hypruse"] is mine


def test_merge_from_empty():
    merged, changed = cli.merge_desktop_config({})
    assert changed and "hypruse" in merged["mcpServers"]


def test_desktop_init_backs_up_and_writes(tmp_path, monkeypatch):
    cfgfile = tmp_path / "claude_desktop_config.json"
    cfgfile.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(cli, "DESKTOP_CONFIG", cfgfile)
    cli._init_claude_desktop(assume_yes=True)
    written = json.loads(cfgfile.read_text())
    assert written["mcpServers"]["hypruse"]["args"] == ["hypruse"]
    backups = list(tmp_path.glob("*.bak.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"mcpServers": {}}


def test_desktop_init_skips_when_configured(tmp_path, monkeypatch, capsys):
    cfgfile = tmp_path / "claude_desktop_config.json"
    cfgfile.write_text(json.dumps({"mcpServers": {"hypruse": {"command": "uv"}}}))
    monkeypatch.setattr(cli, "DESKTOP_CONFIG", cfgfile)
    cli._init_claude_desktop(assume_yes=True)
    assert "already configured" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.bak.*"))


def test_desktop_init_refuses_broken_json(tmp_path, monkeypatch, capsys):
    cfgfile = tmp_path / "claude_desktop_config.json"
    cfgfile.write_text("{not json")
    monkeypatch.setattr(cli, "DESKTOP_CONFIG", cfgfile)
    cli._init_claude_desktop(assume_yes=True)
    assert "not valid JSON" in capsys.readouterr().out
    assert cfgfile.read_text() == "{not json"  # untouched


def test_check_deps_reports_missing(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda t: None)
    ok, detail = cli._check_deps()
    assert not ok and "grim" in detail and "wtype" in detail


def test_doctor_never_crashes_on_broken_check(monkeypatch, capsys):
    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(cli, "CHECKS", (("broken", boom),))
    assert cli.doctor() == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "kaput" in out


def test_main_dispatch(monkeypatch):
    monkeypatch.setattr(cli, "doctor", lambda: 0)
    monkeypatch.setattr(cli.sys, "argv", ["hypruse", "doctor"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0

    monkeypatch.setattr(cli, "stop", lambda: 0)
    monkeypatch.setattr(cli.sys, "argv", ["hypruse", "stop"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0

    monkeypatch.setattr(cli.sys, "argv", ["hypruse", "bogus"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2


def test_stop_no_beacon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert cli.stop() == 0
    assert "no active hypruse session" in capsys.readouterr().out


def test_stop_signals_beacon_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = tmp_path / "hypruse"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"pid": 4242, "started": 1}))
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert cli.stop() == 0
    assert killed == [(4242, cli.signal.SIGTERM)]
    assert "stopped hypruse (pid 4242)" in capsys.readouterr().out


def test_stop_handles_non_dict_beacon(tmp_path, monkeypatch, capsys):
    # valid JSON that is not an object must not crash the emergency stop
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = tmp_path / "hypruse"
    d.mkdir()
    (d / "state.json").write_text("[1, 2, 3]")
    assert cli.stop() == 1
    assert "unreadable" in capsys.readouterr().out


def test_stop_handles_dict_beacon_missing_pid(tmp_path, monkeypatch, capsys):
    # a dict beacon without a pid key hits the KeyError branch the fix added
    # (int({...}["pid"]) raises KeyError, must be caught)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = tmp_path / "hypruse"
    d.mkdir()
    (d / "state.json").write_text('{"started": 1}')
    assert cli.stop() == 1
    assert "unreadable" in capsys.readouterr().out


def test_stop_clears_stale_beacon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = tmp_path / "hypruse"
    d.mkdir()
    beacon = d / "state.json"
    beacon.write_text(json.dumps({"pid": 999999, "started": 1}))

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(cli.os, "kill", gone)
    assert cli.stop() == 0
    assert "already gone" in capsys.readouterr().out
    assert not beacon.exists()
