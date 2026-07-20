"""Trust layers: confinement, auth interlock, seat-contention guard,
ownership marking. Each is env-gated and fails toward less action."""

import pytest

from hypruse import trust

# bound at import time, so conftest's autouse stub does not hide it
from hypruse.trust import session_locked as real_session_locked


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


# --- focus-stealing layer coverage -------------------------------------------


LAYERS_RAW = {
    "DP-1": {
        "levels": {
            "2": [{"namespace": "waybar", "x": 0, "y": 0, "w": 1920, "h": 30}],
            "3": [{"namespace": "rofi", "x": 560, "y": 300, "w": 800, "h": 480}],
        }
    }
}


def test_covering_layer_finds_the_launcher_over_the_point(monkeypatch):
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LAYERS_RAW)
    hit = trust.covering_layer(600, 350)
    assert hit["namespace"] == "rofi" and hit["kind"] == "launcher"
    assert trust.covering_layer(600, 900) is None  # below the surface


def test_covering_layer_ignores_bars(monkeypatch):
    # a bar covers its strip permanently but is not a seat takeover
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LAYERS_RAW)
    assert trust.covering_layer(10, 10) is None


def test_covering_layer_fails_open_when_layers_unreadable(monkeypatch):
    # truthfulness aid, not a confinement boundary: an unreadable layer
    # list must not start blocking every click
    def boom(cmd):
        raise trust.hyprctl.HyprctlError("hyprctl down")

    monkeypatch.setattr(trust.hyprctl, "query", boom)
    assert trust.covering_layer(1, 1) is None


def test_guard_covering_layer_refuses_with_the_layer_named(monkeypatch):
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LAYERS_RAW)
    with pytest.raises(trust.TrustError, match="rofi"):
        trust.guard_covering_layer(600, 350)
    trust.guard_covering_layer(600, 900)  # uncovered point: no raise


def test_covering_layer_skips_partial_geometry(monkeypatch):
    # hyprctl can report a surface without w/h; skip it, never TypeError
    raw = {"DP-1": {"levels": {"3": [{"namespace": "rofi", "x": 0, "y": 0}]}}}
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: raw)
    assert trust.covering_layer(5, 5) is None


# --- keyboard-grabbing layers -------------------------------------------------


LOCK_RAW = {
    "DP-1": {
        "levels": {"3": [{"namespace": "hyprlock", "x": 0, "y": 0, "w": 1920, "h": 1080}]}
    }
}


def test_guard_keyboard_layer_quiet_without_grabbers(monkeypatch):
    # bars and notification popups do not hold the keyboard
    raw = {"DP-1": {"levels": {"2": [{"namespace": "waybar", "x": 0, "y": 0,
                                      "w": 1920, "h": 30}]}}}
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: raw)
    assert trust.guard_keyboard_layer(True, False) == ""


def test_guard_keyboard_layer_refuses_window_target_under_launcher(monkeypatch):
    # keyboard(window=X) promises keys land in X; a launcher's grab makes
    # that promise false, so it must refuse rather than type into rofi
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LAYERS_RAW)
    with pytest.raises(trust.TrustError, match="rofi"):
        trust.guard_keyboard_layer(True, False)


def test_guard_keyboard_layer_window_target_refused_under_lock_despite_allow_auth(
    monkeypatch,
):
    # allow_auth means 'a human intends credential entry HERE', which
    # contradicts naming some other target window: typing a browser
    # password into a lock screen that mapped mid-flow must still refuse
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LOCK_RAW)
    with pytest.raises(trust.TrustError, match="cannot reach the requested window"):
        trust.guard_keyboard_layer(True, True)


def test_guard_keyboard_layer_refuses_launcher_under_confinement(monkeypatch):
    # a launcher runs whatever is typed into it and is not a window, so no
    # scope can contain it: the same escape guard_use_bind refuses
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LAYERS_RAW)
    with pytest.raises(trust.TrustError, match="cannot be confined"):
        trust.guard_keyboard_layer(False, False)


def test_guard_keyboard_layer_confinement_outranks_allow_auth(monkeypatch):
    # allow_auth overrides the auth guard, never the confinement scope
    monkeypatch.setenv("HYPRUSE_CONFINE", "class:kitty")
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LOCK_RAW)
    with pytest.raises(trust.TrustError, match="cannot be confined"):
        trust.guard_keyboard_layer(False, True)


def test_guard_keyboard_layer_notes_windowless_typing_into_launcher(monkeypatch):
    # typing with no window while a launcher is up is the legitimate way
    # to drive one: allowed, but the result must say where the keys went
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LAYERS_RAW)
    note = trust.guard_keyboard_layer(False, False)
    assert "rofi" in note and "keyboard grab" in note


def test_guard_keyboard_layer_lock_screen_refuses_even_windowless(monkeypatch):
    # a lock screen's focused control is a credential prompt
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LOCK_RAW)
    with pytest.raises(trust.TrustError, match="hyprlock"):
        trust.guard_keyboard_layer(False, False)


