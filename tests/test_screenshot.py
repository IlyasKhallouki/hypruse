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


# A window on a workspace no monitor shows (#4): its `at` is a rect on the
# monitor, but the monitor is showing something else there, so cropping
# that rect returns another window's pixels under this window's name.
ONE_MONITOR = [{"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080,
                "scale": 1.0, "activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0}}]
HIDDEN = {"address": "0xb", "class": "foot", "at": [10, 20], "size": [800, 600],
          "workspace": {"id": 2}, "stableId": "1800002a"}
SHOWN = {**HIDDEN, "address": "0xa", "workspace": {"id": 1}, "stableId": "18000010"}


def _png(w, h):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")


def _desktop(monkeypatch, toplevel=lambda args: _png(800, 600)):
    """hyprctl sees ONE_MONITOR and both windows; grim answers -T with
    `toplevel` and -g with a region-sized image, recording every call."""
    replies = {"monitors": ONE_MONITOR, "clients": [SHOWN, HIDDEN], "activewindow": SHOWN}
    monkeypatch.setattr(screenshot.hyprctl, "query", lambda what: replies[what])
    calls = []

    def fake_grim(args):
        calls.append(args)
        if "-T" in args:
            return toplevel(args)
        return _png(800, 600)

    monkeypatch.setattr(screenshot, "_grim", fake_grim)
    return calls


def _no_toplevel_capture(args):
    raise screenshot.ScreenshotError("grim failed: cannot find toplevel")


def test_hidden_window_is_captured_as_a_toplevel_not_a_screen_crop(monkeypatch):
    calls = _desktop(monkeypatch)
    _, meta = screenshot.capture(window="0xb")
    assert all("-g" not in a for a in calls)
    assert calls[0][-2:] == ["-T", "1800002a"]
    assert meta["geometry"] == [10, 20, 800, 600] and meta["scale"] == 1.0
    assert meta["visible"] is False
    assert "focus" in meta["coords"]  # a pointer click there lands on another window


def test_shown_window_is_marked_visible(monkeypatch):
    _desktop(monkeypatch)
    _, meta = screenshot.capture(window="0xa")
    assert meta["visible"] is True


def test_hidden_window_is_refused_when_grim_cannot_capture_toplevels(monkeypatch):
    calls = _desktop(monkeypatch, toplevel=_no_toplevel_capture)
    with pytest.raises(screenshot.ScreenshotError, match="workspace 2"):
        screenshot.capture(window="0xb")
    assert all("-g" not in a for a in calls)


def test_hidden_window_without_a_stable_id_is_refused(monkeypatch):
    # Hyprland before stableId: no toplevel handle to ask grim for
    calls = _desktop(monkeypatch)
    old = {k: v for k, v in HIDDEN.items() if k != "stableId"}
    monkeypatch.setattr(screenshot.hyprctl, "query",
                        lambda w: {"monitors": ONE_MONITOR, "clients": [old],
                                   "activewindow": SHOWN}[w])
    with pytest.raises(screenshot.ScreenshotError, match="not shown"):
        screenshot.capture(window="0xb")
    assert calls == []


def test_shown_window_falls_back_to_a_screen_crop(monkeypatch):
    calls = _desktop(monkeypatch, toplevel=_no_toplevel_capture)
    _, meta = screenshot.capture(window="0xa")
    assert "-g" in calls[-1] and "10,20 800x600" in calls[-1]
    assert meta["visible"] is True


def test_toplevel_image_that_does_not_fit_the_geometry_falls_back(monkeypatch):
    # a client-side shadow around the surface would shift every mapped
    # point, so an image that is not the window's size is not used
    calls = _desktop(monkeypatch, toplevel=lambda args: _png(860, 660))
    _, meta = screenshot.capture(window="0xa")
    assert "-g" in calls[-1]
    assert meta["image"] == [800, 600]


def test_zoom_refuses_a_hidden_window(monkeypatch):
    # zoom crops the screen, which is not showing this window
    _desktop(monkeypatch)
    with pytest.raises(screenshot.ScreenshotError, match="workspace 2"):
        screenshot.zoom_region(100, 100, window="0xb")
    assert screenshot.zoom_region(100, 100, window="0xa")[2:] == (480, 360)


def test_window_on_a_pulled_up_special_workspace_without_an_id_is_shown():
    # Hyprland 0.57: special workspaces carry a name and no id
    special = {"type": "special", "name": "special:vault"}
    monitors = [{**ONE_MONITOR[0], "specialWorkspace": special}]
    assert screenshot._shown({**HIDDEN, "workspace": special}, monitors) is True
    other = {"type": "special", "name": "special:notes"}
    assert screenshot._shown({**HIDDEN, "workspace": other}, monitors) is False


# HiDPI (verification round for 0.12.0): grim -T renders the window's own
# buffer, which Hyprland sizes at window size x the scale of the window's
# monitor, and grim's -s then scales THAT buffer. -g is different: there -s
# is an absolute logical-to-pixel factor. This fake follows both rules.
HIDPI = [
    {"id": 0, "name": "eDP-1", "x": 0, "y": 0, "width": 2880, "height": 1800,
     "scale": 2.0, "activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0}},
    {"id": 1, "name": "DP-1", "x": 1440, "y": 0, "width": 1920, "height": 1080,
     "scale": 1.0, "activeWorkspace": {"id": 3}, "specialWorkspace": {"id": 0}},
]
WIDE_HIDDEN = {"address": "0xh", "class": "foot", "at": [10, 20], "size": [1000, 600],
               "workspace": {"id": 2}, "monitor": 0, "stableId": "1800002a"}
WIDE_SHOWN = {**WIDE_HIDDEN, "address": "0xs", "workspace": {"id": 1}, "stableId": "18000010"}


def _hidpi_grim(monkeypatch, clients):
    monkeypatch.setattr(screenshot.hyprctl, "query", lambda what: {
        "monitors": HIDPI, "clients": clients, "activewindow": clients[0]}[what])
    calls = []

    def fake_grim(args):
        calls.append(args)
        s = float(args[args.index("-s") + 1]) if "-s" in args else None
        if "-T" in args:
            c = next(c for c in clients if c["stableId"] == args[args.index("-T") + 1])
            own = next(m["scale"] for m in HIDPI if m["id"] == c["monitor"])
            w, h = c["size"][0] * own, c["size"][1] * own
            f = s if s is not None else 1.0
        else:
            w, h = (int(v) for v in args[args.index("-g") + 1].split(" ")[1].split("x"))
            f = s if s is not None else 2.0  # grim's default: the greatest output scale
        return _png(round(w * f), round(h * f))

    monkeypatch.setattr(screenshot, "_grim", fake_grim)
    return calls


def test_hidpi_hidden_window_downscaled_to_the_edge_cap(monkeypatch):
    calls = _hidpi_grim(monkeypatch, [WIDE_HIDDEN])
    _, meta = screenshot.capture(window="0xh", max_edge=1568)
    assert "-g" not in calls[-1] and "-T" in calls[-1]
    assert calls[-1][calls[-1].index("-s") + 1] == "0.784"  # a fraction of the 2000 px buffer
    assert meta["image"] == [1568, 941]
    assert meta["scale"] == 1.568  # global = geometry[:2] + pixel / 1.568


def test_hidpi_covered_window_is_still_its_own_pixels(monkeypatch):
    calls = _hidpi_grim(monkeypatch, [WIDE_SHOWN])
    _, meta = screenshot.capture(window="0xs", max_edge=1568)
    assert all("-g" not in a for a in calls)
    assert meta["image"] == [1568, 941] and meta["visible"] is True


def test_hidpi_explicit_downscale(monkeypatch):
    calls = _hidpi_grim(monkeypatch, [WIDE_HIDDEN])
    _, meta = screenshot.capture(window="0xh", scale=0.5)
    assert calls[-1][calls[-1].index("-s") + 1] == "0.5"
    assert (meta["image"], meta["scale"]) == ([1000, 600], 1.0)


def test_hidpi_full_resolution(monkeypatch):
    calls = _hidpi_grim(monkeypatch, [WIDE_HIDDEN])
    _, meta = screenshot.capture(window="0xh")
    assert "-s" not in calls[-1]
    assert (meta["image"], meta["scale"]) == ([2000, 1200], 2.0)


def test_window_overhanging_a_sharper_monitor_uses_its_own_scale(monkeypatch):
    # a floating window on the 1.0 monitor whose rect pokes onto the 2.0 one:
    # its buffer is at 1.0, so the size check must not expect 2.0
    over = {**WIDE_SHOWN, "address": "0xo", "at": [1300, 100], "size": [400, 300],
            "workspace": {"id": 3}, "monitor": 1}
    calls = _hidpi_grim(monkeypatch, [over])
    _, meta = screenshot.capture(window="0xo")
    assert all("-g" not in a for a in calls)
    assert (meta["image"], meta["scale"]) == ([400, 300], 1.0)


def test_window_capture_is_refused_while_the_session_is_locked(monkeypatch):
    # Hyprland renders a window share with no session-lock check, so -T
    # would return the live window behind the human's lock screen
    calls = _desktop(monkeypatch)
    monkeypatch.setattr(screenshot.trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(screenshot.ScreenshotError, match="locked"):
        screenshot.capture(window="0xa")
    assert calls == []
    _, meta = screenshot.capture()  # the monitor shows the lock screen itself
    assert meta["target"] == "monitor"
