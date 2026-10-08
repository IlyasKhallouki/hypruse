"""The CLI verbs: the shell contract over the same tool functions the MCP
server registers. Nothing here touches a live desktop: the tool functions
are stubbed, and what is under test is the mapping from argv to a tool
call, the gates, the exit codes, the rendering, and the state that must
survive between one-shot processes."""

import json
import os

import pytest
from mcp.types import TextContent

from hypruse import cli, cli_state, journal, safety, session, trust, verbs
from hypruse import input as hinput
from hypruse import server as srv


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No desktop, no beacon, no shared state: each test starts empty."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig-test")
    for var in ("HYPRUSE_READONLY", "HYPRUSE_CLIPBOARD", "HYPRUSE_CONFINE", "HYPRUSE_STRICT",
                "HYPRUSE_MARK", "HYPRUSE_SCREENSHOT_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session, "ensure_session_env", lambda: None)
    monkeypatch.setattr(cli, "_take_beacon", lambda: False)
    monkeypatch.setattr(safety, "arm", lambda: None)
    monkeypatch.setattr(safety, "on_shutdown", lambda fn: None)
    monkeypatch.setattr(trust, "_seat", {"cursor": None, "active": None})
    monkeypatch.setattr(srv, "_last_marks", {})
    monkeypatch.setattr(cli_state, "_flags", {})
    trust._owned.clear()
    yield
    trust._owned.clear()


@pytest.fixture
def stub(monkeypatch):
    """Replace a tool function with a recorder that returns a canned result."""
    calls = []

    def make(tool, result):
        def fake(**kw):
            calls.append((tool, kw))
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(srv, tool, fake)
        return calls

    return make


def parse(*argv):
    ns = verbs.build_parser().parse_args(list(argv))
    return verbs.ALIASES.get(ns.verb, ns.verb), ns


def kwargs_of(*argv):
    verb, ns = parse(*argv)
    kw, _opts = verbs.normalize(verb, ns)
    return kw


# --- entry: help and version never start the server --------------------------


def test_help_prints_and_exits_zero_even_with_piped_stdin(monkeypatch, capsys):
    def boom():
        raise AssertionError("the MCP server must not start for --help")

    monkeypatch.setattr("hypruse.server.main", boom)
    for flag in ("--help", "-h"):
        monkeypatch.setattr(cli.sys, "argv", ["hypruse", flag])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 0
        out = capsys.readouterr().out
        assert "click_ui" in out and "wait_for" in out and "doctor" in out


def test_version_prints_without_the_server(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["hypruse", "--version"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("hypruse ")


def test_no_arguments_still_runs_the_server(monkeypatch):
    started = []
    monkeypatch.setattr("hypruse.server.main", lambda: started.append(True))
    monkeypatch.setattr(cli.sys, "argv", ["hypruse"])
    cli.main()
    assert started == [True]


def test_serve_is_the_explicit_form(monkeypatch):
    started = []
    monkeypatch.setattr("hypruse.server.main", lambda: started.append(True))
    assert verbs.main(["serve"]) == 0
    assert started == [True]


def test_owner_subcommands_are_looked_up_at_call_time(monkeypatch):
    monkeypatch.setattr(cli, "doctor", lambda: 7)
    assert verbs.main(["doctor"]) == 7
    seen = []
    monkeypatch.setattr(cli, "journal_cmd", lambda rest: seen.append(rest) or 0)
    assert verbs.main(["journal", "--acts", "-n", "3"]) == 0
    assert seen == [["--acts", "-n", "3"]]
    monkeypatch.setattr(cli, "init", lambda assume_yes, install_skill: 0 if install_skill else 5)
    assert verbs.main(["init", "--yes", "--skill"]) == 0
    assert verbs.main(["init"]) == 5


# --- argv to tool call --------------------------------------------------------


def test_only_passed_flags_reach_the_tool():
    assert kwargs_of("pointer", "click") == {"action": "click"}
    assert kwargs_of("pointer", "click", "800", "60", "--button", "right", "--double",
                     "--then", "ui") == {
        "action": "click", "x": 800.0, "y": 60.0, "button": "right", "double": True,
        "then": "ui",
    }
    assert kwargs_of("pointer", "move", "1", "2") == {"action": "move", "x": 1.0, "y": 2.0}
    assert kwargs_of("pointer", "drag", "1", "2", "3", "4", "--allow-auth") == {
        "action": "drag", "x": 1.0, "y": 2.0, "to_x": 3.0, "to_y": 4.0, "allow_auth": True,
    }
    assert kwargs_of("pointer", "scroll", "3") == {"action": "scroll", "scroll_dy": 3.0}
    assert kwargs_of("pointer", "scroll", "-2", "1", "--at", "10", "20") == {
        "action": "scroll", "scroll_dy": -2.0, "scroll_dx": 1.0, "x": 10.0, "y": 20.0,
    }


def test_keyboard_hypr_click_ui_launch_wait_for_mappings():
    assert kwargs_of("keyboard", "type", "hello", "--window", "0xabc") == {
        "action": "type", "text": "hello", "window": "0xabc",
    }
    assert kwargs_of("keyboard", "key", "ctrl+s") == {"action": "key", "keys": "ctrl+s"}
    assert kwargs_of("hypr", "move_window", "0xabc", "3") == {
        "action": "move_window", "target": "0xabc", "workspace": "3",
    }
    assert kwargs_of("hypr", "fullscreen") == {"action": "fullscreen"}
    assert kwargs_of("hypr", "workspace", "special:notes") == {
        "action": "workspace", "workspace": "special:notes",
    }
    assert kwargs_of("click_ui", "Save", "--index", "1") == {"name": "Save", "index": 1}
    assert kwargs_of("click-ui", "--mark", "3") == {"mark": 3}
    assert kwargs_of("use-bind", "SUPER+F") == {"combo": "SUPER+F"}
    assert kwargs_of("wait-for", "window_open", "--match", "kitty", "--timeout", "5") == {
        "event": "window_open", "match": "kitty", "timeout_s": 5.0,
    }
    assert kwargs_of("ui", "--all") == {"actionable": False}
    assert kwargs_of("launch", "firefox") == {"command": "firefox"}
    # several words are joined the way a shell would quote them
    assert kwargs_of("launch", "--workspace", "2", "--", "firefox", "--new-window", "a b") == {
        "command": "firefox --new-window 'a b'", "workspace": "2",
    }
    assert kwargs_of("launch", "kitty", "--wait", "3") == {"command": "kitty", "wait_s": 3.0}


def test_shell_side_options_are_split_from_tool_arguments():
    verb, ns = parse("zoom", "1", "2", "--size", "300x200", "--out", "/tmp/z.png", "--json")
    kw, opts = verbs.normalize(verb, ns)
    assert kw == {"x": 1.0, "y": 2.0, "size": "300x200"}
    assert opts == {"json_out": True, "out_path": "/tmp/z.png", "dry_run": False}
    verb, ns = parse("hypr", "workspace", "3", "--dry-run")
    kw, opts = verbs.normalize(verb, ns)
    assert kw == {"action": "workspace", "workspace": "3"} and opts["dry_run"] is True


def test_json_and_dry_run_are_accepted_after_the_action_word():
    # argparse hands everything after the action to that action's parser,
    # which is where an agent puts its flags; every such parser has them
    for argv in (
        ("hypr", "workspace", "3", "--dry-run", "--json"),
        ("pointer", "click", "1", "2", "--json", "--dry-run"),
        ("keyboard", "key", "esc", "--dry-run", "--json"),
        ("clipboard", "read", "--json"),
        ("clipboard", "write", "x", "--json", "--dry-run"),
        ("pointer", "--json", "click", "1", "2"),  # before the action still works
    ):
        verb, ns = parse(*argv)
        _kw, opts = verbs.normalize(verb, ns)
        assert opts["json_out"] is True, argv


def test_click_ui_needs_exactly_one_target_as_a_usage_error(capsys):
    for argv in (["click_ui"], ["click_ui", "Save", "--mark", "2"]):
        assert verbs.main(argv) == verbs.EXIT_USAGE
        assert "hypruse: usage: pass NAME or --mark N" in capsys.readouterr().err
    assert verbs.main(["sequence", "[]"]) == verbs.EXIT_USAGE
    assert "at least one step" in capsys.readouterr().err


def test_usage_errors_are_one_line(capsys):
    with pytest.raises(SystemExit):
        verbs.main(["keyboard", "type"])
    err = capsys.readouterr().err
    assert err.startswith("hypruse: usage:") and err.count("\n") == 1


def test_text_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("two\nlines\n"))
    assert kwargs_of("keyboard", "type", "-")["text"] == "two\nlines"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("secret"))
    assert kwargs_of("clipboard", "write", "-") == {"action": "write", "text": "secret"}


def test_sequence_steps_inline_file_and_stdin(tmp_path, monkeypatch):
    steps = [{"op": "hypr", "action": "workspace", "workspace": "3"}]
    assert kwargs_of("sequence", json.dumps(steps)) == {"steps": steps}
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    assert kwargs_of("sequence", f"@{f}", "--no-stop-on-change") == {
        "steps": steps, "stop_on_change": False,
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(steps)))
    assert kwargs_of("sequence", "-") == {"steps": steps}


