"""Act-and-observe fusion: an acting tool can append a fresh view of the
result to its own tool result, saving the agent a second round-trip."""

import json

import pytest
from mcp.types import TextContent

from hypruse import server as srv


def test_then_none_returns_bare_string():
    assert srv._acted("did it", "none") == "did it"


def test_then_desktop_appends_snapshot(monkeypatch):
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {"monitors": ["m"], "windows": []})
    out = srv._acted("clicked", "desktop")
    assert isinstance(out, list) and len(out) == 2
    assert out[0].text == "clicked"
    assert json.loads(out[1].text)["monitors"] == ["m"]


def test_then_screenshot_appends_capture(monkeypatch):
    monkeypatch.setattr(
        srv, "_deliver_capture", lambda **kw: [TextContent(type="text", text="IMG")]
    )
    out = srv._acted("clicked", "screenshot")
    assert isinstance(out, list)
    assert [c.text for c in out] == ["clicked", "IMG"]


def test_screenshot_observation_waits_for_stable(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        srv, "_deliver_capture", lambda **kw: seen.update(kw) or [TextContent(type="text", text="")]
    )
    srv._acted("x", "screenshot")
    assert seen.get("stable") is True


def test_unknown_then_raises():
    with pytest.raises(ValueError, match="unknown then"):
        srv._acted("x", "wat")


def test_then_ui_appends_elements(monkeypatch):
    elements = [{"role": "entry", "name": "Email", "x": 5, "y": 6, "value": "hi@x"}]
    monkeypatch.setattr(srv, "_ui_read", lambda window="": elements)
    out = srv._acted("typed", "ui")
    assert out[0].text == "typed"
    assert json.loads(out[1].text) == elements


def test_then_ui_reads_the_callers_window(monkeypatch):
    # a caller that knows which window it acted on passes it through, so
    # the observation shows THAT window, not whatever holds focus now
    seen = {}
    monkeypatch.setattr(
        srv, "_ui_read", lambda window="": seen.update(window=window) or []
    )
    srv._acted("clicked", "ui", window="0xw")
    assert seen["window"] == "0xw"


def test_then_ui_degrades_without_a_tree(monkeypatch):
    monkeypatch.setattr(srv, "_ui_read", lambda window="": "kitty exposes no tree")
    out = srv._acted("typed", "ui")
    assert out[1].text == "kitty exposes no tree"


def test_then_ui_never_masks_the_action(monkeypatch):
    # the action succeeded; a failing observation must not turn it into an error
    def boom(window=""):
        raise ValueError("no active window")

    monkeypatch.setattr(srv, "_ui_read", boom)
    out = srv._acted("clicked", "ui")
    assert out[0].text == "clicked"
    assert "ui read failed" in out[1].text


def test_pointer_fuses_observation(monkeypatch):
    monkeypatch.setattr(srv.hinput, "move", lambda x, y: None)
    monkeypatch.setattr(srv.hyprctl, "cursor_pos", lambda: (10, 20))
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {"cursor": [10, 20]})
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)

    bare = srv.pointer("move", x=10, y=20)
    assert bare == "move ok; cursor now at (10, 20)"

    fused = srv.pointer("move", x=10, y=20, then="desktop")
    assert isinstance(fused, list)
    assert fused[0].text.startswith("move ok")
    assert json.loads(fused[1].text)["cursor"] == [10, 20]


def test_hypr_fuses_observation(monkeypatch):
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {"ok": True})
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)

    assert srv.hypr("workspace", workspace="3") == "on workspace 3"
    fused = srv.hypr("workspace", workspace="3", then="desktop")
    assert isinstance(fused, list) and json.loads(fused[1].text) == {"ok": True}


def test_hypr_targetless_fullscreen_honors_seat_guard(monkeypatch):
    # target-less fullscreen/toggle_floating act on the ACTIVE window; under
    # HYPRUSE_STRICT, if the human refocused since hypruse last acted, they
    # would hit the wrong window, so the seat guard must refuse
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: None)
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    # seat baseline says cursor (1,1)/active 0xa; now it reads (9,9)/0xb -> moved
    srv.trust._seat.update(cursor=(1, 1), active="0xa")
    monkeypatch.setattr(srv.hyprctl, "cursor_pos", lambda: (9, 9))
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"address": "0xb"})
    with pytest.raises(srv.trust.TrustError, match="seat moved"):
        srv.hypr("fullscreen")  # no target -> guarded
    # an address-targeted action names its window, so it is not seat-gated
    srv.trust._seat.update(cursor=(1, 1), active="0xa")
    assert srv.hypr("fullscreen", target="0xabc") == "fullscreen toggled"


