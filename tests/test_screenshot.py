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
    assert screenshot._scale_at(500, 500, MONITORS) == 1.0
    assert screenshot._scale_at(2000, 100, MONITORS) == 1.25
    assert screenshot._scale_at(99999, 0, MONITORS) == 1.0  # off-layout → neutral


def test_find_window_active_and_missing():
    clients = [{"address": "0xa", "at": [0, 0], "size": [10, 10]}]
    assert screenshot._find_window("active", clients, "0xa")["address"] == "0xa"
    assert screenshot._find_window("0xa", clients, None)["address"] == "0xa"
    with pytest.raises(screenshot.ScreenshotError, match="not found"):
        screenshot._find_window("0xdead", clients, "0xa")
    with pytest.raises(screenshot.ScreenshotError, match="no active window"):
        screenshot._find_window("active", clients, None)
