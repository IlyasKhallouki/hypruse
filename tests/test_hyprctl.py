import json
from pathlib import Path

import pytest

from hypruse import hyprctl

FIX = json.loads((Path(__file__).parent / "fixtures" / "desktop.json").read_text())


def snap():
    return hyprctl.snapshot_from(
        FIX["monitors"],
        FIX["workspaces"],
        FIX["clients"],
        FIX["activewindow"],
        (FIX["cursorpos"]["x"], FIX["cursorpos"]["y"]),
    )


def test_snapshot_shape():
    s = snap()
    assert set(s) == {"monitors", "workspaces", "windows", "active_window", "cursor"}
    assert s["active_window"] == "0xaaaa000000000001"
    assert s["cursor"] == [640, 400]


def test_monitors_carry_geometry_and_scale():
    mons = {m["name"]: m for m in snap()["monitors"]}
    assert mons["eDP-1"]["geometry"] == [0, 0, 1920, 1080]
    # DP-3 is 2560x1440 physical at scale 1.25: geometry is the LOGICAL
    # footprint (2048x1152), the same space window `at`/`size` live in
    assert mons["DP-3"]["geometry"] == [1920, 0, 2048, 1152]
    assert mons["DP-3"]["scale"] == 1.25
    assert mons["eDP-1"]["active_workspace"] == 2


def test_logical_rect_scale_and_transform():
    base = {"x": 0, "y": 0, "width": 2880, "height": 1800}
    assert hyprctl.logical_rect({**base, "scale": 1.0}) == (0, 0, 2880, 1800)
    assert hyprctl.logical_rect({**base, "scale": 1.5}) == (0, 0, 1920, 1200)
    # 90-degree transform swaps the logical footprint
    assert hyprctl.logical_rect({**base, "scale": 1.0, "transform": 1}) == (0, 0, 1800, 2880)
    assert hyprctl.logical_rect({**base, "scale": 1.5, "transform": 3}) == (0, 0, 1200, 1920)
    # even transforms (180) do not swap
    assert hyprctl.logical_rect({**base, "scale": 1.0, "transform": 2}) == (0, 0, 2880, 1800)


