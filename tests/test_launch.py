"""launch() behaviour around single-instance apps and slow startups.

Regression source: real test session 2026-07-16, `google-chrome
--profile-picker` with workspace=2 opened its window from the existing
Chrome process on workspace 5, and the old 3s detection window missed it
entirely.
"""

import itertools

import pytest

from hypruse import server


class FakeHyprctl:
    def __init__(self, snapshots):
        self._snapshots = snapshots  # list of client lists, consumed per query
        self.dispatched = []

    def query(self, cmd):
        assert cmd == "clients"
        return self._snapshots[0] if len(self._snapshots) == 1 else self._snapshots.pop(0)

    def dispatch(self, *args):
        self.dispatched.append(args)


EXISTING = {"address": "0xa", "class": "kitty", "title": "~", "workspace": {"id": 5, "name": "5"}}
CHROME = {
    "address": "0xb",
    "class": "google-chrome",
    "title": "profile picker",
    "workspace": {"id": 5, "name": "5"},
}


class NoSocket:
    def __init__(self):
        raise server.events.EventError("no socket in tests")


class FakeStream:
    """Event-socket double: yields one openwindow event."""

    def __init__(self, payload):
        self._payload = payload

    def wait_for(self, names, matcher, timeout):
        assert "openwindow" in names
        return ("openwindow", self._payload) if self._payload else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)


def _patch(monkeypatch, fake, stream="none"):
    monkeypatch.setattr(server.hyprctl, "query", fake.query)
    monkeypatch.setattr(server.hyprctl, "dispatch", fake.dispatch)
    if stream == "none":
        monkeypatch.setattr(server.events, "EventStream", NoSocket)
    else:
        monkeypatch.setattr(server.events, "EventStream", lambda: stream)


def test_launch_moves_single_instance_window(monkeypatch, no_sleep):
    fake = FakeHyprctl([[EXISTING], [EXISTING], [EXISTING, CHROME]])
    _patch(monkeypatch, fake)
    out = server.launch("google-chrome", workspace="2")
    assert out["address"] == "0xb"
    assert out["workspace"] == "2"
    assert "moved" in out["note"]
    assert ("movetoworkspacesilent", "2,address:0xb") in fake.dispatched


def test_launch_no_move_when_rule_worked(monkeypatch, no_sleep):
    landed_right = dict(CHROME, workspace={"id": 2, "name": "2"})
    fake = FakeHyprctl([[EXISTING], [EXISTING, landed_right]])
    _patch(monkeypatch, fake)
    out = server.launch("google-chrome", workspace="2")
    assert out["workspace"] == 2
    assert "note" not in out
    assert not any(d[0] == "movetoworkspacesilent" for d in fake.dispatched)


def test_launch_timeout_message_mentions_single_instance(monkeypatch, no_sleep):
    fake = FakeHyprctl([[EXISTING]])  # nothing ever appears
    _patch(monkeypatch, fake)
    clock = itertools.count(start=0, step=3.0)
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    out = server.launch("slowapp", workspace="2", wait_s=8)
    assert isinstance(out, str)
    assert "single-instance" in out
    assert "desktop()" in out


def test_launch_wait_s_clamped(monkeypatch, no_sleep):
    fake = FakeHyprctl([[EXISTING], [EXISTING, CHROME]])
    _patch(monkeypatch, fake)
    out = server.launch("app", wait_s=999)  # must not loop for 999s
    assert out["address"] == "0xb"


def test_launch_uses_event_socket_when_available(monkeypatch, no_sleep):
    fake = FakeHyprctl([[EXISTING, CHROME]])  # single snapshot: lookup by address
    stream = FakeStream({"address": "0xb", "workspace": "5", "class": "google-chrome"})
    _patch(monkeypatch, fake, stream=stream)
    out = server.launch("google-chrome", workspace="2")
    assert out["address"] == "0xb"
    assert out["workspace"] == "2"  # moved: event said it landed on 5
    assert ("movetoworkspacesilent", "2,address:0xb") in fake.dispatched


def test_launch_event_timeout_message(monkeypatch, no_sleep):
    fake = FakeHyprctl([[EXISTING]])
    _patch(monkeypatch, fake, stream=FakeStream(None))
    out = server.launch("slowapp", wait_s=2)
    assert isinstance(out, str) and "no new window" in out


def test_wait_for_tool_matching(monkeypatch):
    hits = iter([("openwindow", {"address": "0xc", "class": "firefox", "title": "Docs"})])

    class S:
        def wait_for(self, names, matcher, timeout):
            for name, p in hits:
                if matcher is None or matcher(name, p):
                    return name, p
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(server.events, "EventStream", lambda: S())
    out = server.wait_for("window_open", match="FIRE")
    assert out["class"] == "firefox"


def test_wait_for_rejects_unknown_event():
    with pytest.raises(ValueError, match="unknown event"):
        server.wait_for("coffee_ready")
