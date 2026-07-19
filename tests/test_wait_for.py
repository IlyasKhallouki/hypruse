"""wait_for: level-triggered pre-checks vs edge-triggered waits."""

from hypruse import server as srv


class OneShotStream:
    def __init__(self, hit):
        self.hit = hit
        self.calls = []

    def wait_for(self, names, matcher, timeout):
        self.calls.append((set(names), timeout))
        return self.hit

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def test_unfiltered_workspace_wait_blocks_for_next_switch(monkeypatch):
    # 'the next workspace switch, whatever it is' must not be answered
    # instantly with the CURRENT workspace: that wait would never wait
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "3"})
    stream = OneShotStream(("workspace", {"name": "4"}))
    monkeypatch.setattr(srv.events, "EventStream", lambda: stream)
    out = srv.wait_for("workspace")
    assert out == {"event": "workspace", "name": "4"}
    assert len(stream.calls) == 1  # it actually subscribed and waited


def test_filtered_workspace_wait_already_active(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {"name": "mail"})

    def boom():
        raise AssertionError("must not subscribe when already satisfied")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    out = srv.wait_for("workspace", match="mail")
    assert out.get("already") is True


def test_window_close_already_gone(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: [])

    def boom():
        raise AssertionError("must not subscribe when already satisfied")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    out = srv.wait_for("window_close", match="gedit")
    assert out.get("already") is True


LAYERS_RAW = {
    "DP-1": {"levels": {"3": [{"namespace": "rofi", "x": 560, "y": 300, "w": 800, "h": 480}]}}
}


def test_layer_open_already_mapped(monkeypatch):
    # the classic late subscribe: use_bind returns AFTER the launcher maps,
    # so openlayer fired before wait_for could connect; the mapped surface
    # must satisfy the wait instead of timing out forever
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: LAYERS_RAW)

    def boom():
        raise AssertionError("must not subscribe when already satisfied")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    out = srv.wait_for("layer_open", match="rofi")
    assert out == {"event": "openlayer", "namespace": "rofi", "already": True}


def test_layer_open_not_yet_mapped_subscribes(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {})
    stream = OneShotStream(("openlayer", {"namespace": "rofi"}))
    monkeypatch.setattr(srv.events, "EventStream", lambda: stream)
    out = srv.wait_for("layer_open", match="rofi")
    assert out == {"event": "openlayer", "namespace": "rofi"}
    assert len(stream.calls) == 1  # it actually subscribed and waited


def test_layer_close_already_gone(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: {})

    def boom():
        raise AssertionError("must not subscribe when already satisfied")

    monkeypatch.setattr(srv.events, "EventStream", boom)
    out = srv.wait_for("layer_close", match="rofi")
    assert out.get("already") is True


def test_unfiltered_layer_open_still_waits(monkeypatch):
    # some layer (a bar) is always mapped: an unfiltered wait means 'the
    # NEXT layer, whatever it is' and must not answer from current state
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv.hyprctl, "query", lambda cmd: LAYERS_RAW)
    stream = OneShotStream(("openlayer", {"namespace": "mako"}))
    monkeypatch.setattr(srv.events, "EventStream", lambda: stream)
    out = srv.wait_for("layer_open")
    assert out == {"event": "openlayer", "namespace": "mako"}
    assert len(stream.calls) == 1


def test_title_change_waits_on_v2_only(monkeypatch):
    # plain windowtitle carries only an address, so it can never satisfy a
    # title match; it must not be in the wait set at all
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    stream = OneShotStream(("windowtitlev2", {"address": "0xa", "title": "Inbox (3)"}))
    monkeypatch.setattr(srv.events, "EventStream", lambda: stream)
    out = srv.wait_for("title_change", match="inbox")
    assert out["title"] == "Inbox (3)"
    assert stream.calls[0][0] == {"windowtitlev2"}
