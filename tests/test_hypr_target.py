"""hypr looks its target window up before acting (#11).

Hyprland matches an address selector as an exact lowercase string and
answers "ok" when it matches no window, so float, move and close on a
stale (or uppercase) address used to do nothing and report success, and
a dry run rehearsed them happily.
"""

import pytest

from hypruse import server as srv

LIVE = {"address": "0xabc", "class": "kitty", "title": "shell", "pid": 7,
        "at": [0, 0], "size": [800, 600], "mapped": True,
        "workspace": {"id": 1, "name": "1"}}

# every action that names a window, with the arguments it needs
TARGETED = [
    ("focus_window", {}),
    ("move_window", {"workspace": "3"}),
    ("close_window", {}),
    ("fullscreen", {}),
    ("toggle_floating", {}),
]


@pytest.fixture
def desk(monkeypatch):
    """One live window; every dispatch and every event subscription is
    recorded, so a test can see that nothing was acted on."""
    seen = {"dispatch": [], "subscribed": 0}
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query",
                        lambda cmd: [LIVE] if cmd == "clients" else {})
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: seen["dispatch"].append(a))

    class Stream:
        def __init__(self):
            seen["subscribed"] += 1

        def wait_for(self, names, matcher, timeout):
            return ("closewindow", {"address": "0xabc"})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(srv.events, "EventStream", Stream)
    return seen


@pytest.mark.parametrize(("action", "extra"), TARGETED)
def test_a_stale_address_is_refused_and_nothing_is_dispatched(desk, action, extra):
    with pytest.raises(ValueError, match="0xdeadbeef' not found, call desktop"):
        srv.hypr(action, target="0xdeadbeef", **extra)
    assert desk["dispatch"] == []
    assert desk["subscribed"] == 0  # close does not wait a second for nothing


@pytest.mark.parametrize(("action", "extra"), TARGETED)
def test_a_dry_run_refuses_a_stale_address_too(desk, monkeypatch, action, extra):
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    with pytest.raises(ValueError, match="not found"):
        srv.hypr(action, target="0xdeadbeef", **extra)
    assert desk["dispatch"] == []


def test_a_dry_run_on_a_live_window_still_plans(desk, monkeypatch):
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    out = srv.hypr("toggle_floating", target="0xabc")
    assert "would toggle floating on 0xabc" in out
    assert desk["dispatch"] == []


@pytest.mark.parametrize(("action", "extra", "sent"), [
    ("focus_window", {}, ("focuswindow", "address:0xabc")),
    ("move_window", {"workspace": "3"}, ("movetoworkspacesilent", "3,address:0xabc")),
    ("close_window", {}, ("closewindow", "address:0xabc")),
    ("toggle_floating", {}, ("togglefloating", "address:0xabc")),
])
def test_an_uppercase_address_acts_on_the_live_window(desk, action, extra, sent):
    # the address the human typed, or copied from somewhere that shouts:
    # Hyprland would match none of it, so hypr sends the name Hyprland uses
    srv.hypr(action, target="0xABC", **extra)
    assert desk["dispatch"][0] == sent


def test_the_reply_names_the_window_as_hyprland_does(desk):
    assert srv.hypr("move_window", target="0xABC", workspace="3") == (
        "moved 0xabc to workspace 3"
    )


def test_a_targetless_action_is_not_refused_for_want_of_a_target(desk, monkeypatch):
    # no target means the active window, which Hyprland finds itself
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: [] if cmd == "clients" else {})
    assert srv.hypr("toggle_floating") == "floating toggled"
    assert desk["dispatch"] == [("togglefloating",)]


def test_an_uppercase_address_does_not_slip_past_confinement(desk, monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:firefox")
    with pytest.raises(srv.trust.TrustError, match="confinement scope"):
        srv.hypr("close_window", target="0xABC")  # kitty, out of scope
    assert desk["dispatch"] == []
