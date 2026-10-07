"""Wait-for-stable capture: a post-action screenshot must not land
mid-animation, and content that never settles must still return."""

from itertools import count

from hypruse import screenshot


def _feed(monkeypatch, frames):
    """capture() returns the next frame each call; records call count."""
    calls = {"n": 0}

    def fake_capture(
        window="", region="", scale=0.0, max_bytes=None, max_edge=None, lossless=False
    ):
        i = min(calls["n"], len(frames) - 1)
        calls["n"] += 1
        return frames[i], {"format": "png", "frame": i}

    monkeypatch.setattr(screenshot, "capture", fake_capture)
    return calls


def test_returns_once_two_frames_match(monkeypatch):
    calls = _feed(monkeypatch, [b"a", b"b", b"b", b"c"])
    data, meta = screenshot.capture_stable(interval=0, timeout=5)
    assert data == b"b"
    assert meta["stable"] is True
    assert calls["n"] == 3  # a, b, b: stopped at the first match


def test_identical_from_the_start_is_two_captures(monkeypatch):
    calls = _feed(monkeypatch, [b"same", b"same"])
    data, meta = screenshot.capture_stable(interval=0, timeout=5)
    assert (data, meta["stable"]) == (b"same", True)
    assert calls["n"] == 2


def test_never_settles_times_out_with_last_frame(monkeypatch):
    # A finite feed can exhaust before the timeout on a fast machine, then
    # repeat its last frame and accidentally satisfy the stability condition.
    frames = count()
    last = {}

    def changing_capture(*args, **kwargs):
        frame = next(frames)
        last["data"] = str(frame).encode()
        last["frame"] = frame
        return last["data"], {"format": "png", "frame": frame}

    monkeypatch.setattr(screenshot, "capture", changing_capture)
    data, meta = screenshot.capture_stable(interval=0, timeout=0.05)
    assert meta["stable"] is False
    assert data == last["data"]
    assert meta["frame"] == last["frame"]
