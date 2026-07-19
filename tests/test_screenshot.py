import pytest

from hypruse import screenshot


def test_parse_region_both_separators():
    assert screenshot.parse_region("10,20,300x400") == (10, 20, 300, 400)
    assert screenshot.parse_region("10,20 300x400") == (10, 20, 300, 400)
    assert screenshot.parse_region("-5, -7, 8x9") == (-5, -7, 8, 9)


@pytest.mark.parametrize("bad", ["", "10,20", "a,b,cxd", "10,20,0x50", "10;20;3x4"])
def test_parse_region_rejects(bad):
    with pytest.raises(screenshot.ScreenshotError):
        screenshot.parse_region(bad)


MONITORS = [
    {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
    {"name": "DP-3", "x": 1920, "y": 0, "width": 2048, "height": 1152, "scale": 1.25},
]


def test_scale_lookup_per_monitor():
    assert screenshot._scale_for_rect(500, 500, 10, 10, MONITORS) == 1.0
    assert screenshot._scale_for_rect(2000, 100, 10, 10, MONITORS) == 1.25
    assert screenshot._scale_for_rect(99999, 0, 10, 10, MONITORS) == 1.0  # off-layout → neutral


def test_scale_lookup_uses_logical_bounds():
    # HiDPI 2880x1800 @ 1.5 ends logically at x=1920, where the FHD begins:
    # a rect on the FHD must not be claimed via the HiDPI's mode width
    seam = [
        {"name": "eDP-1", "x": 0, "y": 0, "width": 2880, "height": 1800, "scale": 1.5},
        {"name": "DP-1", "x": 1920, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
    ]
    assert screenshot._scale_for_rect(2500, 500, 10, 10, seam) == 1.0
    assert screenshot._scale_for_rect(1900, 500, 10, 10, seam) == 1.5


def test_scale_for_rect_cross_seam_takes_max():
    # grim renders a -g rect at the GREATEST scale among intersected
    # outputs, so a rect straddling a 1.0/2.0 seam maps at 2.0 even though
    # its top-left corner sits on the 1.0 monitor
    seam = [
        {"name": "a", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
        {"name": "b", "x": 1920, "y": 0, "width": 3840, "height": 2160, "scale": 2.0},
    ]
    assert screenshot._scale_for_rect(1800, 100, 300, 100, seam) == 2.0
    assert screenshot._scale_for_rect(100, 100, 300, 100, seam) == 1.0  # fully on 1.0


def test_find_window_active_and_missing():
    clients = [{"address": "0xa", "at": [0, 0], "size": [10, 10]}]
    assert screenshot._find_window("active", clients, "0xa")["address"] == "0xa"
    assert screenshot._find_window("0xa", clients, None)["address"] == "0xa"
    with pytest.raises(screenshot.ScreenshotError, match="not found"):
        screenshot._find_window("0xdead", clients, "0xa")
    with pytest.raises(screenshot.ScreenshotError, match="no active window"):
        screenshot._find_window("active", clients, None)
