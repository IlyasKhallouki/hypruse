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


def test_image_size_png():
    # 4-byte-length IHDR width/height at offsets 16 and 20
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4 + b"IHDR"
    png += (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
    assert screenshot.image_size(png) == (1920, 1080)


def test_image_size_jpeg_sof0():
    jpeg = b"\xff\xd8"
    jpeg += b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9  # APP0
    jpeg += b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
    jpeg += (720).to_bytes(2, "big") + (1280).to_bytes(2, "big") + b"\x03" + b"\x00" * 6
    assert screenshot.image_size(jpeg) == (1280, 720)


def test_image_size_rejects_unknown():
    with pytest.raises(screenshot.ScreenshotError, match="dimensions"):
        screenshot.image_size(b"GIF89a not an image we emit")


@pytest.mark.parametrize(
    "long_edge,max_edge,expected",
    [
        (1920, 1568, 1568 / 1920),  # over → downscale
        (1366, 1568, 1.0),  # under → untouched
        (1568, 1568, 1.0),  # exactly at limit → untouched
        (4000, None, 1.0),  # no cap → untouched
        (4000, 0, 1.0),  # zero cap → untouched
    ],
)
def test_cap_scale(long_edge, max_edge, expected):
    assert screenshot._cap_scale(long_edge, max_edge) == pytest.approx(expected)
