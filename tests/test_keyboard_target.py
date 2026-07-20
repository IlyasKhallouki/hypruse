"""keyboard(window=...) focuses the target window before typing, so
keystrokes never land in whatever happens to hold focus."""

import pytest

from hypruse import server as srv


@pytest.fixture
def stub(monkeypatch):
    calls = {"dispatch": [], "typed": [], "keys": [], "slept": []}
    clients = [
        {"address": "0xabc", "class": "kitty", "title": "t", "pid": 1,
         "at": [0, 0], "size": [10, 10], "workspace": {"id": 1}},
    ]
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)

    def query(cmd):
        if cmd == "clients":
            return clients
        if cmd == "activewindow":
            return clients[0]
        return {}

    monkeypatch.setattr(srv.hyprctl, "query", query)
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: calls["dispatch"].append(a))
    monkeypatch.setattr(srv.hinput, "type_text", lambda t: calls["typed"].append(t))
    monkeypatch.setattr(srv.hinput, "key_combo", lambda k: calls["keys"].append(k))
    monkeypatch.setattr(srv.time, "sleep", lambda s: calls["slept"].append(s))
    return calls


def test_type_focuses_window_first(stub):
    out = srv.keyboard("type", text="hi", window="0xabc")
    assert stub["dispatch"] == [("focuswindow", "address:0xabc")]
    assert stub["typed"] == ["hi"]
    assert stub["slept"]  # settled before typing
    assert out == "typed 2 characters into 0xabc"


def test_key_focuses_window_first(stub):
    out = srv.keyboard("key", keys="ctrl+t", window="0xabc")
    assert stub["dispatch"] == [("focuswindow", "address:0xabc")]
    assert stub["keys"] == ["ctrl+t"]
    assert out == "pressed ctrl+t into 0xabc"


def test_no_window_does_not_focus(stub):
    out = srv.keyboard("type", text="hi")
    assert stub["dispatch"] == []
    assert out == "typed 2 characters"


def test_bad_window_address_rejected(stub):
    with pytest.raises(ValueError, match="not a window address"):
        srv.keyboard("type", text="hi", window="firefox")
    assert stub["typed"] == []  # nothing typed into the wrong place


def test_window_target_composes_with_then(stub, monkeypatch):
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {"ok": 1})
    out = srv.keyboard("type", text="hi", window="0xabc", then="desktop")
    assert isinstance(out, list)
    assert out[0].text == "typed 2 characters into 0xabc"


LAUNCHER_LAYERS = {
    "DP-1": {"levels": {"3": [{"namespace": "rofi", "x": 560, "y": 300,
                               "w": 800, "h": 480}]}}
}

LOCK_LAYERS = {
    "DP-1": {"levels": {"3": [{"namespace": "hyprlock", "x": 0, "y": 0,
                               "w": 1920, "h": 1080}]}}
}


def _with_layers(monkeypatch, layers):
    prev = srv.hyprctl.query
    monkeypatch.setattr(
        srv.hyprctl, "query", lambda cmd: layers if cmd == "layers" else prev(cmd)
    )


def test_keyboard_window_target_refused_while_launcher_grabs(stub, monkeypatch):
    # keys go to the seat's keyboard focus, and rofi holds the grab no
    # matter what focuswindow does: 'typed into 0xabc' would be a lie
    _with_layers(monkeypatch, LAUNCHER_LAYERS)
    with pytest.raises(srv.trust.TrustError, match="rofi"):
        srv.keyboard("type", text="hi", window="0xabc")
    assert stub["typed"] == []
    assert stub["dispatch"] == []  # refused before even focusing


def test_keyboard_windowless_drives_the_launcher_with_a_note(stub, monkeypatch):
    _with_layers(monkeypatch, LAUNCHER_LAYERS)
    out = srv.keyboard("type", text="firefox")
    assert stub["typed"] == ["firefox"]
    assert "rofi" in out and "keyboard grab" in out


def test_keyboard_refused_under_a_lock_screen(stub, monkeypatch):
    _with_layers(monkeypatch, LOCK_LAYERS)
    with pytest.raises(srv.trust.TrustError, match="hyprlock"):
        srv.keyboard("type", text="hunter2")
    assert stub["typed"] == []
    out = srv.keyboard("type", text="hunter2", allow_auth=True)  # human intent
    assert stub["typed"] == ["hunter2"]
    assert "hyprlock" in out


def test_lock_screen_never_absorbs_a_window_targeted_secret(stub, monkeypatch):
    # HYPRUSE_AUTH_GUARD=strict's documented way to fill a browser login
    # is keyboard(type, window=0xbrowser, allow_auth=true); if an idle
    # timeout maps hyprlock in between, that secret must NOT be typed into
    # the lock prompt just because allow_auth was set for the browser
    _with_layers(monkeypatch, LOCK_LAYERS)
    with pytest.raises(srv.trust.TrustError, match="cannot reach the requested window"):
        srv.keyboard("type", text="browser-secret", window="0xabc", allow_auth=True)
    assert stub["typed"] == []
    assert stub["dispatch"] == []


def test_windowless_launcher_typing_refused_under_confinement(stub, monkeypatch):
    # a launcher executes whatever is typed into it, so under confinement
    # this is arbitrary out-of-scope execution: the same escape use_bind
    # is refused for
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    _with_layers(monkeypatch, LAUNCHER_LAYERS)
    with pytest.raises(srv.trust.TrustError, match="cannot be confined"):
        srv.keyboard("type", text="malicious-command")
    assert stub["typed"] == []
