"""The zoom primitive: box math, bounds resolution, and the MCP tool.

The coarse-to-fine loop lives or dies on this arithmetic: a zoom box must
always be a valid on-screen grim region, whatever point the agent
estimates, or the precision step fails exactly when it is needed.
"""

import json

import pytest

from hypruse import screenshot
from hypruse import server as srv


def test_parse_size():
    assert screenshot.parse_size("480x360") == (480, 360)
    assert screenshot.parse_size(" 64 x 64 ") == (64, 64)


@pytest.mark.parametrize("bad", ["", "480", "480x", "x360", "0x50", "axb", "480,360"])
def test_parse_size_rejects(bad):
    with pytest.raises(screenshot.ScreenshotError):
        screenshot.parse_size(bad)


BOUNDS = (0, 0, 1920, 1080)


def test_clamp_box_centers_on_point():
    assert screenshot.clamp_box(960, 540, 480, 360, BOUNDS) == (720, 360, 480, 360)


def test_clamp_box_slides_inside_at_edges():
    assert screenshot.clamp_box(10, 10, 480, 360, BOUNDS) == (0, 0, 480, 360)
    assert screenshot.clamp_box(1915, 1075, 480, 360, BOUNDS) == (1440, 720, 480, 360)


def test_clamp_box_shrinks_to_bounds():
    assert screenshot.clamp_box(960, 540, 4000, 300, BOUNDS) == (0, 390, 1920, 300)


def test_clamp_box_bad_estimate_off_screen_still_valid():
    assert screenshot.clamp_box(-500, -500, 480, 360, BOUNDS) == (0, 0, 480, 360)


def test_clamp_box_offset_monitor():
    assert screenshot.clamp_box(2000, 100, 400, 300, (1920, 0, 2048, 1152)) == (1920, 0, 400, 300)


# width/height are physical mode pixels, exactly as hyprctl reports them;
# DP-3's logical footprint is 2560x1440 / 1.25 = 2048x1152 at x=1920
MONITORS = [
    {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0, "focused": True},
    {"name": "DP-3", "x": 1920, "y": 0, "width": 2560, "height": 1440, "scale": 1.25},
]


def _fake_query(answers):
    return lambda cmd: answers[cmd]


def test_zoom_region_picks_containing_monitor(monkeypatch):
    monkeypatch.setattr(screenshot.hyprctl, "query", _fake_query({"monitors": MONITORS}))
    assert screenshot.zoom_region(3900, 1100) == (3488, 792, 480, 360)


def test_zoom_region_off_layout_falls_back_to_focused(monkeypatch):
    monkeypatch.setattr(screenshot.hyprctl, "query", _fake_query({"monitors": MONITORS}))
    assert screenshot.zoom_region(99999, 99999) == (1440, 720, 480, 360)


# HiDPI 2880x1800 @ 1.5 is logically 1920x1200; FHD sits at its logical
# right edge. Regression cluster: bounds must be logical, not mode pixels.
SEAM = [
    {"name": "eDP-1", "x": 0, "y": 0, "width": 2880, "height": 1800, "scale": 1.5,
     "focused": True},
    {"name": "DP-1", "x": 1920, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
]


def test_zoom_region_clamps_to_logical_edge_on_fractional_scale(monkeypatch):
    monkeypatch.setattr(screenshot.hyprctl, "query", _fake_query({"monitors": SEAM}))
    # near the logical bottom-right corner (1920x1200), not the 2880x1800 mode
    assert screenshot.zoom_region(1900, 1180) == (1440, 840, 480, 360)


def test_zoom_region_across_fractional_seam(monkeypatch):
    monkeypatch.setattr(screenshot.hyprctl, "query", _fake_query({"monitors": SEAM}))
    # (2500, 500) is on DP-1; the HiDPI monitor's mode width must not claim it
    assert screenshot.zoom_region(2500, 500) == (2260, 320, 480, 360)


def test_zoom_region_rotated_monitor(monkeypatch):
    portrait = [
        {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0,
         "transform": 1, "focused": True},
    ]
    monkeypatch.setattr(screenshot.hyprctl, "query", _fake_query({"monitors": portrait}))
    # transform 1 swaps the logical footprint to 1080x1920
    assert screenshot.zoom_region(1000, 1500) == (600, 1320, 480, 360)


def test_zoom_region_window_bounds(monkeypatch):
    clients = [{"address": "0xa", "at": [100, 50], "size": [800, 600]}]
    monkeypatch.setattr(
        screenshot.hyprctl,
        "query",
        _fake_query({"clients": clients, "activewindow": {"address": "0xa"}}),
    )
    assert screenshot.zoom_region(120, 60, window="0xa") == (100, 50, 480, 360)
    assert screenshot.zoom_region(120, 60, size="2000x2000", window="0xa") == (100, 50, 800, 600)


def test_zoom_region_rejects_bad_size(monkeypatch):
    monkeypatch.setattr(screenshot.hyprctl, "query", _fake_query({"monitors": MONITORS}))
    with pytest.raises(screenshot.ScreenshotError, match="bad size"):
        screenshot.zoom_region(10, 10, size="huge")


def test_zoom_tool_builds_region_and_echoes_point(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPRUSE_SCREENSHOT_MODE", raising=False)
    monkeypatch.setattr(
        srv.shot, "zoom_region", lambda x, y, size="", window="": (720, 360, 480, 360)
    )
    captured = {}

    def fake_capture(window="", region="", scale=0.0, max_bytes=None, max_edge=None):
        captured["region"] = region
        meta = {
            "target": "region",
            "geometry": [720, 360, 480, 360],
            "image": [480, 360],
            "format": "png",
            "scale": 1.0,
        }
        return b"\x89PNG\r\n\x1a\nfake", meta

    monkeypatch.setattr(srv.shot, "capture", fake_capture)
    out = srv.zoom(960, 540)
    assert captured["region"] == "720,360,480x360"
    meta = json.loads(out[-1].text)
    assert meta["target"] == "zoom"
    assert meta["point"] == [960, 540]
    assert meta["geometry"] == [720, 360, 480, 360]
    saved = list(tmp_path.glob("hypruse/shot-*.png"))
    assert len(saved) == 1
