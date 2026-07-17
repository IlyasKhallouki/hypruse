"""The `ui` tool: resolve a window, read its a11y tree, and map
window-relative element extents to global click points."""

import pytest

from hypruse import server as srv


class FakeBus:
    pass


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.a11y, "connect", lambda: FakeBus())
    clients = [{"address": "0xw", "pid": 42, "at": [100, 200], "class": "yad", "title": "T"}]
    monkeypatch.setattr(
        srv.hyprctl,
        "query",
        lambda cmd: clients if cmd == "clients" else {"address": "0xw"},
    )
    return monkeypatch


def test_ui_maps_window_extent_to_global(wired):
    wired.setattr(srv.a11y, "app_for_pid", lambda bus, pid, title: ("a", "/root"))
    wired.setattr(
        srv.a11y,
        "find_elements",
        lambda *a, **k: [
            {"role": "button", "name": "Save", "extent": (10, 20, 80, 40), "clickable": True}
        ],
    )
    out = srv.ui(window="0xw")
    # global = at + extent origin + half-size: (100+10+40, 200+20+20)
    assert out == [{"role": "button", "name": "Save", "x": 150, "y": 240, "clickable": True}]


def test_ui_defaults_to_active_window(wired):
    seen = {}
    wired.setattr(
        srv.a11y, "app_for_pid", lambda bus, pid, title: seen.update(pid=pid) or ("a", "/root")
    )
    wired.setattr(srv.a11y, "find_elements", lambda *a, **k: [])
    srv.ui()  # no window -> active
    assert seen["pid"] == 42  # resolved the active window's client


def test_ui_no_a11y_bus_is_friendly(wired):
    def boom():
        raise srv.a11y.A11yError("no bus")

    wired.setattr(srv.a11y, "connect", boom)
    out = srv.ui(window="0xw")
    assert isinstance(out, str) and "screenshot + zoom" in out


def test_ui_app_without_tree_is_friendly(wired):
    wired.setattr(srv.a11y, "app_for_pid", lambda *a: None)
    out = srv.ui(window="0xw")
    assert isinstance(out, str) and "no accessibility tree" in out


def test_ui_no_matching_elements_is_friendly(wired):
    wired.setattr(srv.a11y, "app_for_pid", lambda *a: ("a", "/root"))
    wired.setattr(srv.a11y, "find_elements", lambda *a, **k: [])
    assert "no matching 'Nope'" in srv.ui(window="0xw", name="Nope")
    assert "no actionable" in srv.ui(window="0xw")


def test_resolve_window_errors(monkeypatch):
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: [] if cmd == "clients" else {})
    with pytest.raises(ValueError, match="no active window"):
        srv._resolve_window("")
    monkeypatch.setattr(
        srv.hyprctl, "query", lambda cmd: [] if cmd == "clients" else {"address": "0xz"}
    )
    with pytest.raises(ValueError, match="not found"):
        srv._resolve_window("0xz")