def test_guard_keyboard_layer_lock_allow_auth_downgrades_to_note(monkeypatch):
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LOCK_RAW)
    note = trust.guard_keyboard_layer(False, True)
    assert "hyprlock" in note


def test_guard_keyboard_layer_fails_open_on_unreadable_layers(monkeypatch):
    def boom(cmd):
        raise trust.hyprctl.HyprctlError("hyprctl down")

    monkeypatch.setattr(trust.hyprctl, "query", boom)
    assert trust.guard_keyboard_layer(True, False) == ""


# --- session lock (ext-session-lock, invisible to `layers`) -------------------


def _fake_proc(tmp_path, procs):
    """A /proc tree with the given {pid: comm}, plus a non-pid entry."""
    for pid, comm in procs.items():
        d = tmp_path / pid
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
    (tmp_path / "self").mkdir()  # a real /proc has non-numeric entries too
    return str(tmp_path)


def test_session_locked_reads_proc(monkeypatch, tmp_path):
    # hyprlock is an ext-session-lock-v1 client, so it appears in NEITHER
    # `hyprctl clients` nor `hyprctl layers`; the process is the signal
    monkeypatch.setattr(
        trust, "_PROC", _fake_proc(tmp_path, {"101": "waybar", "102": "hyprlock"})
    )
    assert real_session_locked() == "hyprlock"


def test_session_locked_none_when_no_locker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        trust, "_PROC", _fake_proc(tmp_path, {"101": "waybar", "102": "kitty"})
    )
    assert real_session_locked() is None


def test_session_locked_survives_a_process_exiting_mid_scan(monkeypatch, tmp_path):
    # /proc entries vanish under you; a disappearing pid must not raise
    root = _fake_proc(tmp_path, {"101": "kitty", "102": "hyprlock"})
    (tmp_path / "103").mkdir()  # a pid dir with no readable comm
    monkeypatch.setattr(trust, "_PROC", root)
    assert real_session_locked() == "hyprlock"


def test_session_locked_fails_open_on_unreadable_proc(monkeypatch):
    def boom(path):
        raise OSError("no /proc")

    monkeypatch.setattr(trust.os, "scandir", boom)
    assert real_session_locked() is None  # best-effort, never blocks


def test_guard_session_lock_refuses_and_allow_auth_downgrades(monkeypatch):
    monkeypatch.setattr(trust, "session_locked", lambda: "hyprlock")
    with pytest.raises(trust.TrustError, match="session is locked"):
        trust.guard_session_lock(allow_auth=False)
    note = trust.guard_session_lock(allow_auth=True)
    assert "hyprlock" in note and "credential prompt" in note


def test_guard_session_lock_silent_when_unlocked(monkeypatch):
    monkeypatch.setattr(trust, "session_locked", lambda: None)
    assert trust.guard_session_lock(allow_auth=False) == ""


# --- topmost surface resolution ----------------------------------------------


def test_covering_layer_picks_the_topmost_overlapping_surface(monkeypatch):
    # two focus-stealing surfaces over the same point: the one that gets
    # the click is the higher level, and naming the other would misinform
    raw = {
        "DP-1": {
            "levels": {
                "2": [{"namespace": "wvkbd", "x": 0, "y": 0, "w": 1000, "h": 1000}],
                "3": [{"namespace": "rofi", "x": 0, "y": 0, "w": 1000, "h": 1000}],
            }
        }
    }
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: raw)
    assert trust.covering_layer(500, 500)["namespace"] == "rofi"  # overlay > top


def test_covering_layer_last_mapped_wins_within_a_level(monkeypatch):
    raw = {
        "DP-1": {
            "levels": {
                "3": [
                    {"namespace": "rofi", "x": 0, "y": 0, "w": 1000, "h": 1000},
                    {"namespace": "fuzzel", "x": 0, "y": 0, "w": 1000, "h": 1000},
                ]
            }
        }
    }
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: raw)
    assert trust.covering_layer(500, 500)["namespace"] == "fuzzel"


def test_refusal_advice_is_written_per_kind(monkeypatch):
    # 'usually esc' is false for a lock screen (the one surface designed
    # not to yield to a keystroke) and for an on-screen keyboard
    assert "esc" in trust._dismiss_advice("launcher")
    assert "esc" not in trust._dismiss_advice("lock")
    assert "Unlock" in trust._dismiss_advice("lock")
    assert "esc" not in trust._dismiss_advice("osk")


def test_lock_layer_refusal_does_not_recommend_a_path_that_refuses(monkeypatch):
    # the window= refusal used to say 'drive the layer itself', which
    # routes straight into the credential refusal below it
    monkeypatch.setattr(trust.hyprctl, "query", lambda cmd: LOCK_RAW)
    with pytest.raises(trust.TrustError) as exc:
        trust.guard_keyboard_layer(True, False)
    assert "without window=" not in str(exc.value)
