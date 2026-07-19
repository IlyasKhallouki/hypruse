"""hypr close_window: the destroy is asynchronous (closewindow only ASKS
the app), so the tool must wait for the compositor's word before the
`then` observation, or that observation still lists the closed window."""

from hypruse import server as srv


class FakeStream:
    def __init__(self, hit, order):
        self.hit = hit
        self.order = order
        order.append("subscribe")

    def wait_for(self, names, matcher, timeout):
        self.order.append("wait")
        self.names, self.matcher, self.timeout = names, matcher, timeout
        return self.hit

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _wired(monkeypatch, order, hit):
    streams = []
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(
        srv.events, "EventStream",
        lambda: streams.append(FakeStream(hit, order)) or streams[-1],
    )
    monkeypatch.setattr(
        srv.hyprctl, "dispatch", lambda *a: order.append(("dispatch", a))
    )
    return streams


def test_close_window_subscribes_then_dispatches_then_waits(monkeypatch):
    order = []
    _wired(monkeypatch, order, hit=("closewindow", {"address": "0xdead"}))
    out = srv.hypr("close_window", target="0xdead")
    assert out == "closed 0xdead"
    # subscribe BEFORE dispatch, so a fast destroy cannot slip past
    assert order == ["subscribe", ("dispatch", ("closewindow", "address:0xdead")), "wait"]


def test_close_window_matcher_is_address_exact(monkeypatch):
    streams = _wired(monkeypatch, [], hit=("closewindow", {"address": "0xdead"}))
    srv.hypr("close_window", target="0xDEAD")  # address compare is case-blind
    s = streams[-1]
    assert s.names == {"closewindow"}
    assert s.matcher("closewindow", {"address": "0xdead"})
    assert not s.matcher("closewindow", {"address": "0xdea"})
    assert not s.matcher("closewindow", {"address": "0xother"})


def test_close_window_observation_comes_after_the_wait(monkeypatch):
    order = []
    _wired(monkeypatch, order, hit=("closewindow", {"address": "0xdead"}))
    monkeypatch.setattr(
        srv.hyprctl, "snapshot", lambda: order.append("snapshot") or {"windows": []}
    )
    srv.hypr("close_window", target="0xdead", then="desktop")
    assert order.index("wait") < order.index("snapshot")


def test_close_window_reports_a_window_that_would_not_close(monkeypatch):
    order = []
    _wired(monkeypatch, order, hit=None)  # no closewindow within the bound
    out = srv.hypr("close_window", target="0xdead")
    assert "still open" in out and "0xdead" in out


def test_close_window_without_socket_fires_and_forgets(monkeypatch):
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    dispatched = []

    def no_socket():
        raise srv.events.EventError("no socket")

    monkeypatch.setattr(srv.events, "EventStream", no_socket)
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: dispatched.append(a))
    out = srv.hypr("close_window", target="0xdead")
    assert out == "asked 0xdead to close"
    assert dispatched == [("closewindow", "address:0xdead")]
