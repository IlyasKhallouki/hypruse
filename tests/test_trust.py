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


def _batch(monkeypatch, monitors, windows):
    monkeypatch.setattr(trust.hyprctl, "batch_query", lambda cmds: [monitors, windows])


MON = [{"name": "m", "x": 0, "y": 0, "width": 1920, "height": 1080,
        "scale": 1.0, "activeWorkspace": {"id": 1}}]


def test_guard_pointer_refuses_over_out_of_scope_window(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")
    windows = [
        client(addr="0xk", cls="kitty"),
        {"address": "0xbank", "class": "firefox", "at": [50, 50], "size": [200, 200],
         "workspace": {"id": 1}, "mapped": True},
    ]
    _batch(monkeypatch, MON, windows)
    trust.guard_pointer(10, 10)  # only over the in-scope kitty
    # (60,60) is over the out-of-scope firefox (and kitty): fail closed, z-order unknown
    with pytest.raises(trust.TrustError, match="outside the confinement"):
        trust.guard_pointer(60, 60)


def test_guard_pointer_over_empty_space_allowed(monkeypatch):
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")
    _batch(monkeypatch, MON, [])
    trust.guard_pointer(500, 500)  # no window there, nothing confined is touched


def test_guard_pointer_includes_special_workspace(monkeypatch):
    # a scratchpad password manager pulled up as a special workspace is
    # visibly on top; its (negative) ws id is not activeWorkspace, so the
    # coverage check must add specialWorkspace to the visible set
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")
    monitors = [{"name": "m", "activeWorkspace": {"id": 1},
                 "specialWorkspace": {"id": -99}}]
    windows = [
        {"address": "0xvault", "class": "keepassxc", "at": [40, 40], "size": [300, 300],
         "workspace": {"id": -99}, "mapped": True},
    ]
    _batch(monkeypatch, monitors, windows)
    with pytest.raises(trust.TrustError, match="outside the confinement"):
        trust.guard_pointer(50, 50)  # over the scratchpad vault, must be refused


def test_guard_pointer_coordinate_less_uses_cursor(monkeypatch):
    # a click with no x/y lands at the current cursor; the guard must
    # resolve and check THAT point, not skip
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")
    windows = [{"address": "0xother", "class": "firefox", "at": [0, 0], "size": [100, 100],
                "workspace": {"id": 1}, "mapped": True}]
    _batch(monkeypatch, MON, windows)
    monkeypatch.setattr(trust.hyprctl, "cursor_pos", lambda: (50, 50))
    with pytest.raises(trust.TrustError, match="outside the confinement"):
        trust.guard_pointer(None, None)


def test_guard_pointer_coordinate_less_fails_closed_on_cursor_error(monkeypatch):
    # the coordinate-less path resolves the cursor; if that read fails while
    # a guard is active, refuse (the wire delivers the click even when
    # hyprctl is down), never fire unchecked
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")

    def boom():
        raise trust.hyprctl.HyprctlError("cursorpos timed out")

    monkeypatch.setattr(trust.hyprctl, "cursor_pos", boom)
    with pytest.raises(trust.TrustError, match="cannot resolve the cursor"):
        trust.guard_pointer(None, None)


def test_guard_pointer_auth_over_polkit(monkeypatch):
    # default auth guard: a click over a polkit dialog is refused even with
    # no confinement configured
    windows = [{"address": "0xpk", "class": "hyprpolkitagent", "at": [0, 0],
                "size": [400, 200], "workspace": {"id": 1}, "mapped": True}]
    _batch(monkeypatch, MON, windows)
    with pytest.raises(trust.TrustError, match="authentication dialog"):
        trust.guard_pointer(10, 10)
    trust.guard_pointer(10, 10, allow_auth=True)  # override works


def test_guard_pointer_no_query_when_all_off(monkeypatch):
    monkeypatch.setenv("HYPRUSE_AUTH_GUARD", "0")

    def boom(cmds):
        raise AssertionError("must not query when no guard is active")

    monkeypatch.setattr(trust.hyprctl, "batch_query", boom)
    trust.guard_pointer(10, 10)  # confinement off + auth off: no query


def test_use_bind_refused_under_confinement(monkeypatch):
    trust.guard_use_bind()  # no confinement: allowed
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    with pytest.raises(trust.TrustError, match="cannot be confined"):
        trust.guard_use_bind()


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


def test_seat_guard_fails_closed_when_seat_unreadable(monkeypatch):
    # the module invariant is fail-toward-less-action: a seat that cannot
    # be read cannot be proven still ours, so strict mode must refuse,
    # not silently allow the action through
    monkeypatch.setenv("HYPRUSE_STRICT", "1")
    monkeypatch.setattr(trust.hyprctl, "cursor_pos", lambda: (10, 10))
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: {"address": "0xa"})
    trust.remember_seat()

    def boom():
        raise trust.hyprctl.HyprctlError("hyprctl cursorpos timed out")

    monkeypatch.setattr(trust.hyprctl, "cursor_pos", boom)
    with pytest.raises(trust.TrustError, match="cannot read the seat"):
        trust.guard_seat()


# --- ownership marking ------------------------------------------------------


def test_init_marking_installs_border_rule(monkeypatch):
    monkeypatch.setenv("HYPRUSE_MARK", "1")
    rules = []
    monkeypatch.setattr(trust.hyprctl, "keyword", lambda kw, rule: rules.append((kw, rule)))
    trust.init_marking()
    assert rules == [("windowrule", "border_color rgb(ff5555), tag hypruse-owned")]


def test_init_marking_falls_back_to_legacy_matcher(monkeypatch):
    monkeypatch.setenv("HYPRUSE_MARK", "1")
    tried = []

    def keyword(kw, rule):
        tried.append(rule)
        if "tag hypruse-owned" in rule:  # modern form rejected on older Hyprland
            raise trust.hyprctl.HyprctlError("invalid")

    monkeypatch.setattr(trust.hyprctl, "keyword", keyword)
    trust.init_marking()
    assert tried == [
        "border_color rgb(ff5555), tag hypruse-owned",
        "border_color rgb(ff5555), tag:hypruse-owned",
    ]


def test_init_marking_noop_without_flag(monkeypatch):
    called = []
    monkeypatch.setattr(trust.hyprctl, "keyword", lambda *a: called.append(a))
    trust.init_marking()
    assert called == []


def test_note_launched_tags_and_notifies_when_marking(monkeypatch):
    monkeypatch.setenv("HYPRUSE_MARK", "1")
    dispatched, notes = [], []
    monkeypatch.setattr(trust.hyprctl, "dispatch", lambda *a: dispatched.append(a))
    monkeypatch.setattr(trust.hyprctl, "notify", lambda msg, **k: notes.append(msg))
    trust.note_launched("0xnew", "firefox")
    assert "0xnew" in trust.owned()
    assert ("tagwindow", "+hypruse-owned", "address:0xnew") in dispatched
    assert notes == ["hypruse opened firefox"]  # visible presence toast


def test_note_launched_no_tag_or_notify_without_marking(monkeypatch):
    dispatched, notes = [], []
    monkeypatch.setattr(trust.hyprctl, "dispatch", lambda *a: dispatched.append(a))
    monkeypatch.setattr(trust.hyprctl, "notify", lambda *a, **k: notes.append(a))
    trust.note_launched("0xnew", "firefox")
    assert "0xnew" in trust.owned()  # still tracked for `launched` confinement
    assert dispatched == [] and notes == []  # but no tag, no toast


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
