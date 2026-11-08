"""AT-SPI accessibility reader (a11y.py). The traversal/filtering is tested
against a fake tree; the busctl JSON parsing against canned output."""

import subprocess

import pytest

from hypruse import a11y

# --- busctl JSON parsing (the shapes are easy to get subtly wrong) ---


def _fake_proc(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_bus_address_unwraps_call_data(monkeypatch):
    # GetAddress is a method call, so data is list-wrapped: data[0] is the value
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")
    monkeypatch.setattr(
        a11y.subprocess,
        "run",
        lambda *a, **k: _fake_proc('{"type":"s","data":["unix:path=/run/a11y"]}'),
    )
    assert a11y.bus_address() == "unix:path=/run/a11y"


def test_bus_address_raises_without_apps(monkeypatch):
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")
    empty = '{"type":"s","data":[""]}'
    monkeypatch.setattr(a11y.subprocess, "run", lambda *a, **k: _fake_proc(empty))
    with pytest.raises(a11y.A11yError, match="no address"):
        a11y.bus_address()


def test_busctl_missing_binary(monkeypatch):
    monkeypatch.setattr(a11y.shutil, "which", lambda n: None)
    with pytest.raises(a11y.A11yError, match="busctl not found"):
        a11y.bus_address()


def test_busctl_error_surfaces_stderr(monkeypatch):
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")
    monkeypatch.setattr(a11y.subprocess, "run", lambda *a, **k: _fake_proc("", 1, "boom"))
    with pytest.raises(a11y.A11yError, match="boom"):
        a11y.bus_address()


def test_bus_call_and_prop_shapes(monkeypatch):
    # every busctl shape the reader parses: (iiii) struct, a(so) array of
    # refs, au state words, scalar property. Easy to get subtly wrong.
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")
    outputs = iter(
        [
            '{"type":"(iiii)","data":[[1,2,3,4]]}',
            '{"type":"a(so)","data":[[[":1.5","/a"],[":1.5","/b"]]]}',
            '{"type":"au","data":[[8,0]]}',
            '{"type":"i","data":7}',
        ]
    )
    monkeypatch.setattr(a11y.subprocess, "run", lambda *a, **k: _fake_proc(next(outputs)))
    bus = a11y.Bus("unix:x")
    assert bus.call("s", "/p", "i", "GetExtents")[0] == [1, 2, 3, 4]
    assert bus.call("s", "/p", "i", "GetChildren")[0] == [[":1.5", "/a"], [":1.5", "/b"]]
    assert bus.call("s", "/p", "i", "GetState")[0] == [8, 0]
    assert bus.prop("s", "/p", "i", "ChildCount") == 7


def test_busctl_timeout_becomes_a11yerror(monkeypatch):
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="busctl", timeout=10)

    monkeypatch.setattr(a11y.subprocess, "run", boom)
    with pytest.raises(a11y.A11yError, match="timed out"):
        a11y.bus_address()


# --- traversal against a fake tree ---


