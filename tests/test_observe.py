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
