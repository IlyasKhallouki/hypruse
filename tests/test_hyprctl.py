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
        "namespace": "wofi", "kind": "launcher", "level": "overlay",
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
