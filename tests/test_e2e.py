"""Live-session checks. Run explicitly with:  pytest -m e2e

Everything here is read-only or side-effect-free on the seat: no clicks,
no keys, no cursor movement. Input-event verification is deliberately a
separate, human-supervised script (scripts/e2e_input.py) because it
takes over the shared cursor/keyboard for a few seconds.
"""

import os

import pytest

from hypruse import hyprctl, screenshot, wire

pytestmark = pytest.mark.e2e

needs_hyprland = pytest.mark.skipif(
    not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
    reason="no live Hyprland session",
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def _is_image(data: bytes) -> bool:
    return data[:8] == PNG_MAGIC or data[:3] == JPEG_MAGIC


@needs_hyprland
def test_snapshot_live():
    snap = hyprctl.snapshot()
    assert snap["monitors"], "no monitors reported"
    assert snap["cursor"] is not None
    names = {m["name"] for m in snap["monitors"]}
    assert all(w["address"].startswith("0x") for w in snap["windows"])
    assert {w["monitor"] for w in snap["workspaces"] if w["visible"]} <= names


@needs_hyprland
def test_monitor_geometry_is_logical_and_consistent():
    """The coordinate contract, live: every monitor's reported geometry is
    its logical rect, monitors do not overlap in logical space, and each
    monitor's own logical center resolves back to itself. Exercised for
    real against multiple scaled/rotated outputs under headless CI."""
    raw = hyprctl.query("monitors")
    snap = hyprctl.snapshot()
    by_name = {m["name"]: m for m in snap["monitors"]}

    for rm in raw:
        assert by_name[rm["name"]]["geometry"] == list(hyprctl.logical_rect(rm))

    rects = [tuple(m["geometry"]) for m in snap["monitors"]]
    for i, (ax, ay, aw, ah) in enumerate(rects):
        assert aw > 0 and ah > 0
        for bx, by, bw, bh in rects[i + 1 :]:
            apart = ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay
            assert apart, "monitor logical rects overlap"

    for rm in raw:
        x, y, w, h = hyprctl.logical_rect(rm)
        hit = hyprctl.monitor_at(raw, x + w // 2, y + h // 2)
        assert hit is not None and hit["name"] == rm["name"]


@needs_hyprland
def test_screenshot_per_monitor_maps_back():
    """Capturing each monitor yields logical-origin geometry and a scale
    that folds in the monitor's fractional scale, so pixel/scale lands in
    that monitor's logical rect."""
    raw = hyprctl.query("monitors")
    for rm in raw:
        lx, ly, lw, lh = hyprctl.logical_rect(rm)
        img, meta = screenshot.capture(region=f"{lx},{ly},{min(lw, 64)}x{min(lh, 64)}")
        assert _is_image(img)
        gx = meta["geometry"][0] + (meta["image"][0] / meta["scale"]) / 2
        assert lx <= gx < lx + lw


@needs_hyprland
def test_screenshot_live_monitor_and_region():
    img, meta = screenshot.capture()
    assert _is_image(img) and meta["format"] == "jpeg"  # default is fast JPEG
    assert meta["target"] == "monitor"
    img2, meta2 = screenshot.capture(region="50,50,120x80")
    assert _is_image(img2)
    assert meta2["geometry"] == [50, 50, 120, 80]
    png, pmeta = screenshot.capture(region="50,50,120x80", lossless=True)
    assert png[:8] == PNG_MAGIC and pmeta["format"] == "png"


@needs_hyprland
def test_virtual_pointer_handshake_no_events():
    """Bind the manager and create/destroy a device, proves the protocol
    path end-to-end without sending a single input event."""
    with wire.VirtualPointer() as vp:
        assert vp._pointer > 0
    # context manager closed the connection; a leaked device would show in
    # `hyprctl devices`, creation+destroy inside one connection is silent.
