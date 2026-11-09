import json
import subprocess
import tomllib
from pathlib import Path

import pytest

import hypruse
from hypruse import cli
from hypruse import hyprctl as hyprctl_mod

ROOT = Path(__file__).resolve().parent.parent


def test_version_sources_agree():
    # RELEASING.md requires bumping pyproject.toml (the PyPI build),
    # __init__.py (what `hypruse --version` prints), server.json (the MCP
    # registry entry), the skill and the Claude Code plugin; a release cut
    # with only one bumped would report the previous version forever
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert hypruse.__version__ == version

    server = json.loads((ROOT / "server.json").read_text())
    assert server["version"] == version
    # the registry rejects a package version that is not on PyPI yet, and
    # validates ownership against *that* version's README
    assert [p["version"] for p in server["packages"]] == [version]
    # the skill ships inside the wheel and names the release it describes
    skill = (ROOT / "skills" / "hypruse" / "SKILL.md").read_text()
    assert f'version: "{version}"' in skill
    # Claude Code keeps a plugin user on the cached copy until plugin.json's
    # version changes, so a release that skips it never reaches them
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == version
    # plugin.json overrides a marketplace entry's version, so a version in
    # the entry could only drift from it
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert [p.get("version") for p in market["plugins"]] == [None]


def test_claude_plugin_runs_its_own_locked_source():
    # Anthropic's plugin directory refuses a PyPI version pin (a release can
    # gain files after review), so the plugin runs the source it ships with
    # the dependency versions in the committed uv.lock, and nothing else
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    server = plugin["mcpServers"]["hypruse"]
    assert server == {"command": "uv", "args": [
        "run", "--quiet", "--locked", "--no-dev", "--project", "${CLAUDE_PLUGIN_ROOT}", "hypruse"]}
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    ours = [p for p in lock["package"] if p["name"] == "hypruse"]
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert ours and ours[0]["version"] == version  # a stale lock fails --locked


def test_registry_ownership_token_matches_server_name():
    # the MCP registry proves we own the PyPI package by finding
    # `mcp-name: <name>` in the README it published as the package
    # description, and a PyPI release is immutable: a name that drifts from
    # the token can only be fixed by cutting another version
    name = json.loads((ROOT / "server.json").read_text())["name"]
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["readme"] == "README.md"

    readme = (ROOT / "README.md").read_text()
    token = f"mcp-name: {name}"
    assert token in readme
    # the registry requires a boundary after the name, so the token cannot be
    # read as the prefix of a longer one: anything outside the server-name
    # charset, or an HTML comment close
    rest = readme.split(token, 1)[1]
    assert not rest[:1].isalnum() and rest[:1] not in ".-_/" or rest.startswith(("-->", "--!>"))


@pytest.fixture
def uvx_only(monkeypatch):
    """No installed hypruse on PATH, so init falls back to uvx."""
    real = cli.shutil.which
    monkeypatch.setattr(cli.shutil, "which", lambda t: None if t == "hypruse" else real(t))


def test_merge_adds_entry_preserving_others(uvx_only):
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


def test_desktop_init_backs_up_and_writes(tmp_path, monkeypatch, uvx_only):
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


def test_stop_reaches_a_verb_acting_beside_a_server(tmp_path, monkeypatch, capsys):
    # a verb acting while a server owns the beacon is findable only through
    # the lock it holds; the panic bind must stop it too
    from hypruse import verbs

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = tmp_path / "hypruse"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"pid": 4242, "started": 1}))
    held = verbs._lock()  # this process stands in for the verb
    held.seek(0)
    held.truncate()
    held.write("7777")  # a pid that is not ours, so stop() may signal it
    held.flush()
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    try:
        assert cli._running_verb_pid() == 7777
        assert cli.stop() == 0
    finally:
        held.close()
    assert killed == [(7777, cli.signal.SIGTERM), (4242, cli.signal.SIGTERM)]
    out = capsys.readouterr().out
    assert "stopped a running hypruse verb (pid 7777)" in out
    assert "stopped hypruse (pid 4242)" in out
    # lock released: nothing acting any more
    assert cli._running_verb_pid() is None


def test_init_yes_does_not_write_into_agent_directories_unasked(monkeypatch, capsys):
    from hypruse import skill

    installs = []
    monkeypatch.setattr(skill, "install", lambda agents, copy: installs.append(agents) or 0)
    cli._init_skill(assume_yes=True, wanted=False)
    assert installs == [] and "skipped" in capsys.readouterr().out
    cli._init_skill(assume_yes=True, wanted=True)
    assert installs == [[]]


