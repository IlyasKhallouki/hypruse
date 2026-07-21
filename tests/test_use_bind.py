import pytest

from hypruse import hyprctl, server

BINDS = [
    {"combo": "SUPER+F", "action": "exec", "arg": "kitty --class wb-float-files -e yazi"},
    {"combo": "SUPER+2", "action": "workspace", "arg": "2"},
    {"combo": "SUPER+W", "action": "togglefloating", "arg": ""},
]


@pytest.fixture
def fake_binds(monkeypatch):
    monkeypatch.setattr(hyprctl, "binds", lambda: BINDS)
    dispatched = []
    monkeypatch.setattr(hyprctl, "dispatch", lambda *a: dispatched.append(a))
    return dispatched


def test_find_bind_case_insensitive(fake_binds):
    assert hyprctl.find_bind("super+f")["action"] == "exec"
    assert hyprctl.find_bind("SUPER + 2")["arg"] == "2"
    assert hyprctl.find_bind("super+nope") is None


def test_use_bind_runs_exec_action(fake_binds):
    out = server.use_bind("super+f")
    assert fake_binds == [("exec", "kitty --class wb-float-files -e yazi")]
    assert "SUPER+F" in out


def test_use_bind_runs_dispatcher_action(fake_binds):
    server.use_bind("SUPER+2")
    assert fake_binds == [("workspace", "2")]


def test_use_bind_empty_arg_dispatched_bare(fake_binds):
    server.use_bind("super+w")
    assert fake_binds == [("togglefloating",)]


def test_use_bind_unknown_raises(fake_binds):
    with pytest.raises(ValueError, match="no keybind"):
        server.use_bind("super+q")


def test_wait_for_close_already_gone(monkeypatch):
    # no client matches -> already closed, returns without touching the socket
    def _no_connect():
        raise AssertionError("should not connect when already satisfied")

    monkeypatch.setattr(hyprctl, "query", lambda cmd: [] if cmd == "clients" else {})
    monkeypatch.setattr(server.events, "EventStream", _no_connect)
    out = server.wait_for("window_close", match="0xdead")
    assert out["already"] is True


def test_wait_for_close_still_open_waits(monkeypatch):
    monkeypatch.setattr(
        hyprctl, "query", lambda cmd: [{"address": "0xabc", "class": "kitty", "title": "x"}]
    )

    class S:
        def wait_for(self, names, matcher, timeout):
            return ("closewindow", {"address": "0xabc"})

        def __enter__(self):
            return self

        def __exit__(self, *e):
            pass

    monkeypatch.setattr(server.events, "EventStream", lambda: S())
    out = server.wait_for("window_close", match="0xabc")
    assert out["event"] == "closewindow" and "already" not in out


def test_wait_for_workspace_already_active(monkeypatch):
    def _no_connect():
        raise AssertionError("should not connect when already satisfied")

    monkeypatch.setattr(hyprctl, "query", lambda cmd: {"name": "3"})
    monkeypatch.setattr(server.events, "EventStream", _no_connect)
    out = server.wait_for("workspace", match="3")
    assert out["already"] is True and out["name"] == "3"


def test_use_bind_refused_under_confinement(fake_binds, monkeypatch):
    # integration: the guard_use_bind call site is real; a bind runs an
    # arbitrary compositor action that cannot be scoped, so confinement
    # refuses it wholesale, before dispatching anything
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    with pytest.raises(server.trust.TrustError, match="cannot be confined"):
        server.use_bind("super+f")
    assert fake_binds == []  # nothing dispatched


def test_hypr_focus_window_refused_out_of_scope(monkeypatch):
    # integration: hypr's guard_window(target) call site is real; focusing
    # an out-of-scope window by address is refused before dispatch
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setattr(server.safety, "touch", lambda *a: None)
    firefox = {"address": "0xff", "class": "firefox", "workspace": {"id": 1},
               "at": [0, 0], "size": [10, 10], "mapped": True}
    dispatched = []
    monkeypatch.setattr(server.hyprctl, "query", lambda cmd: [firefox])
    monkeypatch.setattr(server.hyprctl, "dispatch", lambda *a: dispatched.append(a))
    with pytest.raises(server.trust.TrustError, match="confinement scope"):
        server.hypr("focus_window", target="0xff")
    assert dispatched == []