class FakeBus:
    """A canned AT-SPI tree. `nodes` maps (svc, path) -> dict with role
    (int), name, extent, states (set of bit indices), children [(svc,path)].
    `pids` maps svc -> connection pid, `toolkits` svc -> ToolkitName."""

    def __init__(self, nodes, pids=None, toolkits=None):
        self.nodes = nodes
        self.pids = pids or {}
        self.toolkits = toolkits or {}

    def call(self, svc, path, iface, method, *sig):
        node = self.nodes[(svc, path)]
        if method == "GetInterfaces":
            return [node.get("ifaces", [])]
        if method == "GetText":
            start, end = int(sig[1]), int(sig[2])  # sig = (signature, start, end)
            return [node.get("text", "")[start:end]]
        if method == "GetChildren":
            return [[list(c) for c in node["children"]]]
        if method == "GetRole":
            return [node["role"]]
        if method == "GetRoleName":
            if node.get("role_name", "widget") is None:  # AccessKit lacks the method
                raise a11y.CallRefused("GetRoleName failed: Unknown method")
            return [node.get("role_name", "widget")]
        if method == "GetExtents":
            return [list(node["extent"])] if node.get("extent") else [[0, 0, 0, 0]]
        if method == "GetState":
            lo = sum(1 << b for b in node.get("states", set()) if b < 32)
            hi = sum(1 << (b - 32) for b in node.get("states", set()) if b >= 32)
            return [[lo, hi]]
        if method == "DoAction":
            node["done"] = True
            return [True]
        if method == "GetName":  # Action.GetName(index)
            return [node.get("actions", ["click"])[int(sig[1])]]
        if method == "GetRelationSet":
            return [[[t, [list(x) for x in targets]] for t, targets in node.get("relations", [])]]
        if method == "GrabFocus":
            node["grabbed"] = True
            return [True]
        if method == "InsertText":
            pos, text = int(sig[1]), sig[2]  # sig = (signature, position, text, length)
            n = int(sig[3])
            if self.toolkits.get(svc) == "Qt":  # QString::resize: UTF-16 units, padded
                units = text.encode("utf-16-le")[:2 * n].decode("utf-16-le", errors="ignore")
                text = units + "\0" * (n - len(text.encode("utf-16-le")) // 2)
            else:  # GTK (ATK) reads the length in BYTES, as UTF-8
                text = text.encode()[:n].decode(errors="ignore")
            old = node.get("text", "")
            node["text"] = old[:pos] + text + old[pos:]
            return [True]
        raise AssertionError(method)

    def prop(self, svc, path, iface, name):
        if name == "ToolkitName":
            return self.toolkits.get(svc, "GTK")
        if name == "Version":  # the toolkit's
            return "6.11.1" if self.toolkits.get(svc) == "Qt" else "4.22.4"
        node = self.nodes[(svc, path)]
        if name == "CharacterCount":
            return len(node.get("text", ""))
        if name == "NActions":  # a property in AT-SPI, not a method
            return node.get("n_actions", 1)
        if name == "CaretOffset":  # likewise a property
            return node.get("caret", 0)
        if name in ("CurrentValue", "MinimumValue", "MaximumValue"):
            return node[{"CurrentValue": "cur", "MinimumValue": "min", "MaximumValue": "max"}[name]]
        return node.get("name", "")

    def conn_pid(self, svc):
        return self.pids.get(svc)


ALL_STATES = {8, 24, 25, 30}  # enabled, sensitive, showing, visible


def _tree():
    A = ("app", "/root")
    F = ("app", "/frame")
    SAVE = ("app", "/save")
    CANCEL = ("app", "/cancel")
    LABEL = ("app", "/label")
    nodes = {
        A: {"role": 75, "name": "yad", "children": [F]},
        F: {"role": 23, "name": "dialog", "children": [SAVE, CANCEL, LABEL]},
        SAVE: {"role": 43, "role_name": "button", "name": "Save",
               "extent": (10, 100, 80, 30), "states": ALL_STATES, "children": []},
        CANCEL: {"role": 43, "role_name": "button", "name": "Cancel",
                 "extent": (100, 100, 80, 30), "states": {8, 30}, "children": []},
        LABEL: {"role": 29, "name": "Some label", "extent": (10, 10, 200, 20),
                "states": ALL_STATES, "children": []},
    }
    return FakeBus(nodes), A


def test_find_actionable_only():
    bus, root = _tree()
    els, truncated = a11y.find_elements(bus, *root, actionable=True)
    assert [e["name"] for e in els] == ["Save", "Cancel"]  # label (role 29) excluded
    assert els[0]["extent"] == (10, 100, 80, 30)
    assert truncated is False


def test_a_role_without_a_name_is_named_from_its_number():
    bus, root = _tree()
    bus.nodes[("app", "/save")].update(role=7, role_name=None)
    els = {e["name"]: e for e in a11y.find_elements(bus, *root)[0]}
    assert els["Save"]["role"] == "check box"
    assert els["Cancel"]["role"] == "button"


def test_clickable_reflects_states():
    bus, root = _tree()
    els = {e["name"]: e for e in a11y.find_elements(bus, *root)[0]}
    assert els["Save"]["clickable"] is True
    assert els["Cancel"]["clickable"] is False  # missing showing+sensitive


def test_name_filter_is_case_insensitive_substring():
    bus, root = _tree()
    els, _ = a11y.find_elements(bus, *root, name="canc", actionable=False)
    assert [e["name"] for e in els] == ["Cancel"]


def test_name_filter_finds_nonactionable_too():
    bus, root = _tree()
    els, _ = a11y.find_elements(bus, *root, name="label", actionable=False)
    assert [e["name"] for e in els] == ["Some label"]


def test_name_match_still_excluded_when_role_not_actionable():
    # a label matches the name but is not actionable: with actionable=True
    # it must be dropped, not returned
    bus, root = _tree()
    els, _ = a11y.find_elements(bus, *root, name="label", actionable=True)
    assert els == []


def test_max_results_caps_output():
    bus, root = _tree()
    els, _ = a11y.find_elements(bus, *root, actionable=True, max_results=1)
    assert len(els) == 1


def test_max_nodes_truncation_is_signalled():
    # a linear chain of panels ending in a button, walked with a tiny budget:
    # the button is beyond the frontier, so results empty AND truncated True
    nodes = {}
    for i in range(10):
        child = ("app", f"/n{i + 1}") if i < 9 else ("app", "/btn")
        nodes[("app", f"/n{i}")] = {"role": 39, "name": "", "children": [child]}
    nodes[("app", "/btn")] = {"role": 43, "name": "Deep", "extent": (0, 0, 1, 1),
                              "states": ALL_STATES, "children": []}
    bus = FakeBus(nodes)
    els, truncated = a11y.find_elements(bus, "app", "/n0", actionable=True, max_nodes=3)
    assert els == [] and truncated is True


def _shared_subtree():
    # Qt WebEngine lists one web document under two parents: the same
    # object reached twice
    root, a, b, doc = ("app", "/root"), ("app", "/a"), ("app", "/b"), ("app", "/doc")
    ok, last = ("app", "/ok"), ("app", "/last")
    nodes = {
        root: {"role": 23, "name": "w", "children": [a, b, last]},
        a: {"role": 39, "name": "", "children": [doc], "states": ALL_STATES},
        b: {"role": 39, "name": "", "children": [doc], "states": ALL_STATES},
        doc: {"role": 95, "name": "", "children": [ok], "states": ALL_STATES},
        ok: {"role": 43, "name": "OK", "extent": (0, 0, 9, 9), "states": ALL_STATES,
             "children": []},
        last: {"role": 43, "name": "Last", "extent": (0, 20, 9, 9), "states": ALL_STATES,
               "children": []},
    }
    return FakeBus(nodes), root


def test_an_object_reached_twice_is_walked_once():
    # walking the copy spent the budget twice over: six objects, six visits
    bus, root = _shared_subtree()
    reads = []
    call = bus.call
    bus.call = lambda svc, path, iface, method, *sig: (
        reads.append((path, method)), call(svc, path, iface, method, *sig))[1]
    els, truncated = a11y.find_elements(bus, *root, max_nodes=6)
    assert [e["name"] for e in els] == ["OK", "Last"]
    assert truncated is False
    children = [p for p, m in reads if m == "GetChildren"]
    assert sorted(children) == sorted(set(children))  # the copy is not read again


def test_the_field_walks_visit_each_object_once():
    bus, root = _shared_subtree()
    focused = ("app", "/ok")
    bus.nodes[focused]["states"] = ALL_STATES | {a11y._STATE_FOCUSED}
    bus.nodes[("app", "/last")]["role"] = 79
    bus.nodes[("app", "/last")]["states"] = ALL_STATES | {a11y._STATE_EDITABLE}
    assert a11y.editable_fields(bus, *root, max_nodes=6) == ([("app", "/last", 79)], True)
    assert a11y.focused_role(bus, *root, max_nodes=6) == 43
    bus.nodes[focused]["states"] = ALL_STATES
    bus.nodes[("app", "/last")]["states"] |= {a11y._STATE_FOCUSED}
    assert a11y.focused_text(bus, *root, max_nodes=6) == ("app", "/last", 79)


_ACTION_IFACES = ["org.a11y.atspi.Accessible", "org.a11y.atspi.Action",
                  "org.a11y.atspi.Component"]


def _chat_list(with_button=False):
    # a web app's list row: the row and its cells carry the chat's text as
    # their names, and only one cell has an action (Qt WebEngine: SetFocus)
    root, row, cell, label = ("app", "/root"), ("app", "/row"), ("app", "/cell"), ("app", "/lbl")
    btn = ("app", "/btn")
    nodes = {
        root: {"role": 23, "name": "w", "children": [btn, row] if with_button else [row]},
        row: {"role": 90, "name": "Yasser Hajbi 19:09 hello", "children": [cell],
              "extent": (0, 100, 400, 60), "states": ALL_STATES},
        cell: {"role": 56, "name": "Yasser Hajbi 19:09 hello", "children": [label],
               "extent": (0, 100, 400, 60), "states": ALL_STATES, "ifaces": _ACTION_IFACES,
               "actions": ["SetFocus"]},
        label: {"role": 29, "name": "Yasser Hajbi", "children": [],
                "extent": (60, 100, 100, 20), "states": ALL_STATES},
        btn: {"role": 43, "name": "Call Yasser", "children": [],
              "extent": (300, 0, 40, 40), "states": ALL_STATES},
    }
    return FakeBus(nodes), root


def test_a_named_element_with_an_action_is_found_when_no_control_matches():
    bus, root = _chat_list()
    els, _ = a11y.find_elements(bus, *root, name="yasser hajbi")
    assert [(e["path"], e["action"]) for e in els] == [("/cell", "SetFocus")]


def test_a_listed_control_is_preferred_over_an_element_with_an_action():
    # every name that found a control before finds the same one now
    bus, root = _chat_list(with_button=True)
    els, _ = a11y.find_elements(bus, *root, name="yasser")
    assert [e["path"] for e in els] == ["/btn"]


def test_elements_with_actions_are_not_listed_without_a_name():
    bus, root = _chat_list()
    assert a11y.find_elements(bus, *root)[0] == []


def test_an_element_whose_action_list_is_empty_is_not_offered():
    bus, root = _chat_list()
    bus.nodes[("app", "/cell")]["n_actions"] = 0
    assert a11y.find_elements(bus, *root, name="yasser hajbi")[0] == []


def test_action_name_and_focus_are_read():
    bus, _ = _chat_list()
    cell = ("app", "/cell")
    assert a11y.action_name(bus, *cell) == "SetFocus"
    assert a11y.is_focused(bus, *cell) is False
    bus.nodes[cell]["states"] = ALL_STATES | {a11y._STATE_FOCUSED}
    assert a11y.is_focused(bus, *cell) is True


def test_element_without_component_is_skipped():
    # GetExtents raising (no Component interface) drops the element quietly
    nodes = {
        ("app", "/root"): {"role": 75, "name": "app", "children": [("app", "/b")]},
        ("app", "/b"): {"role": 43, "name": "NoComp", "extent": None,
                        "states": ALL_STATES, "children": []},
    }

    class NoCompBus(FakeBus):
        def call(self, svc, path, iface, method, *sig):
            if method == "GetExtents":
                raise a11y.CallRefused("no Component")
            return super().call(svc, path, iface, method, *sig)

    els, _ = a11y.find_elements(NoCompBus(nodes), "app", "/root", actionable=True)
    assert els == []


def test_window_frame_scopes_multi_window_app():
    # one app, two toplevels; the walk must scope to the requested one
    nodes = {
        ("app", "/root"): {"role": 75, "name": "app", "children": [("app", "/f1"), ("app", "/f2")]},
        ("app", "/f1"): {"role": 23, "name": "Doc A", "extent": (0, 0, 400, 300), "children": []},
        ("app", "/f2"): {"role": 23, "name": "Doc B", "extent": (0, 0, 800, 600), "children": []},
    }
    bus = FakeBus(nodes)
    assert a11y.window_frame(bus, "app", "/root", title="Doc B") == ("app", "/f2")
    assert a11y.window_frame(bus, "app", "/root", size=(400, 300)) == ("app", "/f1")
    # no confident match -> app root (best effort)
    assert a11y.window_frame(bus, "app", "/root", title="Doc C") == ("app", "/root")


def test_window_frame_single_toplevel_uses_root():
    bus, root = _tree()  # app -> one frame
    assert a11y.window_frame(bus, *root, title="anything") == root


def test_app_for_pid_exact_match(monkeypatch):
    bus = FakeBus({("a", "/root"): {"role": 75, "name": "x", "children": []}}, pids={"a": 42})
    monkeypatch.setattr(a11y, "apps", lambda b: [("a", "/root")])
    assert a11y.app_for_pid(bus, 42) == ("a", "/root")
    assert a11y.app_for_pid(bus, 999) is None


def test_app_for_pid_title_fallback(monkeypatch):
    # multi-process app: pid mismatch, but a frame's name matches the title
    nodes = {
        ("a", "/root"): {"role": 75, "name": "app", "children": [("a", "/f")]},
        ("a", "/f"): {"role": 23, "name": "My Window", "children": []},
    }
    bus = FakeBus(nodes, pids={"a": 111})
    monkeypatch.setattr(a11y, "apps", lambda b: [("a", "/root")])
    assert a11y.app_for_pid(bus, 999, title="My Window") == ("a", "/root")
    assert a11y.app_for_pid(bus, 999, title="Other") is None


def test_app_for_pid_skips_childless_same_pid_root(monkeypatch):
    # Qt under QT_QPA_PLATFORMTHEME=gtk3 registers TWO roots under one PID:
    # the GTK bridge (name "qml6", NO children) first, the Qt root ("Qml
    # Runtime") with the real window tree second; a portal root (different
    # PID) sits in the same registry. Taking the first same-PID root made ui
    # report "no actionable elements" for a window full of named buttons.
    gtk, qt, portal = ("gtk", "/root"), ("qt", "/root"), ("portal", "/root")
    frame, btn = ("qt", "/frame"), ("qt", "/btn")
    nodes = {
        gtk: {"role": 75, "name": "qml6", "children": []},
        qt: {"role": 75, "name": "Qml Runtime", "children": [frame]},
        portal: {"role": 75, "name": "xdg-desktop-portal-gtk", "children": []},
        frame: {"role": 23, "name": "QML Window", "children": [btn]},
        btn: {"role": 43, "role_name": "button", "name": "Approve Alpha",
              "extent": (10, 10, 80, 30), "states": ALL_STATES, "children": []},
    }
    bus = FakeBus(nodes, pids={"gtk": 42, "qt": 42, "portal": 7})
    monkeypatch.setattr(a11y, "apps", lambda b: [portal, gtk, qt])
    # without a title the nonempty root wins; with one, the frame name matches
    assert a11y.app_for_pid(bus, 42) == qt
    assert a11y.app_for_pid(bus, 42, title="QML Window") == qt
    els, _ = a11y.find_elements(bus, *a11y.app_for_pid(bus, 42))
    assert [e["name"] for e in els] == ["Approve Alpha"]  # the symptom, gone


def test_app_for_pid_stops_at_the_first_root_that_holds_a_window(monkeypatch):
    # every PID lookup is a busctl subprocess (~8 ms each on a live desktop),
    # so the usual one-root app must not cost a lookup per registered root
    a, b, c = ("a", "/root"), ("b", "/root"), ("c", "/root")
    nodes = {
        a: {"role": 75, "name": "one", "children": [("a", "/f")]},
        ("a", "/f"): {"role": 23, "name": "Window", "children": []},
        b: {"role": 75, "name": "two", "children": []},
        c: {"role": 75, "name": "three", "children": []},
    }
    bus = FakeBus(nodes, pids={"a": 42, "b": 7, "c": 8})
    asked = []
    pid_of = bus.conn_pid
    bus.conn_pid = lambda svc: asked.append(svc) or pid_of(svc)
    monkeypatch.setattr(a11y, "apps", lambda _: [a, b, c])
    assert a11y.app_for_pid(bus, 42) == a
    assert asked == ["a"]


def test_app_for_pid_all_same_pid_roots_childless_keeps_first(monkeypatch):
    # no candidate is distinguishable: keep the first exact-PID root rather
    # than reaching for the title fallback across other processes' roots
    a, b = ("a", "/root"), ("b", "/root")
    nodes = {
        a: {"role": 75, "name": "one", "children": []},
        b: {"role": 75, "name": "two", "children": []},
    }
    bus = FakeBus(nodes, pids={"a": 7, "b": 7})
    monkeypatch.setattr(a11y, "apps", lambda _: [a, b])
    assert a11y.app_for_pid(bus, 7) == a


def test_do_action():
    bus, root = _tree()
    assert a11y.do_action(bus, "app", "/save") is True
    assert bus.nodes[("app", "/save")]["done"] is True


def test_degenerate_and_wild_extents_are_rejected():
    # toolkits report unrendered widgets with zero-size or absurd origins
    # (GTK notebook scroll arrows come back 8x0; offscreen tab pages report
    # origins in the millions). Neither is a real click target.
    nodes = {
        ("a", "/ghost"): {"role": 43, "name": "Next tab", "extent": (-1, 3, 8, 0),
                          "children": []},
        ("a", "/wild"): {"role": 43, "name": "Offscreen", "extent": (139367382, 5, 10, 10),
                         "children": []},
        ("a", "/ok"): {"role": 43, "name": "Real", "extent": (1, 2, 3, 4), "children": []},
    }
    bus = FakeBus(nodes)
    assert a11y._window_extents(bus, "a", "/ghost") is None
    assert a11y._window_extents(bus, "a", "/wild") is None
    assert a11y._window_extents(bus, "a", "/ok") == (1, 2, 3, 4)


def test_clickable_accepts_sensitive_without_enabled():
    # GTK notebook tabs report SENSITIVE+SHOWING+VISIBLE but NOT ENABLED and
    # click fine, so requiring both would falsely mark them unclickable
    assert a11y._clickable_now({24, 25, 30}) is True  # sensitive, no enabled
    assert a11y._clickable_now({8, 25, 30}) is True  # enabled, no sensitive
    assert a11y._clickable_now({24, 30}) is False  # not showing
    assert a11y._clickable_now({25, 30}) is False  # neither sensitive nor enabled


# --- current values (what a control is set to, not just its label) ---


def test_value_of_checkable_comes_free_from_states():
    bus = FakeBus({("a", "/c"): {"role": 7, "name": "Agree", "children": []}})
    assert a11y.element_value(bus, "a", "/c", 7, {4}) == {"checked": True}  # CHECKED
    assert a11y.element_value(bus, "a", "/c", 7, {20}) == {"checked": True}  # PRESSED
    assert a11y.element_value(bus, "a", "/c", 7, set()) == {"checked": False}


def test_value_reads_entry_text():
    nodes = {("a", "/e"): {"role": 79, "name": "", "text": "hello",
                           "ifaces": ["org.a11y.atspi.Text"], "children": []}}
    assert a11y.element_value(FakeBus(nodes), "a", "/e", 79, set()) == {"value": "hello"}


def test_value_truncates_a_long_text_area():
    nodes = {("a", "/e"): {"role": 61, "name": "", "text": "x" * 500,
                           "ifaces": ["org.a11y.atspi.Text"], "children": []}}
    got = a11y.element_value(FakeBus(nodes), "a", "/e", 61, set())["value"]
    assert got.endswith("...") and len(got) == a11y._MAX_TEXT + 3


def test_password_contents_are_never_read():
    # a password field is a legitimate element to report, but its contents
    # must never leave the app
    nodes = {("a", "/p"): {"role": 40, "name": "Password", "text": "hunter2",
                           "ifaces": ["org.a11y.atspi.Text"], "children": []}}
    assert a11y.element_value(FakeBus(nodes), "a", "/p", 40, set()) == {}


def test_value_reads_slider_position_as_percent():
    # raw units are toolkit-specific (PulseAudio uses 0..99957), so a
    # position percentage is what an agent can actually reason about
    nodes = {("a", "/s"): {"role": 51, "name": "", "cur": 65536.0, "min": 0.0, "max": 99957.0,
                           "ifaces": ["org.a11y.atspi.Value"], "children": []}}
    assert a11y.element_value(FakeBus(nodes), "a", "/s", 51, set()) == {
        "value": 65536.0,
        "percent": 66,
    }


def test_value_skips_roles_and_missing_interfaces():
    bus, _ = _tree()
    assert a11y.element_value(bus, "app", "/save", 43, ALL_STATES) == {}  # plain button
    nodes = {("a", "/e"): {"role": 79, "name": "", "ifaces": [], "children": []}}
    assert a11y.element_value(FakeBus(nodes), "a", "/e", 79, set()) == {}  # no Text iface


def test_unnamed_value_bearing_element_is_still_reported():
    # pavucontrol's volume slider has no accessible name; its reading is
    # still worth returning
    nodes = {
        ("a", "/root"): {"role": 75, "name": "app", "children": [("a", "/s"), ("a", "/box")]},
        ("a", "/s"): {"role": 51, "name": "", "cur": 5.0, "min": 0.0, "max": 10.0,
                      "extent": (1, 2, 3, 4), "states": ALL_STATES,
                      "ifaces": ["org.a11y.atspi.Value"], "children": []},
        ("a", "/box"): {"role": 39, "name": "", "extent": (0, 0, 9, 9), "children": []},
    }
    els, _ = a11y.find_elements(FakeBus(nodes), "a", "/root", actionable=False)
    assert [(e["role"], e.get("percent")) for e in els] == [("widget", 50)]  # panel skipped


def test_state_decoding_two_words():
    # bit 30 (visible) in low word, bit 32 in high word
    bus = FakeBus({("a", "/n"): {"role": 43, "name": "b", "extent": (0, 0, 1, 1),
                                 "states": {30, 32}, "children": []}})
    assert a11y._states(bus, "a", "/n") == {30, 32}


# A Qt app under QT_QPA_PLATFORMTHEME=gtk3 shows NATIVE GTK file, color and
# font dialogs: while one is open the GTK bridge root has children too,
# but they are the dialog, not the window being asked about.
@pytest.mark.parametrize("order", ["gtk-first", "qt-first"])
def test_app_for_pid_takes_the_root_that_holds_this_window(monkeypatch, order):
    gtk, qt = ("gtk", "/root"), ("qt", "/root")
    nodes = {
        gtk: {"role": 75, "name": "qml6", "children": [("gtk", "/dialog")]},
        ("gtk", "/dialog"): {"role": 23, "name": "Open File", "extent": (0, 0, 600, 400),
                             "children": []},
        qt: {"role": 75, "name": "Qml Runtime", "children": [("qt", "/main")]},
        ("qt", "/main"): {"role": 23, "name": "Editor", "extent": (0, 0, 1200, 800),
                          "children": []},
    }
    bus = FakeBus(nodes, pids={"gtk": 42, "qt": 42})
    roots = [gtk, qt] if order == "gtk-first" else [qt, gtk]
    monkeypatch.setattr(a11y, "apps", lambda _: roots)
    assert a11y.app_for_pid(bus, 42, title="Editor") == qt
    assert a11y.app_for_pid(bus, 42, title="Open File") == gtk
    # by size when the title does not match (Chrome's frame names never do)
    assert a11y.app_for_pid(bus, 42, title="x", size=(1200, 800)) == qt



def test_has_action_and_text_primitives():
    entry = ("app", "/entry")
    nodes = {entry: {"role": 79, "name": "", "children": [], "text": "ab", "caret": 1,
                     "n_actions": 0, "states": {a11y._STATE_FOCUSED}}}
    bus = FakeBus(nodes)
    assert a11y.has_action(bus, *entry) is False
    assert a11y.grab_focus(bus, *entry) is True
    assert a11y.insert_text(bus, *entry, "XY") is True
    assert a11y.text_of(bus, *entry) == "aXYb"  # inserted at the caret


def test_focused_text_finds_the_focused_field_in_a_hidden_window():
    # a hidden window's subtree may not report SHOWING, so the walk must not
    # depend on it
    root, frame, entry = ("app", "/root"), ("app", "/frame"), ("app", "/entry")
    nodes = {
        root: {"role": 75, "name": "app", "children": [frame], "states": set()},
        frame: {"role": 23, "name": "w", "children": [entry], "states": set()},
        entry: {"role": 79, "name": "", "children": [], "states": {a11y._STATE_FOCUSED}},
    }
    assert a11y.focused_text(FakeBus(nodes), *root) == ("app", "/entry", 79)



def test_an_unnamed_editable_field_is_listed_under_its_label():
    # GTK reports an entry as role text with no name of its own; the label
    # beside it names it through a LABELLED_BY relation (type 2)
    root, field, label, ro = ("app", "/root"), ("app", "/field"), ("app", "/label"), ("app", "/ro")
    nodes = {
        root: {"role": 23, "name": "w", "children": [label, field, ro]},
        label: {"role": 29, "name": "Name", "children": [], "relations": [(1, [field])],
                "extent": (10, 10, 40, 20), "states": ALL_STATES},
        field: {"role": 61, "name": "", "children": [], "relations": [(2, [label])],
                "extent": (60, 10, 200, 20), "states": ALL_STATES | {a11y._STATE_EDITABLE},
                "text": ""},
        ro: {"role": 61, "name": "", "children": [], "extent": (10, 40, 200, 20),
             "states": ALL_STATES, "text": "read only"},
    }
    els, _ = a11y.find_elements(FakeBus(nodes), *root)
    assert [(e["role"], e["name"]) for e in els] == [("widget", "Name")]
    assert els[0]["path"] == "/field"
    # a name search finds it through the label (LABEL_FOR, type 1)
    found, _ = a11y.find_elements(FakeBus(nodes), *root, name="name")
    assert [(e["name"], e["path"]) for e in found] == [("Name", "/field")]



def test_editable_fields_lists_only_editable_text():
    root, a, b, ro = ("app", "/root"), ("app", "/a"), ("app", "/b"), ("app", "/ro")
    ed = ALL_STATES | {a11y._STATE_EDITABLE}
    nodes = {
        root: {"role": 23, "name": "w", "children": [a, ro, b], "states": set()},
        a: {"role": 61, "name": "", "children": [], "states": ed},
        ro: {"role": 61, "name": "", "children": [], "states": ALL_STATES},
        b: {"role": 79, "name": "", "children": [], "states": ed},
    }
    assert a11y.editable_fields(FakeBus(nodes), *root) == (
        [("app", "/a", 61), ("app", "/b", 79)], True)


def test_editable_fields_says_when_it_stopped_short():
    # a window with one field in its first elements and another past the
    # budget must not pass for a window with one field
    root, a, pad, b = ("app", "/root"), ("app", "/a"), ("app", "/pad"), ("app", "/b")
    ed = ALL_STATES | {a11y._STATE_EDITABLE}
    nodes = {
        root: {"role": 23, "name": "w", "children": [a, pad], "states": set()},
        a: {"role": 61, "name": "", "children": [], "states": ed},
        pad: {"role": 39, "name": "", "children": [b], "states": set()},
        b: {"role": 79, "name": "", "children": [], "states": ed},
    }
    assert a11y.editable_fields(FakeBus(nodes), *root, max_nodes=3) == (
        [("app", "/a", 61)], False)



def test_insert_text_passes_its_length_in_bytes():
    entry = ("app", "/entry")
    bus = FakeBus({entry: {"role": 79, "name": "", "children": [], "text": "", "caret": 0}})
    assert a11y.insert_text(bus, *entry, " \u2713 ok")
    assert a11y.text_of(bus, *entry) == " \u2713 ok"  # all of it, not cut at 5 bytes


def test_insert_text_counts_the_length_in_utf16_for_qt():
    # Qt resizes the text to the length in UTF-16 units: a byte count
    # would pad it with garbage characters
    entry = ("qtapp", "/entry")
    bus = FakeBus({entry: {"role": 79, "name": "", "children": [], "text": "", "caret": 0}},
                  toolkits={"qtapp": "Qt"})
    assert a11y.insert_text(bus, *entry, " \u2713 \U0001f600 ok")
    assert a11y.text_of(bus, *entry) == " \u2713 \U0001f600 ok"


# --- which frame is THIS window, for the background paths --------------------


def _two_frames(name_a, name_b, size_a=(400, 300), size_b=(800, 600)):
    return FakeBus({
        ("app", "/root"): {"role": 75, "name": "app", "children": [("app", "/fa"), ("app", "/fb")]},
        ("app", "/fa"): {"role": 23, "name": name_a, "extent": (0, 0, *size_a), "children": []},
        ("app", "/fb"): {"role": 23, "name": name_b, "extent": (0, 0, *size_b), "children": []},
    })


def test_a_unique_title_names_the_frame():
    bus = _two_frames("Home", "Docs")
    assert a11y.window_frame_for(bus, "app", "/root", "Docs", (800, 600), []) == ("app", "/fb")


def test_two_frames_with_the_title_cannot_be_told_apart():
    # a single-instance app: the human's window and the agent's, both 'Home'
    bus = _two_frames("Home", "Home")
    assert a11y.window_frame_for(bus, "app", "/root", "Home", (800, 600), []) is None


def test_a_peer_window_with_the_same_title_proves_nothing_by_title():
    # the agent's frame may carry another name while the human's 'Home'
    # matches the title; only a size no peer shares can tell them apart
    bus = _two_frames("Home", "Other")
    peers = [("Home", (400, 300))]
    assert a11y.window_frame_for(bus, "app", "/root", "Home", (800, 600), peers) == (
        "app", "/fb")
    bus = _two_frames("Home", "Other", size_b=(400, 300))
    assert a11y.window_frame_for(bus, "app", "/root", "Home", (400, 300), peers) is None


def test_a_unique_size_names_the_frame_when_no_peer_shares_it():
    bus = _two_frames("x", "y")
    assert a11y.window_frame_for(bus, "app", "/root", "T", (400, 300), [("U", (800, 600))]) == (
        "app", "/fa")
    # a peer of the same size: its frame could be the one that matches
    assert a11y.window_frame_for(bus, "app", "/root", "T", (400, 300), [("U", (400, 300))]) is None


def test_no_match_is_unsure():
    bus = _two_frames("x", "y")
    assert a11y.window_frame_for(bus, "app", "/root", "T", (10, 10), []) is None


def test_one_frame_and_no_peer_is_this_window():
    bus, root = _tree()  # app -> one frame
    assert a11y.window_frame_for(bus, *root, "anything", (1, 1), []) == root


def test_one_frame_with_a_peer_must_match():
    bus, root = _tree()
    assert a11y.window_frame_for(bus, *root, "anything", (1, 1), [("other", (2, 2))]) is None


def test_a_named_field_its_label_points_at_is_found_once():
    # Qt names a line edit after its buddy label, and the label points at
    # it too: a name search must not report one control twice
    root, field, label = ("app", "/root"), ("app", "/field"), ("app", "/label")
    nodes = {
        root: {"role": 23, "name": "w", "children": [label, field]},
        label: {"role": 29, "name": "Email:", "children": [], "relations": [(1, [field])],
                "extent": (10, 10, 40, 20), "states": ALL_STATES},
        field: {"role": 61, "name": "Email:", "children": [], "relations": [(2, [label])],
                "extent": (60, 10, 200, 20), "states": ALL_STATES | {a11y._STATE_EDITABLE},
                "text": ""},
    }
    for actionable in (True, False):
        found, _ = a11y.find_elements(FakeBus(nodes), *root, name="email",
                                      actionable=actionable)
        assert [e["path"] for e in found].count("/field") == 1


def test_a_field_record_says_it_is_a_field():
    root, field = ("app", "/root"), ("app", "/field")
    nodes = {
        root: {"role": 23, "name": "w", "children": [field]},
        field: {"role": 61, "name": "Name", "children": [], "extent": (60, 10, 200, 20),
                "states": ALL_STATES | {a11y._STATE_EDITABLE}, "text": ""},
    }
    els, _ = a11y.find_elements(FakeBus(nodes), *root)
    assert els[0]["field"] is True


def test_the_effect_calls_refuse_in_a_dry_run(monkeypatch):
    # the second barrier, at the effect boundary, like input and dispatch
    monkeypatch.setenv("HYPRUSE_DRYRUN", "1")
    entry = ("app", "/entry")
    bus = FakeBus({entry: {"role": 79, "name": "", "children": [], "text": "", "caret": 0}})
    for call in (lambda: a11y.do_action(bus, *entry),
                 lambda: a11y.grab_focus(bus, *entry),
                 lambda: a11y.insert_text(bus, *entry, "x")):
        with pytest.raises(Exception, match="(?i)dry"):
            call()
    assert a11y.text_of(bus, *entry) == ""


# --- whose accessibility connection is it -------------------------------------


def _procs(monkeypatch, ppid=None, flatpak=None, cgroup=None):
    monkeypatch.setattr(a11y, "_ppid", lambda pid: (ppid or {}).get(pid))
    monkeypatch.setattr(a11y, "_flatpak_instance", lambda pid: (flatpak or {}).get(pid))
    monkeypatch.setattr(a11y, "_cgroup", lambda pid: (cgroup or {}).get(pid))


def test_the_same_process_is_the_same_app(monkeypatch):
    _procs(monkeypatch)
    assert a11y.same_app(42, 42)


def test_a_parent_or_child_process_is_the_same_app(monkeypatch):
    _procs(monkeypatch, ppid={11: 10, 10: 1, 12: 11})
    assert a11y.same_app(10, 12) and a11y.same_app(12, 10)


def test_one_flatpak_instance_is_the_same_app(monkeypatch):
    # the accessibility bus sees Flatpak's xdg-dbus-proxy, not the app
    _procs(monkeypatch, ppid={5: 1, 6: 1}, flatpak={5: "3619740238", 6: "3619740238"})
    assert a11y.same_app(5, 6)
    _procs(monkeypatch, ppid={5: 1, 6: 1}, flatpak={5: "1", 6: "2"})
    assert not a11y.same_app(5, 6)


def test_one_dedicated_app_scope_is_the_same_app(monkeypatch):
    scope = "0::/user.slice/user@1000.service/app.slice/app-Hyprland-foot@x.scope"
    _procs(monkeypatch, ppid={5: 1, 6: 1}, cgroup={5: scope, 6: scope})
    assert a11y.same_app(5, 6)
    shared = "0::/user.slice/user-1000.slice/session-2.scope"  # every app of the session
    _procs(monkeypatch, ppid={5: 1, 6: 1}, cgroup={5: shared, 6: shared})
    assert not a11y.same_app(5, 6)


def test_unrelated_processes_are_not_the_same_app(monkeypatch):
    _procs(monkeypatch, ppid={5: 1, 6: 1})
    assert not a11y.same_app(5, 6) and not a11y.same_app(None, 6)