def test_a_verbs_beacon_is_not_removed_once_a_server_took_the_file(tmp_path, monkeypatch):
    from hypruse import safety

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(safety, "_armed", True)  # keep atexit/signal out of the test process
    safety.init()
    beacon = safety.state_path()
    assert json.loads(beacon.read_text())["pid"] == cli.os.getpid()
    beacon.write_text(json.dumps({"pid": 4242, "started": 2}))  # a server started meanwhile
    safety.shutdown()
    assert json.loads(beacon.read_text())["pid"] == 4242
    safety.init()
    safety.shutdown()
    assert not beacon.exists()


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


@pytest.mark.parametrize(
    "help_text, expect",
    [("  -T <identifier> Set the identifier of a foreign toplevel", "even hidden or covered"),
     ("  -o <output> Set the output name to capture.", "crop the screen")],
)
def test_doctor_says_whether_grim_can_capture_a_window_itself(monkeypatch, help_text, expect):
    class Done:
        stdout, stderr = help_text, ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(hyprctl_mod, "query", lambda what: [{"stableId": "18000010"}])
    ok, detail = cli._window_capture_note()
    assert ok is True and expect in detail  # informational, never a failure


def test_doctor_needs_window_ids_from_hyprland_too(monkeypatch):
    # grim -T captures by the window id Hyprland reports from 0.54 on
    class Done:
        stdout, stderr = "  -T <identifier> Set the identifier", ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(hyprctl_mod, "query", lambda what: [{"address": "0xa"}])
    ok, detail = cli._window_capture_note()
    assert ok is True and "0.54" in detail and "crop the screen" in detail


def test_doctor_does_not_vouch_for_window_ids_it_could_not_see(monkeypatch):
    class Done:
        stdout, stderr = "  -T <identifier> Set the identifier", ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(hyprctl_mod, "query", lambda what: [])
    ok, detail = cli._window_capture_note()
    assert ok is True and "no window" in detail


def test_init_registers_the_installed_hypruse_when_there_is_one(monkeypatch, tmp_path):
    # a client started outside a login shell may not find uvx (a mise shim
    # on Omarchy), so an AUR or `uv tool install` copy is used directly
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda t: "/usr/bin/hypruse")
    assert cli.server_command() == ["/usr/bin/hypruse"]
    assert cli.desktop_entry()["command"] == "/usr/bin/hypruse"
    assert cli.desktop_entry()["args"] == []
    assert "command: /usr/bin/hypruse" in cli.generic_snippet()


def test_init_does_not_register_uvxs_temporary_copy(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    temp = str(cache / "uv" / "archive-v0" / "abc" / "bin" / "hypruse")
    monkeypatch.setattr(cli.shutil, "which", lambda t: temp)
    assert cli.server_command() == ["uvx", "hypruse"]


def test_codex_init_registers_once(monkeypatch, capsys):
    runs = []

    def run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[2] == "get" else 0, "", "")

    monkeypatch.setattr(cli.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(cli.subprocess, "run", run)
    cli._init_codex(assume_yes=True)
    assert runs[-1] == ["codex", "mcp", "add", "hypruse", "--", "/usr/bin/hypruse"]


def test_codex_init_leaves_an_existing_entry_alone(monkeypatch, capsys):
    runs = []

    def run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "hypruse", "")

    monkeypatch.setattr(cli.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(cli.subprocess, "run", run)
    cli._init_codex(assume_yes=True)
    assert [c[2] for c in runs] == ["get"]
    assert "already configured" in capsys.readouterr().out


def test_doctor_fails_without_an_accessibility_bus(monkeypatch):
    from hypruse import a11y

    def no_bus():
        raise a11y.A11yError("no at-spi bus")

    monkeypatch.setattr(a11y, "connect", no_bus)
    ok, detail = cli._check_accessibility()
    assert not ok and "at-spi2-core" in detail


def test_doctor_names_the_flag_for_a_browser_without_a_tree(monkeypatch):
    from hypruse import a11y, hyprctl

    class Bus:
        def conn_pid(self, svc):
            return {":1.1": 100}[svc]

    monkeypatch.setattr(a11y, "connect", lambda: Bus())
    monkeypatch.setattr(a11y, "apps", lambda bus: [(":1.1", "/root")])
    monkeypatch.setattr(a11y, "same_app", lambda a, b: a == b)
    monkeypatch.setattr(hyprctl, "query", lambda what: [
        {"class": "org.gnome.Nautilus", "pid": 100},  # has a tree
        {"class": "chromium", "pid": 200},  # no tree
    ])
    ok, detail = cli._check_accessibility()
    assert ok, detail
    assert "chromium" in detail and "~/.config/chromium-flags.conf" in detail
    assert "--force-renderer-accessibility" in detail and "Nautilus" not in detail