def test_monitor_at_uses_logical_seam():
    # HiDPI ends logically at x=1920 where the FHD begins; physical width
    # (2880) must not let the HiDPI claim points on the FHD
    monitors = [
        {"name": "eDP-1", "x": 0, "y": 0, "width": 2880, "height": 1800, "scale": 1.5},
        {"name": "DP-1", "x": 1920, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
    ]
    assert hyprctl.monitor_at(monitors, 1900, 500)["name"] == "eDP-1"
    assert hyprctl.monitor_at(monitors, 2500, 500)["name"] == "DP-1"
    assert hyprctl.monitor_at(monitors, 99999, 0) is None


def test_transform_field_surfaced_only_when_rotated():
    mons = {m["name"]: m for m in snap()["monitors"]}
    assert "transform" not in mons["eDP-1"]
    rotated = hyprctl._monitor(
        {"name": "R", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0, "transform": 1}
    )
    assert rotated["transform"] == 1
    assert rotated["geometry"] == [0, 0, 1080, 1920]


def test_workspaces_sorted_and_visibility():
    ws = snap()["workspaces"]
    assert [w["id"] for w in ws] == [-98, 2, 5]
    vis = {w["id"]: w["visible"] for w in ws}
    assert vis == {-98: False, 2: True, 5: True}


def test_unmapped_windows_excluded():
    addrs = [w["address"] for w in snap()["windows"]]
    assert "0xaaaa000000000004" not in addrs
    assert len(addrs) == 3


def test_fullscreen_normalized_from_int_enum_and_bool():
    wins = {w["address"]: w for w in snap()["windows"]}
    assert "fullscreen" not in wins["0xaaaa000000000001"]  # 0 → omitted
    assert wins["0xaaaa000000000002"]["fullscreen"] is True  # 2 → True
    assert wins["0xaaaa000000000003"].get("hidden") is True


def test_dispatch_raises_on_error_reply(monkeypatch):
    monkeypatch.setattr(hyprctl, "_run", lambda *a: "Invalid dispatcher")
    with pytest.raises(hyprctl.HyprctlError, match="Invalid dispatcher"):
        hyprctl.dispatch("focuswindow", "address:0xdead")


def test_dispatch_ok(monkeypatch):
    monkeypatch.setattr(hyprctl, "_run", lambda *a: "ok")
    hyprctl.dispatch("workspace", "3")


def test_query_raises_on_garbage(monkeypatch):
    monkeypatch.setattr(hyprctl, "_run", lambda *a: "not json at all")
    with pytest.raises(hyprctl.HyprctlError, match="unparseable"):
        hyprctl.query("clients")


def test_batch_query_splits_concatenated_json(monkeypatch):
    # hyprctl concatenates the documents (arrays and objects, various spacing)
    seen = {}
    fake = '[{"id": 0}]\n[{"id": 2}]  [{"address": "0xa"}]{"address": "0xa"}\n{"x": 5, "y": 9}'

    def run(*args):
        seen["args"] = args
        return fake

    monkeypatch.setattr(hyprctl, "_run", run)
    vals = hyprctl.batch_query(["monitors", "workspaces", "clients", "activewindow", "cursorpos"])
    assert seen["args"] == (
        "--batch",
        "j/monitors ; j/workspaces ; j/clients ; j/activewindow ; j/cursorpos",
    )
    assert [type(v).__name__ for v in vals] == ["list", "list", "list", "dict", "dict"]
    assert vals[4] == {"x": 5, "y": 9}


def test_batch_query_wrong_count_raises(monkeypatch):
    monkeypatch.setattr(hyprctl, "_run", lambda *a: "[]")  # one doc for two commands
    with pytest.raises(hyprctl.HyprctlError, match="returned 1 results for 2"):
        hyprctl.batch_query(["monitors", "workspaces"])


def test_batch_query_garbage_raises(monkeypatch):
    monkeypatch.setattr(hyprctl, "_run", lambda *a: "not json")
    with pytest.raises(hyprctl.HyprctlError, match="unparseable"):
        hyprctl.batch_query(["monitors"])


def test_snapshot_uses_one_batched_call(monkeypatch):
    calls = {"batch": 0, "query": 0}

    def fake_batch(cmds):
        calls["batch"] += 1
        return [
            FIX["monitors"],
            FIX["workspaces"],
            FIX["clients"],
            FIX["activewindow"],
            {"x": FIX["cursorpos"]["x"], "y": FIX["cursorpos"]["y"]},
            {},
        ]

    monkeypatch.setattr(hyprctl, "batch_query", fake_batch)
    monkeypatch.setattr(hyprctl, "query", lambda c: calls.__setitem__("query", calls["query"] + 1))
    s = hyprctl.snapshot()
    assert calls == {"batch": 1, "query": 0}  # one batched call, no per-command queries
    assert s["cursor"] == [640, 400]
    assert s["active_window"] == "0xaaaa000000000001"


def test_snapshot_handles_no_active_window(monkeypatch):
    monkeypatch.setattr(
        hyprctl,
        "batch_query",
        lambda cmds: [FIX["monitors"], FIX["workspaces"], [], {}, {"x": 0, "y": 0}, {}],
    )
    s = hyprctl.snapshot()
    assert s["active_window"] is None  # empty activewindow object -> None


LAYERS_RAW = {
    "eDP-1": {
        "levels": {
            "0": [{"address": "0x1", "x": 0, "y": 0, "w": 1920, "h": 1080,
                   "namespace": "awww-daemon", "pid": 100}],
            "1": [{"address": "0x2", "x": 0, "y": 0, "w": 1920, "h": 38,
                   "namespace": "waybar", "pid": 101}],
            "2": [],
            "3": [{"address": "0x3", "x": 660, "y": 300, "w": 600, "h": 400,
                   "namespace": "wofi", "pid": 102},
                  {"address": "0x4", "x": 1540, "y": 48, "w": 370, "h": 110,
                   "namespace": "notifications", "pid": 103}],
        }
    }
}


def test_parse_layers_flattens_and_classifies():
    out = hyprctl.parse_layers(LAYERS_RAW)
    by_ns = {s["namespace"]: s for s in out}
    assert "awww-daemon" not in by_ns  # background level (wallpaper) dropped
    assert by_ns["waybar"]["kind"] == "bar"
    assert by_ns["waybar"]["level"] == "bottom"
    assert by_ns["wofi"] == {
        "namespace": "wofi", "kind": "launcher", "mapped": True, "level": "overlay",
        "monitor": "eDP-1", "geometry": [660, 300, 600, 400],
    }
    assert by_ns["notifications"]["kind"] == "notifications"


def test_parse_layers_unknown_namespace_degrades():
    raw = {"DP-1": {"levels": {"2": [
        {"x": 0, "y": 0, "w": 10, "h": 10, "namespace": "some-custom-widget"}
    ]}}}
    out = hyprctl.parse_layers(raw)
    assert out[0]["kind"] == "unknown"  # heuristic degrades, never mislabels
    assert hyprctl.parse_layers({}) == []
    assert hyprctl.parse_layers({"eDP-1": {"levels": {}}}) == []


def test_snapshot_surfaces_layers_only_when_present():
    s = hyprctl.snapshot_from(FIX["monitors"], FIX["workspaces"], [], None, None, LAYERS_RAW)
    assert {x["namespace"] for x in s["layers"]} == {"waybar", "wofi", "notifications"}
    bare = hyprctl.snapshot_from(FIX["monitors"], FIX["workspaces"], [], None, None, {})
    assert "layers" not in bare  # token-lean when only wallpaper exists


# --- config manager: hyprlang vs the Lua manager Hyprland 0.56 added --------
#
# Regression source: issue #1, hypruse 0.9.4 on Hyprland 0.56.2 with a
# hyprland.lua, where every `hyprctl dispatch <name> <args>` came back as a
# Lua syntax error. These pin the WIRE FORM, not the Python call, because
# the two ways to get this wrong (a move that is not silent, a string that
# is not a literal) both answer "ok" while doing the wrong thing.

STATUS_LUA = '\n{\n    "configProvider": "lua",\n    "backend": "drm"\n}\n'
STATUS_HYPRLANG = '\n{\n    "configProvider": "hyprlang",\n    "backend": "drm"\n}\n'


def test_parse_provider_reads_the_three_cases():
    assert hyprctl.parse_provider(STATUS_LUA) == hyprctl.LUA
    assert hyprctl.parse_provider(STATUS_HYPRLANG) == hyprctl.HYPRLANG
    # `status` does not exist before 0.56; that reply is not JSON and the
    # compositor it came from speaks the legacy strings
    assert hyprctl.parse_provider("unknown request") == hyprctl.HYPRLANG
    assert hyprctl.parse_provider('{"backend": "drm"}') == hyprctl.HYPRLANG
    assert hyprctl.parse_provider("[]") == hyprctl.HYPRLANG


def test_provider_probes_once_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(hyprctl, "_provider", None)
    monkeypatch.setattr(hyprctl, "_run", lambda *a: calls.append(a) or STATUS_LUA)
    assert hyprctl.provider() == hyprctl.LUA
    assert hyprctl.provider() == hyprctl.LUA
    assert calls == [("-j", "status")]  # one probe, not one per call


def test_provider_does_not_cache_an_unreachable_compositor(monkeypatch):
    def boom(*_a):
        raise hyprctl.HyprctlError("hyprctl not found")

    monkeypatch.setattr(hyprctl, "_provider", None)
    monkeypatch.setattr(hyprctl, "_run", boom)
    assert hyprctl.provider() == hyprctl.HYPRLANG  # legacy is right pre-0.56
    assert hyprctl._provider is None  # but it was a guess, so ask again next time


def _argv(monkeypatch, provider):
    """Record the argv dispatch() hands to hyprctl."""
    seen = []
    monkeypatch.setattr(hyprctl, "_provider", provider)
    monkeypatch.setattr(hyprctl, "_run", lambda *a: seen.append(a) or "ok")
    return seen


def test_dispatch_on_hyprlang_sends_the_legacy_strings(monkeypatch):
    seen = _argv(monkeypatch, hyprctl.HYPRLANG)
    hyprctl.dispatch("movecursor", "100", "200")
    hyprctl.dispatch("togglefloating")
    assert seen == [("dispatch", "movecursor", "100", "200"), ("dispatch", "togglefloating")]


# every dispatcher hypruse emits, and the exact Lua it must become
LUA_FORMS = [
    (("exec", "[workspace 2 silent] foot"),
     'hl.dsp.exec_cmd("[workspace 2 silent] foot")'),
    (("movecursor", "694", "438"),
     "hl.dsp.cursor.move({ x = 694, y = 438 })"),
    (("focuswindow", "address:0xabc"),
     'hl.dsp.focus({ window = "address:0xabc" })'),
    (("workspace", "special:magic"),
     'hl.dsp.focus({ workspace = "special:magic" })'),
    (("closewindow", "address:0xabc"),
     'hl.dsp.window.close({ window = "address:0xabc" })'),
    (("movetoworkspacesilent", "3,address:0xabc"),
     'hl.dsp.window.move({ workspace = "3", window = "address:0xabc", follow = false })'),
    (("fullscreen", "0"),
     'hl.dsp.window.fullscreen({ mode = "fullscreen", action = "toggle" })'),
    (("togglefloating", "address:0xabc"),
     'hl.dsp.window.float({ action = "toggle", window = "address:0xabc" })'),
    (("togglefloating",),
     'hl.dsp.window.float({ action = "toggle" })'),
    (("tagwindow", "+hypruse-owned", "address:0xabc"),
     'hl.dsp.window.tag({ tag = "+hypruse-owned", window = "address:0xabc" })'),
]


@pytest.mark.parametrize("call,expected", LUA_FORMS)
def test_dispatch_on_lua_sends_one_escaped_expression(monkeypatch, call, expected):
    seen = _argv(monkeypatch, hyprctl.LUA)
    hyprctl.dispatch(*call)
    # ONE argv element: hyprctl joins argv with spaces, and the expression
    # carries its own
    assert seen == [("dispatch", expected)]


def test_the_silent_move_says_follow_false_and_means_it(monkeypatch):
    # Hyprland reads `silent = follow.has_value() && !*follow`, so omitting
    # the field, or sending anything that is not Lua's `false`, gives the
    # LOUD move: it drags the human's view to the target workspace and
    # still answers "ok". Nothing but this assertion would catch that.
    seen = _argv(monkeypatch, hyprctl.LUA)
    hyprctl.dispatch("movetoworkspacesilent", "3,address:0xabc")
    assert "follow = false" in seen[0][1]
    for wrong in ("follow = False", "follow = 0", 'follow = "false"'):
        assert wrong not in seen[0][1]


def test_move_to_workspace_without_a_window_targets_the_focused_one(monkeypatch):
    seen = _argv(monkeypatch, hyprctl.LUA)
    hyprctl.dispatch("movetoworkspacesilent", "3")
    assert seen == [('dispatch', 'hl.dsp.window.move({ workspace = "3", follow = false })')]


def test_lua_dispatch_refuses_a_dispatcher_it_cannot_translate():
    with pytest.raises(hyprctl.HyprctlError, match="no Lua form"):
        hyprctl.lua_dispatch("killactive")


def test_lua_dispatch_reports_a_bad_argument_count():
    with pytest.raises(hyprctl.HyprctlError, match="movecursor"):
        hyprctl.lua_dispatch("movecursor", ("100",))


def test_lua_str_is_a_byte_exact_literal():
    assert hyprctl.lua_str("foot") == '"foot"'
    assert hyprctl.lua_str('say "hi"') == '"say \\034hi\\034"'
    assert hyprctl.lua_str("a\\b") == '"a\\092b"'
    # three digits, always: Lua reads up to three, so "\9" followed by "9"
    # would silently be the single byte 99
    assert hyprctl.lua_str("\t9") == '"\\0099"'
    assert hyprctl.lua_str("a\nb") == '"a\\010b"'
    # non-ASCII goes out as its UTF-8 bytes, so the payload stays plain
    # ASCII and cannot disturb hyprctl's argv join or the socket framing
    assert hyprctl.lua_str("é") == '"\\195\\169"'
    assert all(0x20 <= ord(c) < 0x7F for c in hyprctl.lua_str("é\n\t\"\\"))


def test_lua_str_closes_the_bracket_escape():
    # a long-bracket literal would end early here and run the rest as code
    assert hyprctl.lua_str('x]==]) os.execute("rm -rf ~")') == (
        '"x]==]) os.execute(\\034rm -rf ~\\034)"'
    )


def test_lua_str_refuses_a_nul_byte():
    with pytest.raises(hyprctl.HyprctlError, match="NUL"):
        hyprctl.lua_str("a\0b")


def test_an_agent_string_cannot_become_lua_code(monkeypatch):
    # `hyprctl dispatch` on a Lua config evaluates its argument inside the
    # compositor's interpreter, so the command a launch carries has to
    # arrive as a literal, not as an expression
    seen = _argv(monkeypatch, hyprctl.LUA)
    hyprctl.dispatch("exec", 'foo") or os.execute("touch /tmp/pwn')
    assert seen == [
        ("dispatch", 'hl.dsp.exec_cmd("foo\\034) or os.execute(\\034touch /tmp/pwn")')
    ]


def test_dispatch_reprobes_and_retries_when_the_manager_changed(monkeypatch):
    # `hyprctl reload full-reset` re-picks the config manager from the
    # config file's extension, so a long-lived server can find itself
    # talking the wrong language mid-session
    seen = []
    monkeypatch.setattr(hyprctl, "_provider", hyprctl.HYPRLANG)

    def run(*args):
        seen.append(args)
        if args == ("-j", "status"):
            return STATUS_LUA
        if args[1] == "workspace":  # the legacy string, on a Lua compositor
            raise hyprctl.HyprctlError("error: ')' expected near '3'")
        return "ok"

    monkeypatch.setattr(hyprctl, "_run", run)
    hyprctl.dispatch("workspace", "3")
    assert seen == [
        ("dispatch", "workspace", "3"),
        ("-j", "status"),
        ("dispatch", 'hl.dsp.focus({ workspace = "3" })'),
    ]
    assert hyprctl.provider() == hyprctl.LUA  # and it stays corrected


def test_dispatch_does_not_retry_when_the_manager_is_the_same(monkeypatch):
    seen = []
    monkeypatch.setattr(hyprctl, "_provider", hyprctl.HYPRLANG)

    def run(*args):
        seen.append(args)
        if args == ("-j", "status"):
            return STATUS_HYPRLANG
        raise hyprctl.HyprctlError("Invalid dispatcher")

    monkeypatch.setattr(hyprctl, "_run", run)
    with pytest.raises(hyprctl.HyprctlError, match="Invalid dispatcher"):
        hyprctl.dispatch("focuswindow", "address:0xabc")
    # one attempt, one probe, and no second attempt to double-apply
    assert seen == [("dispatch", "focuswindow", "address:0xabc"), ("-j", "status")]


def test_border_rule_uses_the_managers_own_config_call(monkeypatch):
    seen = []
    monkeypatch.setattr(hyprctl, "_run", lambda *a: seen.append(a) or "ok")

    monkeypatch.setattr(hyprctl, "_provider", hyprctl.HYPRLANG)
    hyprctl.border_rule("hypruse-owned", "rgb(ff5555)")
    assert seen == [("keyword", "windowrule", "border_color rgb(ff5555), tag hypruse-owned")]

    seen.clear()
    monkeypatch.setattr(hyprctl, "_provider", hyprctl.LUA)
    hyprctl.border_rule("hypruse-owned", "rgb(ff5555)")
    assert seen == [
        (
            "eval",
            'hl.window_rule({ name = "hypruse-owned", match = { tag = "hypruse-owned" }, '
            'border_color = "rgb(ff5555)" })',
        )
    ]


def test_border_rule_falls_back_to_the_pre_0_42_matcher(monkeypatch):
    tried = []

    def run(*args):
        tried.append(args[-1])
        if "tag hypruse-owned" in args[-1]:  # modern form, older Hyprland
            raise hyprctl.HyprctlError("invalid")
        return "ok"

    monkeypatch.setattr(hyprctl, "_provider", hyprctl.HYPRLANG)
    monkeypatch.setattr(hyprctl, "_run", run)
    hyprctl.border_rule("hypruse-owned", "rgb(ff5555)")
    assert tried == [
        "border_color rgb(ff5555), tag hypruse-owned",
        "border_color rgb(ff5555), tag:hypruse-owned",
    ]


def test_border_rule_raises_when_no_spelling_lands(monkeypatch):
    monkeypatch.setattr(hyprctl, "_provider", hyprctl.HYPRLANG)
    monkeypatch.setattr(hyprctl, "_run", lambda *a: "Invalid rule")
    with pytest.raises(hyprctl.HyprctlError, match="no windowrule spelling"):
        hyprctl.border_rule("hypruse-owned", "rgb(ff5555)")


def test_dispatch_does_not_retry_on_a_probe_it_could_not_run(monkeypatch):
    # provider() answers hyprlang when it cannot reach the compositor, and a
    # guess is not evidence the manager moved. Retrying on it would repeat a
    # dispatch that may well have landed before the socket went quiet.
    seen = []
    monkeypatch.setattr(hyprctl, "_provider", hyprctl.LUA)

    def run(*args):
        seen.append(args)
        raise hyprctl.HyprctlError("hyprctl dispatch: timed out")

    monkeypatch.setattr(hyprctl, "_run", run)
    with pytest.raises(hyprctl.HyprctlError, match="timed out"):  # the REAL reason
        hyprctl.dispatch("closewindow", "address:0xabc")
    assert seen == [
        ("dispatch", 'hl.dsp.window.close({ window = "address:0xabc" })'),
        ("-j", "status"),
    ]  # one attempt, one failed probe, no second dispatch
