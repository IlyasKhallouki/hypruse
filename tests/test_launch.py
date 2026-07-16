"""launch() behaviour around single-instance apps and slow startups.

Regression source: real test session 2026-07-16 — `google-chrome
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


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)


def _patch(monkeypatch, fake):
    monkeypatch.setattr(server.hyprctl, "query", fake.query)
    monkeypatch.setattr(server.hyprctl, "dispatch", fake.dispatch)


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
