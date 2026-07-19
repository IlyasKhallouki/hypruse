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