def test_desktop_rebaselines_the_strict_seat_guard(monkeypatch):
    # the lockout bug: once the human nudged the seat, the guard refused
    # forever, because its own advice ("re-read desktop() and retry") never
    # re-baselined anything. A desktop() read must re-arm the guard.
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    srv.trust._seat.update(cursor=(1, 1), active="0xa")
    monkeypatch.setattr(srv.hyprctl, "cursor_pos", lambda: (9, 9))
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"address": "0xb"})
    with pytest.raises(srv.trust.TrustError, match="seat moved"):
        srv.trust.guard_seat()
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {"windows": []})
    srv.desktop()
    srv.trust.guard_seat()  # re-armed: no raise


def test_capture_rebaselines_the_strict_seat_guard(monkeypatch):
    # same recovery path for screenshot/zoom: any fresh capture counts as
    # the re-observation the guard error asks for
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    srv.trust._seat.update(cursor=(1, 1), active="0xa")
    monkeypatch.setattr(srv.hyprctl, "cursor_pos", lambda: (9, 9))
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"address": "0xb"})
    monkeypatch.setattr(srv, "_grab_env", lambda *a, **k: (b"IMG", {"format": "jpeg"}))
    monkeypatch.setattr(srv, "_package", lambda data, meta: ["pkg"])
    with pytest.raises(srv.trust.TrustError, match="seat moved"):
        srv.trust.guard_seat()
    srv._deliver_capture()
    srv.trust.guard_seat()  # re-armed: no raise


def _pointer_wired(monkeypatch, covering):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.trust, "guard_pointer", lambda *a, **k: None)
    monkeypatch.setattr(srv.trust, "covering_layer", covering)
    monkeypatch.setattr(srv.hinput, "click", lambda *a, **k: None)
    monkeypatch.setattr(srv.hinput, "drag", lambda *a, **k: None)
    monkeypatch.setattr(srv.hinput, "scroll", lambda *a, **k: None)
    monkeypatch.setattr(srv.hyprctl, "cursor_pos", lambda: (600, 350))


@pytest.mark.parametrize(
    "action,extra",
    [
        ("click", {}),
        ("scroll", {"scroll_dy": 3}),
        ("drag", {"to_x": 700, "to_y": 400}),
    ],
)
def test_pointer_warns_under_a_focus_stealing_layer(monkeypatch, action, extra):
    # pointer may legitimately aim at the layer itself (that is how a
    # launcher is driven), so every input-delivering arm warns instead of
    # refusing, and the warning tells the truth about where the input went
    _pointer_wired(
        monkeypatch, lambda x, y: {"namespace": "rofi", "kind": "launcher"}
    )
    out = srv.pointer(action, x=600, y=350, **extra)
    assert "rofi" in out and "not to any window beneath" in out


def test_pointer_click_is_silent_without_a_covering_layer(monkeypatch):
    _pointer_wired(monkeypatch, lambda x, y: None)
    assert srv.pointer("click", x=600, y=350) == "click ok; cursor now at (600, 350)"


def test_pointer_coordinate_less_click_resolves_cursor_for_the_note(monkeypatch):
    # a click with no x/y lands at the current cursor; the layer note must
    # check THAT point, not skip
    probed = []

    def covering(x, y):
        probed.append((x, y))
        return {"namespace": "rofi", "kind": "launcher"}

    _pointer_wired(monkeypatch, covering)
    out = srv.pointer("click")
    assert probed == [(600, 350)]
    assert "rofi" in out


def test_layer_note_empty_when_cursor_unreadable(monkeypatch):
    # best-effort: no cursor means no note, never an error blocking the click
    def boom():
        raise srv.hyprctl.HyprctlError("cursorpos timed out")

    monkeypatch.setattr(srv.hyprctl, "cursor_pos", boom)
    assert srv._layer_note(None, None) == ""
