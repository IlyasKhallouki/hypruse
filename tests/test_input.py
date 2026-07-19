import pytest

from hypruse import input as hinput


def test_parse_combo_mods_and_key():
    assert hinput.parse_combo("ctrl+shift+t") == (["ctrl", "shift"], "t")
    assert hinput.parse_combo("super+enter") == (["logo"], "Return")
    assert hinput.parse_combo("Alt+F4") == (["alt"], "F4")
    assert hinput.parse_combo("esc") == ([], "Escape")
    assert hinput.parse_combo("q") == ([], "q")


def test_parse_combo_bare_modifier_tap():
    assert hinput.parse_combo("super") == (["logo"], None)


def test_parse_combo_passes_unknown_keysyms_through():
    assert hinput.parse_combo("XF86AudioPlay") == ([], "XF86AudioPlay")


def test_parse_combo_rejects_garbage():
    with pytest.raises(hinput.InputError):
        hinput.parse_combo("")
    with pytest.raises(hinput.InputError, match="unknown modifier"):
        hinput.parse_combo("banana+t")


def test_combo_to_wtype_args_press_release_order():
    args = hinput.combo_to_wtype_args(["ctrl", "shift"], "t")
    assert args == ["-M", "ctrl", "-M", "shift", "-k", "t", "-m", "shift", "-m", "ctrl"]
    assert hinput.combo_to_wtype_args(["logo"], None) == ["-M", "logo", "-m", "logo"]


def test_click_validates_before_touching_compositor():
    with pytest.raises(hinput.InputError, match="both x and y"):
        hinput.click(x=100)  # y missing, must fail before any socket use
    with pytest.raises(hinput.InputError, match="unknown button"):
        hinput.click(button="laser")


def test_scroll_requires_a_direction():
    with pytest.raises(hinput.InputError, match="non-zero"):
        hinput.scroll()


class FakeVP:
    def __init__(self):
        self.events = []

    def button(self, button, state):
        self.events.append((button, state))


def test_drag_tracks_and_clears_held_button(monkeypatch):
    vp = FakeVP()
    monkeypatch.setattr(hinput, "_vp", vp)
    monkeypatch.setattr(hinput, "move", lambda x, y: None)
    monkeypatch.setattr(hinput.time, "sleep", lambda s: None)
    hinput.drag(0, 0, 10, 10)
    from hypruse.wire import PRESSED, RELEASED

    assert vp.events == [("left", PRESSED), ("left", RELEASED)]
    assert hinput._held_button is None


def test_concurrent_drags_never_interleave(monkeypatch):
    # MCP hosts can issue tool calls in parallel and sync tools run on
    # worker threads: the seat lock must keep each drag's press/move/release
    # atomic on the shared virtual pointer
    import threading

    from hypruse.wire import PRESSED, RELEASED

    vp = FakeVP()
    monkeypatch.setattr(hinput, "_vp", vp)
    monkeypatch.setattr(hinput.hyprctl, "dispatch", lambda *a: None)
    monkeypatch.setattr(hinput.time, "sleep", lambda s: None)
    threads = [threading.Thread(target=lambda: hinput.drag(0, 0, 5, 5)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [s for _, s in vp.events] == [PRESSED, RELEASED] * 4


def test_release_held_releases_mid_drag_state(monkeypatch):
    # the state a SIGTERM would see if it lands between press and release
    from hypruse.wire import RELEASED

    vp = FakeVP()
    monkeypatch.setattr(hinput, "_vp", vp)
    monkeypatch.setattr(hinput, "_held_button", "left")
    hinput.release_held()
    assert vp.events == [("left", RELEASED)]
    assert hinput._held_button is None
    hinput.release_held()  # idempotent: nothing held, nothing sent
    assert vp.events == [("left", RELEASED)]
