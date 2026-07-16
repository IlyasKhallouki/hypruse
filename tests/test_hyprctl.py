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
    assert mons["DP-3"]["scale"] == 1.25
    assert mons["eDP-1"]["active_workspace"] == 2


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
