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


@needs_hyprland
def test_snapshot_live():
    snap = hyprctl.snapshot()
    assert snap["monitors"], "no monitors reported"
    assert snap["cursor"] is not None
    names = {m["name"] for m in snap["monitors"]}
    assert all(w["address"].startswith("0x") for w in snap["windows"])
    assert {w["monitor"] for w in snap["workspaces"] if w["visible"]} <= names


@needs_hyprland
def test_screenshot_live_monitor_and_region():
    png, meta = screenshot.capture()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["target"] == "monitor"
    png2, meta2 = screenshot.capture(region="50,50,120x80")
    assert png2[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta2["geometry"] == [50, 50, 120, 80]


@needs_hyprland
def test_virtual_pointer_handshake_no_events():
    """Bind the manager and create/destroy a device, proves the protocol
    path end-to-end without sending a single input event."""
    with wire.VirtualPointer() as vp:
        assert vp._pointer > 0
    # context manager closed the connection; a leaked device would show in
    # `hyprctl devices`, creation+destroy inside one connection is silent.