def test_bad_steps_are_usage_errors(capsys):
    for bad in ("{not json", '{"op": "hypr"}', '[1, 2]', "@/nonexistent/steps.json"):
        assert verbs.main(["sequence", bad]) == verbs.EXIT_USAGE
        assert "hypruse: usage:" in capsys.readouterr().err


def test_click_with_one_coordinate_is_a_usage_error(capsys):
    assert verbs.main(["pointer", "click", "5"]) == verbs.EXIT_USAGE
    assert "click takes X Y" in capsys.readouterr().err


def test_scroll_with_a_non_numeric_dx_is_a_usage_error(capsys):
    assert verbs.main(["pointer", "scroll", "3", "left"]) == verbs.EXIT_USAGE
    assert "DX must be a number" in capsys.readouterr().err


def test_unknown_verb_and_unknown_action_exit_two(capsys):
    with pytest.raises(SystemExit) as e:
        verbs.main(["bogus"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        verbs.main(["hypr", "explode"])
    assert e.value.code == 2
    assert "hypruse: usage:" in capsys.readouterr().err


# --- gates and exit codes --------------------------------------------------------


def test_read_only_mode_refuses_acting_verbs_before_calling_them(stub, monkeypatch, capsys):
    calls = stub("pointer", "click ok")
    monkeypatch.setenv("HYPRUSE_READONLY", "1")
    assert verbs.main(["pointer", "click", "1", "2"]) == verbs.EXIT_REFUSED
    assert calls == []
    err = capsys.readouterr().err
    assert err.startswith("hypruse: refused:") and "HYPRUSE_READONLY" in err
    # observation still works
    stub("desktop", {"monitors": [], "workspaces": [], "windows": [], "active_window": None,
                     "cursor": [1, 2]})
    assert verbs.main(["desktop"]) == 0


def test_clipboard_needs_the_same_opt_in_the_server_needs(stub, monkeypatch, capsys):
    calls = stub("clipboard", "hello")
    assert verbs.main(["clipboard", "read"]) == verbs.EXIT_REFUSED
    assert calls == [] and "HYPRUSE_CLIPBOARD=1" in capsys.readouterr().err
    monkeypatch.setenv("HYPRUSE_CLIPBOARD", "1")
    assert verbs.main(["clipboard", "read"]) == 0
    assert calls == [("clipboard", {"action": "read"})]
    assert capsys.readouterr().out == "hello\n"


def test_a_trust_refusal_is_exit_three_with_the_guards_reason(stub, capsys):
    stub("keyboard", trust.TrustError("0xabc (Signal) is outside the confinement scope"))
    assert verbs.main(["keyboard", "type", "hi", "--window", "0xabc"]) == verbs.EXIT_REFUSED
    assert capsys.readouterr().err == (
        "hypruse: refused: 0xabc (Signal) is outside the confinement scope\n"
    )


def test_errors_are_one_line_on_stderr(stub, capsys):
    stub("hypr", ValueError("unknown action 'x'"))
    assert verbs.main(["hypr", "workspace", "3"]) == verbs.EXIT_ERROR
    assert capsys.readouterr().err == "hypruse: error: unknown action 'x'\n"
    stub("hypr", RuntimeError("socket gone"))
    assert verbs.main(["hypr", "workspace", "3"]) == verbs.EXIT_ERROR
    assert capsys.readouterr().err == "hypruse: error: RuntimeError: socket gone\n"


def test_no_result_is_exit_four():
    assert verbs.exit_for("wait_for", "timeout: no matching window_open event within 5s") == 4
    assert verbs.exit_for("wait_for", "event socket unavailable: gone") == 1
    assert verbs.exit_for("wait_for", {"event": "openwindow"}) == 0
    assert verbs.exit_for("launch", "launched, but no new window appeared within 8s, ...") == 4
    assert verbs.exit_for("launch", {"address": "0x1"}) == 0
    assert verbs.exit_for("ui", "kitty exposes no accessibility tree; use screenshot") == 4
    assert verbs.exit_for("ui", [{"role": "push button"}]) == 0
    assert verbs.exit_for("marks", "no actionable elements in kitty") == 4
    assert verbs.exit_for("click_ui", "kitty exposes no accessibility tree") == 4
    ambiguous = [TextContent(type="text", text="'Ok' is ambiguous (2 candidates); call again"),
                 TextContent(type="text", text="[]")]
    assert verbs.exit_for("click_ui", ambiguous) == 4
    assert verbs.exit_for("click_ui", "clicked push button 'Ok' at (1, 2) in kitty") == 0
    assert verbs.exit_for("sequence", "sequence: stopped after 1/3 steps, desktop changed") == 4
    assert verbs.exit_for("sequence", "sequence: all 3/3 steps ran\n[0] ...") == 0
    # a rehearsal is a result, whatever the tool
    assert verbs.exit_for("launch", "DRY RUN, nothing was delivered: would run kitty") == 0


# --- output ---------------------------------------------------------------------

SNAP = {
    "monitors": [{"name": "eDP-1", "geometry": [0, 0, 1920, 1080], "scale": 1.0,
                  "focused": True, "active_workspace": 7}],
    "workspaces": [{"id": 7, "name": "7", "monitor": "eDP-1", "windows": 2, "visible": True}],
    "windows": [
        {"address": "0x1", "workspace": 7, "class": "kitty", "title": "~ ◐",
         "at": [964, 7], "size": [949, 1066], "floating": False, "pid": 1},
        {"address": "0x2", "workspace": 7, "class": "firefox", "title": "Inbox\x1b[2J",
         "at": [7, 7], "size": [950, 1066], "floating": True, "pid": 2, "fullscreen": True},
    ],
    "active_window": "0x1",
    "cursor": [642, 516],
    "layers": [{"namespace": "wofi", "kind": "launcher", "level": "top", "monitor": "eDP-1",
                "geometry": [660, 300, 600, 400]}],
}


def test_desktop_renders_one_line_per_fact(stub, capsys):
    stub("desktop", SNAP)
    assert verbs.main(["desktop"]) == 0
    out = capsys.readouterr().out
    assert out == (
        "monitor eDP-1 at 0,0 1920x1080 scale 1.0 ws 7 focused\n"
        'ws 7 "7" on eDP-1 windows 2 visible\n'
        'win 0x1 ws 7 kitty "~ ◐" at 964,7 949x1066\n'
        'win 0x2 ws 7 firefox "Inbox.[2J" at 7,7 950x1066 floating fullscreen\n'
        "active 0x1\n"
        "cursor 642,516\n"
        "layer top wofi at 660,300 600x400 kind launcher on eDP-1\n"
    )


def test_json_mode_is_one_compact_line_with_the_raw_result(stub, capsys):
    stub("desktop", SNAP)
    assert verbs.main(["desktop", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "\n " not in out
    assert json.loads(out) == {"ok": True, "tool": "desktop", "result": SNAP}


def test_ui_and_binds_render_as_lines(stub, capsys):
    stub("ui", [
        {"role": "push button", "name": "Save", "x": 1204, "y": 88, "clickable": True},
        {"role": "text", "name": "Name", "x": 900, "y": 140, "clickable": False,
         "value": "report.txt"},
        {"role": "check box", "name": "Remember", "x": 1, "y": 2, "clickable": True,
         "checked": True, "percent": 50},
    ])
    assert verbs.main(["ui"]) == 0
    assert capsys.readouterr().out == (
        '[0] push button "Save" @1204,88\n'
        '[1] text "Name" @900,140 value="report.txt" not-clickable\n'
        '[2] check box "Remember" @1,2 percent=50 checked=true\n'
    )
    stub("binds", [{"combo": "SUPER+Q", "action": "exec", "arg": "kitty",
                    "description": "terminal"},
                   {"combo": "SUPER+F", "action": "lua"}])
    assert verbs.main(["binds"]) == 0
    assert capsys.readouterr().out == 'SUPER+Q  exec kitty  "terminal"\nSUPER+F  lua\n'


def test_launch_and_wait_for_dicts(stub, capsys):
    stub("launch", {"address": "0x9", "class": "firefox", "title": "Mozilla Firefox",
                    "workspace": "3", "note": "window opened elsewhere; moved"})
    assert verbs.main(["launch", "firefox", "--workspace", "3"]) == 0
    assert capsys.readouterr().out == (
        '0x9 firefox "Mozilla Firefox" workspace 3\nnote window opened elsewhere; moved\n'
    )
    stub("wait_for", {"event": "openwindow", "address": "0x9", "class": "firefox",
                      "title": "New Tab", "already": True})
    assert verbs.main(["wait_for", "window_open"]) == 0
    assert capsys.readouterr().out == (
        'already: openwindow address=0x9 class=firefox title="New Tab"\n'
    )
    stub("wait_for", {"event": "openlayer", "namespace": "wofi"})
    assert verbs.main(["wait-for", "layer_open", "--match", "wofi"]) == 0
    assert capsys.readouterr().out == "openlayer namespace=wofi\n"


def test_a_capture_prints_its_path_and_metadata_and_honors_out(monkeypatch, tmp_path, capsys):
    shot = tmp_path / "shot-1.jpg"
    meta = {"geometry": [0, 0, 64, 48], "scale": 1.0, "image": [64, 48], "format": "jpeg",
            "path": str(shot)}
    calls = []

    def fake_screenshot(**kw):  # a fresh capture each call, as the real tool makes one
        calls.append(kw)
        shot.write_bytes(b"jpeg")
        return [
            TextContent(type="text",
                        text=f"screenshot saved, read this file to view the screen: {shot}"),
            TextContent(type="text", text=json.dumps(meta)),
        ]

    monkeypatch.setattr(srv, "screenshot", fake_screenshot)
    assert verbs.main(["screenshot", "--region", "0,0,64x48"]) == 0
    assert calls == [{"region": "0,0,64x48"}]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(shot)
    assert json.loads(out[1]) == meta

    dest = tmp_path / "keep" / "here.jpg"
    dest.parent.mkdir()
    assert verbs.main(["screenshot", "--out", str(dest)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(dest) and dest.read_bytes() == b"jpeg" and not shot.exists()
    assert json.loads(out[1])["path"] == str(dest)

    # --out DIR keeps the file's own name inside the directory
    assert verbs.main(["screenshot", "--out", str(tmp_path / "keep")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(tmp_path / "keep" / "shot-1.jpg")
    assert (tmp_path / "keep" / "shot-1.jpg").read_bytes() == b"jpeg"

    # an impossible destination is one error line, and the capture is not lost
    assert verbs.main(["screenshot", "--out", str(tmp_path / "nope" / "x.jpg")]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == str(shot) and shot.exists()
    assert captured.err.startswith("hypruse: error: cannot move the capture")


def test_then_observations_render_in_their_own_shape(stub, capsys):
    stub("hypr", [TextContent(type="text", text="on workspace 3"),
                  TextContent(type="text", text=json.dumps(SNAP))])
    assert verbs.main(["hypr", "workspace", "3", "--then", "desktop"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("on workspace 3\nmonitor eDP-1 at 0,0")
    legend = {"legend": [{"mark": 1, "role": "push button", "name": "Ok", "x": 5, "y": 6,
                          "clickable": True}], "hint": "click_ui(mark=N)"}
    stub("marks", [TextContent(type="text", text="ImageMagick not found, returning the legend "
                                                  "only (its coordinates are exact)"),
                   TextContent(type="text", text=json.dumps(legend))])
    assert verbs.main(["marks"]) == 0
    assert capsys.readouterr().out == (
        "ImageMagick not found, returning the legend only (its coordinates are exact)\n"
        '[1] push button "Ok" @5,6\n'
    )


def test_captures_are_always_files_for_a_shell_caller(stub, monkeypatch):
    seen = {}

    def fake(**kw):
        seen["mode"] = os.environ.get("HYPRUSE_SCREENSHOT_MODE")
        return "x"

    monkeypatch.setattr(srv, "screenshot", fake)
    monkeypatch.setenv("HYPRUSE_SCREENSHOT_MODE", "image")
    verbs.main(["screenshot"])
    assert seen["mode"] == "file"


def test_dry_run_flag_turns_the_rehearsal_on(stub, monkeypatch):
    seen = {}

    def fake(**kw):
        seen["dry"] = journal.dry_run()
        return "DRY RUN, nothing was delivered: would switch to workspace 3"

    monkeypatch.setattr(srv, "hypr", fake)
    assert verbs.main(["hypr", "workspace", "3", "--dry-run"]) == 0
    assert seen["dry"] is True


# --- state, beacon, journal across processes ------------------------------------


def test_state_is_saved_after_every_call_and_restored_before_the_next(stub, monkeypatch):
    def marks(**kw):
        srv._last_marks = {"window": "0xaaa", "items": {1: {"dx": 1, "dy": 2, "label": "b"}}}
        return "legend only"

    monkeypatch.setattr(srv, "marks", marks)
    verbs.main(["marks"])
    assert json.loads(cli_state.path().read_text())["marks"]["items"]["1"]["label"] == "b"

    seen = {}
    srv._last_marks = {}  # a fresh process

    def click(**kw):
        seen["marks"] = dict(srv._last_marks)
        return "clicked mark 1"

    monkeypatch.setattr(srv, "click_ui", click)
    assert verbs.main(["click_ui", "--mark", "1"]) == 0
    assert seen["marks"]["items"] == {1: {"dx": 1, "dy": 2, "label": "b"}}


def test_strict_seat_baseline_survives_between_calls(stub, monkeypatch):
    monkeypatch.setenv("HYPRUSE_STRICT", "1")

    def act(**kw):
        trust._seat.update(cursor=(5, 6), active="0x1")
        return "ok"

    monkeypatch.setattr(srv, "pointer", act)
    verbs.main(["pointer", "move", "5", "6"])
    trust._seat.update(cursor=None, active=None)
    seen = {}

    def next_act(**kw):
        seen["seat"] = dict(trust._seat)
        return "ok"

    monkeypatch.setattr(srv, "pointer", next_act)
    verbs.main(["pointer", "click"])
    # without this, guard_seat is a no-op in every new process: strict mode
    # would fail open
    assert seen["seat"] == {"cursor": (5, 6), "active": "0x1"}


def test_a_live_servers_beacon_is_left_alone_but_cleanup_is_armed(stub, monkeypatch):
    armed = []
    monkeypatch.setattr(cli, "_take_beacon", lambda: False)
    monkeypatch.setattr(safety, "arm", lambda: armed.append("arm"))
    monkeypatch.setattr(safety, "on_shutdown", lambda fn: armed.append(fn))
    stub("pointer", "ok")
    verbs.main(["pointer", "click"])
    assert armed == ["arm", hinput.release_held]
    armed.clear()
    stub("desktop", SNAP)
    verbs.main(["desktop"])
    assert armed == []  # observation takes nothing and arms nothing


def test_take_beacon_is_used_when_no_server_holds_it(stub, monkeypatch):
    took = []
    monkeypatch.setattr(cli, "_take_beacon", lambda: took.append(True) or True)
    monkeypatch.setattr(safety, "arm", lambda: took.append("arm"))
    stub("hypr", "on workspace 3")
    verbs.main(["hypr", "workspace", "3"])
    assert took == [True]  # init() armed it already; arm() is not called again


def test_journal_entries_from_the_cli_carry_a_source_and_one_header(stub, monkeypatch,
                                                                     tmp_path, capsys):
    log = tmp_path / "journal.ndjson"
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(log))
    monkeypatch.setattr(journal, "_seq", 0)

    @journal.journaled("act")
    def pointer(**kw):
        return "click ok"

    monkeypatch.setattr(srv, "pointer", pointer)
    assert verbs.main(["pointer", "click", "1", "2"]) == 0
    assert verbs.main(["pointer", "click", "3", "4"]) == 0
    entries = list(journal.read(log))
    headers = [e for e in entries if e.get("kind") == "session"]
    acts = [e for e in entries if e.get("kind") == "act"]
    assert len(headers) == 1 and headers[0]["source"] == "cli"
    assert [a["source"] for a in acts] == ["cli", "cli"]
    assert [a["seq"] for a in entries] == [1, 2, 3]  # numbering continues across processes
    # still the agent's own actions: replay re-issues them
    assert all(journal.replayable(a) for a in acts)
    assert "by" not in acts[0]
    # and the record reads back with the surface marked
    assert cli.journal_cmd([str(log)]) == 0
    out = capsys.readouterr().out
    assert "session start (cli)" in out
    act_lines = [line for line in out.splitlines() if " act " in line]
    assert len(act_lines) == 2 and all(line.endswith("(cli)") for line in act_lines)


def test_seq_numbers_are_unique_across_writers(stub, monkeypatch, tmp_path):
    # a verb beside a live server, or two verbs at once: the counter file
    # beside the journal hands out numbers, not each process's memory
    log = tmp_path / "journal.ndjson"
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(log))
    monkeypatch.setattr(journal, "_seq", 0)

    @journal.journaled("observe")
    def desktop(**kw):
        return SNAP

    monkeypatch.setattr(srv, "desktop", desktop)
    verbs.main(["desktop"])  # header 1, record 2
    monkeypatch.setattr(journal, "_seq", 0)  # a server process that never read the tail
    journal.record("screenshot", "observe", {}, "ok", 1.0)  # 3, not 1
    monkeypatch.setattr(journal, "_seq", 0)
    verbs.main(["desktop"])  # same header, record 4
    seqs = [e["seq"] for e in journal.read(log)]
    assert seqs == [1, 2, 3, 4]
    assert (tmp_path / "journal.ndjson.seq").read_text() == "4"


def test_a_dry_run_between_real_calls_does_not_open_a_new_header(stub, monkeypatch, tmp_path):
    log = tmp_path / "journal.ndjson"
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(log))
    stub("hypr", "DRY RUN, nothing was delivered: would switch to workspace 3")
    stub("desktop", SNAP)
    verbs.main(["desktop"])
    verbs.main(["hypr", "workspace", "3", "--dry-run"])
    monkeypatch.delenv("HYPRUSE_DRYRUN", raising=False)  # the next process is not dry
    verbs.main(["desktop"])
    headers = [e for e in journal.read(log) if e.get("kind") == "session"]
    assert len(headers) == 1


def test_a_changed_mode_opens_a_new_header(stub, monkeypatch, tmp_path):
    log = tmp_path / "journal.ndjson"
    monkeypatch.setenv("HYPRUSE_JOURNAL", str(log))
    stub("desktop", SNAP)
    verbs.main(["desktop"])
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    verbs.main(["desktop"])
    headers = [e for e in journal.read(log) if e.get("kind") == "session"]
    assert len(headers) == 2 and headers[1]["mode"]["strict"] is True


def test_acting_verbs_take_the_cross_process_lock(stub, monkeypatch, tmp_path):
    stub("pointer", "ok")
    verbs.main(["pointer", "click"])
    assert (tmp_path / "hypruse" / "cli.lock").exists()


def test_stale_or_foreign_marks_are_refused_not_clicked(monkeypatch):
    import time

    monkeypatch.setattr(srv, "_resolve_window", lambda w: {"address": w, "class": "kitty",
                                                            "at": [0, 0], "size": [10, 10]})
    item = {"dx": 1, "dy": 2, "label": "push button 'Ok'"}
    srv._last_marks = {"window": "0x1", "class": "kitty", "ts": time.time() - 700,
                       "items": {1: item}}
    with pytest.raises(ValueError, match="minutes old"):
        srv.click_ui(mark=1)
    srv._last_marks = {"window": "0x1", "class": "firefox", "ts": time.time(), "items": {1: item}}
    with pytest.raises(ValueError, match="now belongs to 'kitty'"):
        srv.click_ui(mark=1)
    # a persisted record without a timestamp is expired, not fresh
    srv.restore_marks({"window": "0x1", "class": "kitty", "items": {"1": item}})
    with pytest.raises(ValueError, match="minutes old"):
        srv.click_ui(mark=1)


def test_an_observe_verb_does_not_clobber_what_another_verb_saved(stub, monkeypatch):
    # restore, then (while we "run") another process records a launch;
    # our save must merge, not overwrite
    stub("desktop", SNAP)
    verbs.main(["desktop"])  # creates the state file
    on_disk = json.loads(cli_state.path().read_text())
    on_disk["trust"]["owned"] = ["0xaaa"]
    on_disk["marks"] = {"window": "0x1", "class": "kitty", "ts": 1.0,
                        "items": {"1": {"dx": 1, "dy": 2, "label": "l"}}}

    def slow_desktop(**kw):
        cli_state.path().write_text(json.dumps(on_disk))  # the concurrent save lands here
        return SNAP

    monkeypatch.setattr(srv, "desktop", slow_desktop)
    verbs.main(["desktop"])
    after = json.loads(cli_state.path().read_text())
    assert after["trust"]["owned"] == ["0xaaa"]
    assert after["marks"]["items"]["1"]["label"] == "l"


def test_control_characters_never_reach_the_terminal(stub, capsys):
    stub("keyboard", "typed into \x1b[2J\x07window")
    verbs.main(["keyboard", "key", "enter"])
    assert capsys.readouterr().out == "typed into .[2J.window\n"
    stub("keyboard", ValueError("bad \x1b[31mcombo"))
    verbs.main(["keyboard", "key", "x"])
    assert capsys.readouterr().err == "hypruse: error: bad .[31mcombo\n"
