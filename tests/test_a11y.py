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
    `pids` maps svc -> connection pid."""

    def __init__(self, nodes, pids=None):
        self.nodes = nodes
        self.pids = pids or {}

    def call(self, svc, path, iface, method, *sig):
        node = self.nodes[(svc, path)]
        if method == "GetChildren":
            return [[list(c) for c in node["children"]]]
        if method == "GetRole":
            return [node["role"]]
        if method == "GetRoleName":
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
        raise AssertionError(method)

    def prop(self, svc, path, iface, name):
        return self.nodes[(svc, path)].get("name", "")

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
                raise a11y.A11yError("no Component")
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


def test_do_action():
    bus, root = _tree()
    assert a11y.do_action(bus, "app", "/save") is True
    assert bus.nodes[("app", "/save")]["done"] is True


def test_state_decoding_two_words():
    # bit 30 (visible) in low word, bit 32 in high word
    bus = FakeBus({("a", "/n"): {"role": 43, "name": "b", "extent": (0, 0, 1, 1),
                                 "states": {30, 32}, "children": []}})
    assert a11y._states(bus, "a", "/n") == {30, 32}
