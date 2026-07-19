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
