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


def test_keyboard_refuses_while_the_session_is_locked(stub, monkeypatch):
    # the lock screen is ext-session-lock, invisible to `layers`: this is
    # the case the layer-based guard could never see
    monkeypatch.setattr(srv.trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(srv.trust.TrustError, match="session is locked"):
        srv.keyboard("type", text="hunter2")
    assert stub["typed"] == []
    assert stub["dispatch"] == []


def test_locked_session_refusal_survives_a_window_target(stub, monkeypatch):
    monkeypatch.setattr(srv.trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(srv.trust.TrustError, match="session is locked"):
        srv.keyboard("type", text="secret", window="0xabc")
    assert stub["typed"] == []
    assert stub["dispatch"] == []  # focus never moved


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"action": "bogus", "window": "0xabc"}, "unknown action"),
        ({"action": "type", "text": "", "window": "0xabc"}, "type needs text"),
        ({"action": "key", "keys": "", "window": "0xabc"}, "key needs keys"),
    ],
)
def test_malformed_call_never_steals_focus(stub, kwargs, match):
    # focusing the target is a visible seat change; a call that goes on to
    # raise must not have moved the human's focus on its way out
    with pytest.raises(ValueError, match=match):
        srv.keyboard(**kwargs)
    assert stub["dispatch"] == []
    assert stub["typed"] == [] and stub["keys"] == []


def test_ext_session_lock_refuses_window_target_despite_allow_auth(stub, monkeypatch):
    # the MODERN locker (ext-session-lock, invisible to `layers`) is seen
    # only via session_locked(); a window= secret must be refused, not
    # typed into the prompt because allow_auth was set for the browser
    monkeypatch.setattr(srv.trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(srv.trust.TrustError, match="cannot reach the requested window"):
        srv.keyboard("type", text="browser-secret", window="0xabc", allow_auth=True)
    assert stub["typed"] == []
    assert stub["dispatch"] == []


def test_ext_session_lock_refuses_confined_typing_despite_allow_auth(stub, monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setattr(srv.trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(srv.trust.TrustError, match="confinable window"):
        srv.keyboard("type", text="secret", allow_auth=True)
    assert stub["typed"] == []


def test_windowless_keyboard_fails_closed_when_compositor_unreadable(stub, monkeypatch):
    # under confinement, a windowless type whose active window cannot be
    # resolved because hyprctl is DOWN must refuse (the wire delivers keys
    # even when hyprctl is down), not skip the confinement check and type
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")

    def boom(cmd):
        raise srv.hyprctl.HyprctlError("hyprctl timed out")

    monkeypatch.setattr(srv.hyprctl, "query", boom)
    with pytest.raises(srv.hyprctl.HyprctlError):
        srv.keyboard("type", text="unconfined")
    assert stub["typed"] == []


def test_password_field_refusal_does_not_move_focus_first(stub, monkeypatch):
    # the a11y focused-role read is per-pid, so the strict password-field
    # refusal must happen BEFORE focuswindow: a refused call must not have
    # moved the human's focus on its way out
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "strict")
    monkeypatch.setattr(srv.a11y, "connect", lambda: object())
    monkeypatch.setattr(srv.a11y, "app_for_pid", lambda *a: ("svc", "/p"))
    monkeypatch.setattr(srv.a11y, "focused_role", lambda *a: srv.a11y.PASSWORD_ROLE)
    with pytest.raises(srv.trust.TrustError, match="password entry"):
        srv.keyboard("type", text="hunter2", window="0xabc")
    assert stub["typed"] == []
    assert stub["dispatch"] == []  # focus NOT moved on the refusing path


def test_keyboard_confinement_wiring_refuses_out_of_scope_active_window(monkeypatch):
    # integration: the guard_client call site is real, not stubbed, so a
    # windowless type whose ACTIVE window is out of scope is refused
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    active = {"address": "0xff", "class": "firefox", "title": "t", "pid": 9,
              "at": [0, 0], "size": [10, 10], "workspace": {"id": 1}}
    typed = []
    monkeypatch.setattr(
        srv.hyprctl, "query",
        lambda cmd: [active] if cmd == "clients" else active,
    )
    monkeypatch.setattr(srv.hinput, "type_text", lambda t: typed.append(t))
    with pytest.raises(srv.trust.TrustError, match="confinement scope"):
        srv.keyboard("type", text="secret")  # active firefox is out of scope
    assert typed == []
