"""Trust layers: confinement, auth interlock, seat-contention guard,
ownership marking. Each is env-gated and fails toward less action."""

import pytest

from hypruse import trust


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("HYPRUSE_CONFINE", "HYPRUSE_AUTH_GUARD", "HYPRUSE_STRICT", "HYPRUSE_MARK"):
        monkeypatch.delenv(var, raising=False)
    trust._owned.clear()
    trust._seat["cursor"] = None
    trust._seat["active"] = None


def client(addr="0xa", cls="kitty", ws=1):
    return {"address": addr, "class": cls, "at": [0, 0], "size": [100, 100],
            "workspace": {"id": ws}, "mapped": True, "pid": 1, "title": ""}


# --- confinement ------------------------------------------------------------


def test_no_confine_allows_everything(monkeypatch):
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: [client()])
    trust.guard_client(client(cls="anything"))  # no raise
    trust.guard_window("0xa")


def test_confine_launched_only_owned(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "launched")
    trust.note_launched("0xowned")
    trust.guard_client(client(addr="0xowned"))  # ok
    with pytest.raises(trust.TrustError, match="confinement scope"):
        trust.guard_client(client(addr="0xother"))


def test_confine_by_class(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:firefox,kitty")
    trust.guard_client(client(cls="kitty"))
    with pytest.raises(trust.TrustError):
        trust.guard_client(client(cls="Signal"))


def test_confine_by_workspace(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "workspace:3,special:notes")
    trust.guard_client(client(ws=3))
    with pytest.raises(trust.TrustError):
        trust.guard_client(client(ws=1))


def test_malformed_confine_fails_closed(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "everything")
    with pytest.raises(trust.TrustError, match="malformed"):
        trust.guard_client(client())


def test_guard_point_refuses_over_out_of_scope_window(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monitors = [{"name": "m", "x": 0, "y": 0, "width": 1920, "height": 1080,
                 "scale": 1.0, "activeWorkspace": {"id": 1}}]
    windows = [
        client(addr="0xk", cls="kitty"),
        {"address": "0xbank", "class": "firefox", "at": [50, 50], "size": [200, 200],
         "workspace": {"id": 1}, "mapped": True},
    ]
    monkeypatch.setattr(
        trust.hyprctl, "query", lambda cmd: monitors if cmd == "monitors" else windows
    )
    # (10,10) is only over the in-scope kitty
    trust.guard_point(10, 10)
    # (60,60) is over the out-of-scope firefox (and kitty): fail closed, z-order unknown
    with pytest.raises(trust.TrustError, match="outside the confinement"):
        trust.guard_point(60, 60)


def test_guard_point_over_empty_space_allowed(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monitors = [{"name": "m", "x": 0, "y": 0, "width": 1920, "height": 1080,
                 "scale": 1.0, "activeWorkspace": {"id": 1}}]
    monkeypatch.setattr(
        trust.hyprctl, "query", lambda cmd: monitors if cmd == "monitors" else []
    )
    trust.guard_point(500, 500)  # no window there, nothing confined is touched


# --- auth interlock ---------------------------------------------------------


def test_auth_guard_blocks_polkit_by_default():
    with pytest.raises(trust.TrustError, match="authentication dialog"):
        trust.guard_auth_client(client(cls="hyprpolkitagent"), allow_auth=False)


def test_auth_guard_allows_ordinary_window():
    trust.guard_auth_client(client(cls="firefox"), allow_auth=False)


def test_allow_auth_overrides():
    trust.guard_auth_client(client(cls="hyprpolkitagent"), allow_auth=True)


def test_auth_guard_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")
    trust.guard_auth_client(client(cls="hyprpolkitagent"), allow_auth=False)


def test_password_field_check_is_opt_in(monkeypatch):
    # default (class-only) mode never walks the tree
    called = []
    monkeypatch.setattr(trust.a11y, "connect", lambda: called.append(1))
    trust.guard_password_field(client(), allow_auth=False)
    assert called == []


def test_password_field_blocks_in_strict_mode(monkeypatch):
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "strict")
    monkeypatch.setattr(trust.a11y, "connect", lambda: "bus")
    monkeypatch.setattr(trust.a11y, "app_for_pid", lambda *a: ("svc", "/p"))
    monkeypatch.setattr(trust.a11y, "focused_role", lambda *a: trust.a11y.PASSWORD_ROLE)
    with pytest.raises(trust.TrustError, match="password entry"):
        trust.guard_password_field(client(), allow_auth=False)


def test_password_field_fails_open_on_unreadable_tree(monkeypatch):
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "strict")

    def boom():
        raise trust.a11y.A11yError("no bus")

    monkeypatch.setattr(trust.a11y, "connect", boom)
    trust.guard_password_field(client(), allow_auth=False)  # no raise: typing allowed


# --- seat-contention guard --------------------------------------------------


def test_seat_guard_off_by_default(monkeypatch):
    monkeypatch.setattr(trust.hyprctl, "cursor_pos", lambda: (1, 1))
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: {"address": "0xa"})
    trust.remember_seat()  # no-op when not strict
    trust.guard_seat()  # no-op
    assert trust._seat["cursor"] is None


def test_seat_guard_detects_drift(monkeypatch):
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    state = {"cursor": (10, 10), "active": "0xa"}
    monkeypatch.setattr(trust.hyprctl, "cursor_pos", lambda: state["cursor"])
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: {"address": state["active"]})
    trust.remember_seat()  # stash (10,10)/0xa
    trust.guard_seat()  # unchanged: ok
    state["cursor"] = (500, 500)  # the human moved the mouse
    with pytest.raises(trust.TrustError, match="seat moved"):
        trust.guard_seat()


# --- ownership marking ------------------------------------------------------


def test_note_launched_tags_when_marking(monkeypatch):
    monkeypatch.setenv("HYPRUSE_MARK", "1")
    dispatched = []
    monkeypatch.setattr(trust.hyprctl, "dispatch", lambda *a: dispatched.append(a))
    trust.note_launched("0xnew")
    assert "0xnew" in trust.owned()
    assert ("tagwindow", "+hypruse-owned", "address:0xnew") in dispatched


def test_note_launched_no_tag_without_marking(monkeypatch):
    dispatched = []
    monkeypatch.setattr(trust.hyprctl, "dispatch", lambda *a: dispatched.append(a))
    trust.note_launched("0xnew")
    assert "0xnew" in trust.owned()  # still tracked for `launched` confinement
    assert dispatched == []  # but no border tag


def test_notify_capture_rate_limited(monkeypatch):
    monkeypatch.setenv("HYPRUSE_MARK", "1")
    notes = []
    monkeypatch.setattr(trust.hyprctl, "notify", lambda msg, **k: notes.append(msg))
    ticks = iter([100.0, 100.5, 200.0])
    monkeypatch.setattr(trust, "_last_notify", {"ts": 0.0})
    import time as _t

    monkeypatch.setattr(_t, "monotonic", lambda: next(ticks))
    trust.notify_capture()  # t=100: fires
    trust.notify_capture()  # t=100.5: within 3s, suppressed
    trust.notify_capture()  # t=200: fires again
    assert len(notes) == 2
