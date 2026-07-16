"""Byte-budget auto-fit for screenshots.

Regression source: Claude Desktop rejects tool results over 1 MB — a
1080p PNG (~900 KB raw, ~1.2 MB as base64) blew the cap on first use.
"""

import pytest

from hypruse import screenshot


class FakeGrim:
    """Returns blobs whose size depends on format/quality/scale args."""

    def __init__(self, sizes):
        self.sizes = sizes  # key: (fmt, scale-string or None)
        self.calls = []

    def __call__(self, args):
        fmt = "jpeg" if "jpeg" in args else "png"
        s = args[args.index("-s") + 1] if "-s" in args else None
        self.calls.append((fmt, s))
        return b"x" * self.sizes[(fmt, s)]


def test_native_png_when_it_fits(monkeypatch):
    fake = FakeGrim({("png", None): 500_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    data, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 0, 700_000)
    assert (fmt, applied, len(data)) == ("png", 1.0, 500_000)
    assert fake.calls == [("png", None)]


def test_degrades_format_before_resolution(monkeypatch):
    fake = FakeGrim({("png", None): 900_000, ("jpeg", None): 330_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    data, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 0, 700_000)
    assert (fmt, applied) == ("jpeg", 1.0)
    assert fake.calls == [("png", None), ("jpeg", None)]


def test_downscales_when_format_is_not_enough(monkeypatch):
    fake = FakeGrim(
        {
            ("png", None): 2_000_000,
            ("jpeg", None): 900_000,
            ("jpeg", "0.75"): 500_000,
        }
    )
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 0, 700_000)
    assert (fmt, applied) == ("jpeg", 0.75)


def test_errors_when_nothing_fits(monkeypatch):
    fake = FakeGrim(
        {
            ("png", None): 9_000_000,
            ("jpeg", None): 8_000_000,
            ("jpeg", "0.75"): 7_000_000,
            ("jpeg", "0.5"): 6_000_000,
        }
    )
    monkeypatch.setattr(screenshot, "_grim", fake)
    with pytest.raises(screenshot.ScreenshotError, match="window or region"):
        screenshot._grab_fitting(["-o", "eDP-1"], 0, 700_000)


def test_no_budget_returns_native(monkeypatch):
    fake = FakeGrim({("png", None): 50_000_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 0, None)
    assert (fmt, applied) == ("png", 1.0)
    assert len(fake.calls) == 1


def test_explicit_scale_is_honored(monkeypatch):
    fake = FakeGrim({("png", "0.5"): 200_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 0.5, 700_000)
    assert (fmt, applied) == ("png", 0.5)
    assert fake.calls == [("png", "0.5")]


def test_capture_folds_applied_scale_into_meta(monkeypatch):
    monkeypatch.setattr(
        screenshot.hyprctl,
        "query",
        lambda cmd: {
            "monitors": [
                {"name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080,
                 "scale": 1.25, "focused": True}
            ]
        }[cmd],
    )
    fake = FakeGrim({("png", None): 2_000_000, ("jpeg", None): 900_000, ("jpeg", "0.75"): 400_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, meta = screenshot.capture(max_bytes=700_000)
    assert meta["format"] == "jpeg"
    assert meta["scale"] == pytest.approx(1.25 * 0.75)


def test_capture_rejects_bad_scale():
    with pytest.raises(screenshot.ScreenshotError, match="out of range"):
        screenshot.capture(scale=3.0)
