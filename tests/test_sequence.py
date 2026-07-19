"""The sequence tool: run ordered actions in one call, stopping (best
effort) when the desktop changes structurally between steps."""

import json

import pytest

from hypruse import server as srv


class FakeStream:
    """Stands in for events.EventStream: yields a scripted list of event
    batches, one per drain() call, then empty batches forever. `waits`
    scripts events for wait_for() calls on the same stream."""

    def __init__(self, batches, waits=None, wait_error=False):
        self.batches = list(batches)
        self.waits = list(waits or [])
        self.wait_error = wait_error
        self.closed = False
        self.drains = 0
        self.wait_calls = []

    def drain(self, settle=0.08):
        self.drains += 1
        return self.batches.pop(0) if self.batches else []

    def wait_for(self, names, matcher, timeout):
        self.wait_calls.append((set(names), timeout))
        if self.wait_error:
            from hypruse import events

            raise events.EventError("socket died mid-wait")
        while self.waits:
            n, p = self.waits.pop(0)
            if n in names and (matcher is None or matcher(n, p)):
                return n, p
        return None

    def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """Record dispatched steps; no real input or compositor."""
    ran = []
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {"snap": True})
    # activeworkspace-shaped for most queries; the layer pre-check asks for
    # "layers", whose real shape is a per-monitor dict (empty = none mapped)
    monkeypatch.setattr(
        srv.hyprctl, "query", lambda cmd: {} if cmd == "layers" else {"name": ""}
    )

    def fake_dispatch(step):
        ran.append(step)
        if step.get("op") == "boom":
            raise RuntimeError("kaboom")
        return f"{step.get('op')} ok"

    monkeypatch.setattr(srv, "_dispatch_step", fake_dispatch)
    return ran


def _head(out):
    return out[0].text if isinstance(out, list) else out


def _stream(monkeypatch, batches, waits=None, wait_error=False):
    stream = FakeStream(batches, waits, wait_error)
    monkeypatch.setattr(srv.events, "EventStream", lambda: stream)
    return stream


def test_runs_all_steps_in_order(wired, monkeypatch):
    _stream(monkeypatch, [])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "keyboard", "action": "type", "text": "hi"},
        {"op": "keyboard", "action": "key", "keys": "enter"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer", "keyboard", "keyboard"]
    assert _head(out).startswith("sequence: all 3/3 steps ran")


def test_stops_on_unexpected_open(wired, monkeypatch):
    # a click pops a dialog (openwindow); the type step must not run blind
    _stream(monkeypatch, [[("openwindow", {"address": "0xnew"})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "keyboard", "action": "type", "text": "into-the-void"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer"]  # second step never ran
    assert "stopped after 1/2" in _head(out) and "openwindow" in _head(out)


def test_focus_and_title_churn_do_not_stop(wired, monkeypatch):
    # a click focuses/retitles a window; that is noise, not structural
    _stream(monkeypatch, [[("activewindow", {"data": "kitty,~"}), ("windowtitlev2", {})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "keyboard", "action": "type", "text": "ok"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer", "keyboard"]
    assert "all 2/2" in _head(out)


def test_expected_workspace_switch_is_excused(wired, monkeypatch):
    # the sequence's own switch to workspace 3 is not a change under it
    _stream(monkeypatch, [[("workspace", {"name": "3"})]])
    steps = [
        {"op": "hypr", "action": "workspace", "workspace": "3"},
        {"op": "pointer", "action": "click", "x": 5, "y": 5},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["hypr", "pointer"]
    assert "all 2/2" in _head(out)


def test_human_switching_to_other_workspace_stops(wired, monkeypatch):
    # payload-aware: a switch to a DIFFERENT workspace than the step's is a
    # real takeover, even though it is the same event type
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "3"})  # our switch landed
    _stream(monkeypatch, [[("workspace", {"name": "5"})]])
    steps = [
        {"op": "hypr", "action": "workspace", "workspace": "3"},
        {"op": "pointer", "action": "click", "x": 5, "y": 5},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["hypr"]  # click never ran
    assert "workspace" in _head(out) and "stopped after 1/2" in _head(out)


def test_relative_workspace_switch_is_excused(wired, monkeypatch):
    # 'workspace +1' emits the RESOLVED name ('4'), which never equals the
    # literal '+1'; the sequence must recognize its own switch and not stop
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "4"})
    _stream(monkeypatch, [[("workspace", {"name": "4"})]])
    steps = [
        {"op": "hypr", "action": "workspace", "workspace": "+1"},
        {"op": "pointer", "action": "click", "x": 5, "y": 5},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["hypr", "pointer"]
    assert "all 2/2" in _head(out)


def test_keyboard_window_on_other_workspace_does_not_abort(wired, monkeypatch):
    # keyboard(window=X) focuses X; if X is on another workspace, focusing
    # switches to it and emits a workspace event. That is the sequence's OWN
    # focus-induced switch, not a takeover: the following enter must still run
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "8"})  # focus landed on ws 8
    _stream(monkeypatch, [[("workspace", {"name": "8"})]])
    steps = [
        {"op": "keyboard", "action": "type", "text": "echo hi", "window": "0xterm"},
        {"op": "keyboard", "action": "key", "keys": "enter"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["keyboard", "keyboard"]  # enter ran
    assert "all 2/2" in _head(out)


def test_click_ui_step_focus_switch_is_excused(wired, monkeypatch):
    # click_ui always focuses its target window; a cross-workspace target's
    # switch must not abort a following step either
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "5"})
    _stream(monkeypatch, [[("workspace", {"name": "5"})]])
    steps = [
        {"op": "click_ui", "name": "Save", "window": "0xapp"},
        {"op": "keyboard", "action": "key", "keys": "enter"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["click_ui", "keyboard"]
    assert "all 2/2" in _head(out)


def test_keyboard_without_window_still_stops_on_workspace_takeover(wired, monkeypatch):
    # the excuse is scoped to focus-capable steps: a plain keyboard step (no
    # window=) cannot switch workspaces, so a workspace event after it IS a
    # human takeover and must still stop the run
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "3"})
    _stream(monkeypatch, [[("workspace", {"name": "9"})]])
    steps = [
        {"op": "keyboard", "action": "type", "text": "hi"},
        {"op": "keyboard", "action": "key", "keys": "enter"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["keyboard"]  # enter did NOT run
    assert "stopped after 1/2" in _head(out) and "workspace" in _head(out)


def test_move_window_expected_by_address(wired, monkeypatch):
    _stream(monkeypatch, [[("movewindow", {"address": "0xA", "workspace": "3"})]])
    steps = [
        {"op": "hypr", "action": "move_window", "target": "0xA", "workspace": "3"},
        {"op": "pointer", "action": "click", "x": 1, "y": 1},
    ]
    srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["hypr", "pointer"]  # own move excused


def test_click_then_wait_is_not_aborted(wired, monkeypatch):
    # click a launcher (opens a window), then wait_for that window: the
    # openwindow is what the next step waits for, not an unexpected change
    stream = _stream(monkeypatch, [[("openwindow", {"address": "0xnew"})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "wait_for", "event": "window_open"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer"]  # wait ran on the stream
    assert "all 2/2" in _head(out)
    # the wait was satisfied by the event the settle-drain consumed: a fast
    # app must not turn the documented click-then-wait pattern into a timeout
    assert "0xnew" in _head(out) and "timeout" not in _head(out)
    assert stream.wait_calls == []  # never had to block: backlog had it


def test_wait_step_blocks_on_the_sequence_stream(wired, monkeypatch):
    # nothing drained yet: the wait must listen on the SAME connection the
    # sequence already holds, not open a fresh one that missed history
    stream = _stream(
        monkeypatch, [[]], waits=[("openwindow", {"address": "0xk", "class": "kitty"})]
    )
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "wait_for", "event": "window_open", "match": "kitty"},
    ]
    out = srv.sequence(steps, then="none")
    assert "all 2/2" in _head(out) and "0xk" in _head(out)
    assert len(stream.wait_calls) == 1 and stream.wait_calls[0][0] == {"openwindow"}


def test_wait_step_never_served_a_stale_event(wired, monkeypatch):
    # [switch to 2, switch to 3, wait for the next switch]: the wait must
    # NOT be handed the excused workspace:2 from two steps ago; with the
    # backlog empty of own-changes it blocks for a genuinely new event
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": ""})
    stream = _stream(
        monkeypatch,
        [[("workspace", {"name": "2"})], [("workspace", {"name": "3"})]],
        waits=[("workspace", {"name": "4"})],
    )
    steps = [
        {"op": "hypr", "action": "workspace", "workspace": "2"},
        {"op": "hypr", "action": "workspace", "workspace": "3"},
        {"op": "wait_for", "event": "workspace"},
    ]
    out = srv.sequence(steps, then="none")
    assert "all 3/3" in _head(out)
    assert "'name': '4'" in _head(out)  # the NEXT switch, not the stale "2"
    assert len(stream.wait_calls) == 1  # it really blocked


def test_wait_step_survives_socket_death_mid_wait(wired, monkeypatch):
    # the event socket dying DURING an in-sequence wait degrades to a note,
    # not a crash, and the sequence still reports its steps
    _stream(monkeypatch, [[]], wait_error=True)
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "wait_for", "event": "window_open"},
    ]
    out = srv.sequence(steps, then="none")
    assert "all 2/2" in _head(out)
    assert "event socket unavailable" in _head(out)


def test_wait_step_without_stream_uses_standalone_dispatch(wired, monkeypatch):
    # with stop_on_change=False there is no sequence stream: a wait step
    # must fall through to the standalone tool (with the budget clamp)
    def boom():
        raise AssertionError("must not open a stream when stop_on_change is False")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    srv.sequence(
        [{"op": "wait_for", "event": "window_open", "timeout_s": 60}],
        stop_on_change=False,
        then="none",
    )
    assert [s["op"] for s in wired] == ["wait_for"]
    assert wired[0]["timeout_s"] <= 30  # clamped to the sequence budget


def test_wait_step_match_filters_backlog(wired, monkeypatch):
    # a drained event that does NOT match the filter is not consumed as a hit
    stream = _stream(monkeypatch, [[("openwindow", {"address": "0xa", "class": "sidebar"})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "wait_for", "event": "window_open", "match": "kitty", "timeout_s": 1},
    ]
    out = srv.sequence(steps, then="none")
    assert "timeout" in _head(out)  # sidebar was not the awaited kitty
    assert len(stream.wait_calls) == 1  # it went on to block for the real one


def test_launcher_layer_opening_stops(wired, monkeypatch):
    # a wofi launcher appearing mid-sequence grabs the keyboard: the type
    # step must not run into it
    _stream(monkeypatch, [[("openlayer", {"namespace": "wofi"})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "keyboard", "action": "type", "text": "into-the-launcher"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer"]
    assert "openlayer" in _head(out) and "stopped after 1/2" in _head(out)


def test_notification_layer_is_noise(wired, monkeypatch):
    # a mako notification popping up is not a takeover; the run continues
    _stream(monkeypatch, [[("openlayer", {"namespace": "notifications"})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "keyboard", "action": "type", "text": "ok"},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer", "keyboard"]
    assert "all 2/2" in _head(out)


def test_click_then_wait_layer_open_is_excused(wired, monkeypatch):
    # click something that pops a launcher, then wait for that layer: the
    # openlayer is what the next step waits for, not an unexpected change
    _stream(monkeypatch, [[("openlayer", {"namespace": "wofi"})]])
    steps = [
        {"op": "pointer", "action": "click", "x": 1, "y": 2},
        {"op": "wait_for", "event": "layer_open", "match": "wofi"},
    ]
    out = srv.sequence(steps, then="none")
    assert "all 2/2" in _head(out) and "wofi" in _head(out)


def test_final_drain_reports_last_step_change(wired, monkeypatch):
    # a change caused by the LAST step is reported, not silently missed
    _stream(monkeypatch, [[("openwindow", {"address": "0xz"})]])
    out = srv.sequence([{"op": "pointer", "action": "click", "x": 1, "y": 1}], then="none")
    assert [s["op"] for s in wired] == ["pointer"]
    assert "after the last step" in _head(out) and "openwindow" in _head(out)


def test_stops_on_step_error(wired, monkeypatch):
    _stream(monkeypatch, [])
    steps = [{"op": "pointer", "action": "move", "x": 1, "y": 1}, {"op": "boom"}, {"op": "hypr"}]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer", "boom"]
    assert "ERROR kaboom" in _head(out) and "step 1 raised" in _head(out)


def test_then_appends_final_snapshot(wired, monkeypatch):
    _stream(monkeypatch, [])
    out = srv.sequence([{"op": "pointer", "action": "move", "x": 1, "y": 1}], then="desktop")
    assert isinstance(out, list)
    assert json.loads(out[1].text) == {"snap": True}


def test_stop_on_change_false_skips_stream(wired, monkeypatch):
    def boom():
        raise AssertionError("must not open a stream when stop_on_change is False")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    srv.sequence(
        [
            {"op": "pointer", "action": "move", "x": 1, "y": 1},
            {"op": "hypr", "action": "workspace"},
        ],
        stop_on_change=False,
        then="none",
    )
    assert len(wired) == 2


def test_degrades_when_event_socket_unavailable(wired, monkeypatch):
    def boom():
        raise srv.events.EventError("no socket")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    out = srv.sequence([{"op": "pointer", "action": "move", "x": 1, "y": 1}], then="none")
    assert len(wired) == 1 and "all 1/1" in _head(out)


def test_time_budget_stops_before_a_late_step(wired, monkeypatch):
    _stream(monkeypatch, [])
    # deadline is set from the first monotonic() reading; make the second
    # step's budget check land past it
    ticks = iter([0.0, 5.0, 100.0])
    monkeypatch.setattr(srv.time, "monotonic", lambda: next(ticks))
    steps = [
        {"op": "pointer", "action": "move", "x": 1, "y": 1},
        {"op": "pointer", "action": "move", "x": 2, "y": 2},
    ]
    out = srv.sequence(steps, then="none")
    assert [s["op"] for s in wired] == ["pointer"]  # second step budget-gated
    assert "time budget" in _head(out)


def test_wait_for_timeout_clamped_to_budget(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "snapshot", lambda: {})
    stream = _stream(monkeypatch, [])
    # 25s already elapsed of a 30s budget -> a 60s wait must clamp to ~5s
    ticks = iter([0.0, 25.0, 25.0])
    monkeypatch.setattr(srv.time, "monotonic", lambda: next(ticks))
    srv.sequence([{"op": "wait_for", "event": "window_open", "timeout_s": 60}], then="none")
    assert len(stream.wait_calls) == 1
    assert stream.wait_calls[0][1] == pytest.approx(5.0)


def test_real_dispatch_wiring(monkeypatch):
    # exercises the REAL _dispatch_step -> real pointer/keyboard, only the
    # input/IPC backends mocked (closes the over-mocked-tests gap)
    calls = {"moved": [], "typed": [], "dispatch": []}
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hinput, "move", lambda x, y: calls["moved"].append((x, y)))
    monkeypatch.setattr(srv.hinput, "type_text", lambda t: calls["typed"].append(t))
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: calls["dispatch"].append(a))
    monkeypatch.setattr(srv.hyprctl, "cursor_pos", lambda: (3, 4))
    _client = {"address": "0xabc", "class": "kitty", "pid": 1, "title": "t",
               "at": [0, 0], "size": [9, 9], "workspace": {"id": 1}}
    monkeypatch.setattr(
        srv.hyprctl, "query",
        lambda cmd: [_client] if cmd == "clients" else _client if cmd == "activewindow" else {},
    )
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)
    _stream(monkeypatch, [])
    steps = [
        {"op": "pointer", "action": "move", "x": 1, "y": 2},
        {"op": "keyboard", "action": "type", "text": "hi", "window": "0xabc"},
    ]
    out = srv.sequence(steps, then="none")
    assert calls["moved"] == [(1, 2)]
    assert calls["typed"] == ["hi"]
    assert ("focuswindow", "address:0xabc") in calls["dispatch"]  # window= focused first
    assert "all 2/2" in _head(out)


def test_empty_and_overlong_rejected(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    with pytest.raises(ValueError, match="at least one step"):
        srv.sequence([])
    with pytest.raises(ValueError, match="too long"):
        srv.sequence([{"op": "pointer"}] * 21)


def test_dispatch_rejects_unknown_op():
    with pytest.raises(ValueError, match="unknown step op"):
        srv._dispatch_step({"op": "teleport"})


def test_dispatch_wraps_bad_args(monkeypatch):
    # a wrong/unknown param becomes a friendly ValueError, not a raw crash
    with pytest.raises(ValueError, match="bad args"):
        srv._dispatch_step({"op": "pointer", "nonsense": 1})


def test_dispatch_strips_then(monkeypatch):
    seen = {}
    monkeypatch.setattr(srv, "_SEQ_HANDLERS", {"x": lambda **kw: seen.update(kw) or "ok"})
    srv._dispatch_step({"op": "x", "action": "go", "then": "desktop"})
    assert "then" not in seen and seen["action"] == "go"


def test_expected_signatures_mapping():
    exp = srv._step_expected_signatures
    assert exp({"op": "hypr", "action": "workspace", "workspace": "3"}) == {"workspace:3"}
    assert exp({"op": "hypr", "action": "move_window", "target": "0xA"}) == {"movewindow:0xA"}
    assert exp({"op": "pointer", "action": "click"}) == set()
    assert srv._step_wait_names({"op": "wait_for", "event": "window_open"}) == {"openwindow"}
    assert srv._step_wait_names({"op": "pointer"}) == set()
    assert srv._event_signature("workspace", {"name": "5"}) == "workspace:5"
