"""Byte-budget auto-fit for screenshots.

Regression source: Claude Desktop rejects tool results over 1 MB, a
1080p PNG (~900 KB raw, ~1.2 MB as base64) blew the cap on first use.
"""

import pytest

from hypruse import screenshot


class FakeGrim:
    """Returns blobs whose size depends on format/quality/scale args."""

    def __init__(self, sizes):
        self.sizes = sizes  # key: (fmt, quality-or-None, scale-string-or-None)
        self.calls = []

    def __call__(self, args):
        fmt = "jpeg" if "jpeg" in args else "png"
        q = int(args[args.index("-q") + 1]) if "-q" in args else None
        s = args[args.index("-s") + 1] if "-s" in args else None
        self.calls.append((fmt, q, s))
        return b"x" * self.sizes[(fmt, q, s)]


def test_ladder_default_is_jpeg_quality_first():
    rungs = screenshot._fit_ladder(1.0)
    assert rungs[0][0] == "jpeg" and "png" not in [f for f, _, _ in rungs]
    # quality degrades at full resolution before any downscale
    full_res = [q for f, q, s in rungs if abs(s - 1.0) < 1e-9]
    assert full_res == sorted(full_res, reverse=True)
    first_downscale = next(i for i, (_, _, s) in enumerate(rungs) if s < 1.0)
    assert first_downscale == len(full_res)  # every full-res rung precedes any downscale


def test_ladder_lossless_leads_with_png():
    rungs = screenshot._fit_ladder(1.0, lossless=True)
    assert rungs[0] == ("png", None, 1.0)
    assert any(f == "jpeg" for f, _, _ in rungs[1:])  # jpeg fallback under budget


def test_default_jpeg_when_it_fits(monkeypatch):
    fake = FakeGrim({("jpeg", 90, None): 400_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    data, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 1.0, 700_000)
    assert (fmt, applied, len(data)) == ("jpeg", 1.0, 400_000)
    assert fake.calls == [("jpeg", 90, None)]


def test_lossless_returns_png_when_it_fits(monkeypatch):
    fake = FakeGrim({("png", None, None): 500_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 1.0, 700_000, lossless=True)
    assert (fmt, applied) == ("png", 1.0)
    assert fake.calls == [("png", None, None)]


def test_degrades_quality_before_resolution(monkeypatch):
    # q90 too big, q75 fits; must not have touched -s
    fake = FakeGrim({("jpeg", 90, None): 900_000, ("jpeg", 75, None): 300_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 1.0, 700_000)
    assert (fmt, applied) == ("jpeg", 1.0)
    assert fake.calls == [("jpeg", 90, None), ("jpeg", 75, None)]


def test_downscales_only_after_quality_exhausted(monkeypatch):
    fake = FakeGrim(
        {
            ("jpeg", 90, None): 2_000_000,
            ("jpeg", 75, None): 1_500_000,
            ("jpeg", 60, None): 1_200_000,
            ("jpeg", 45, None): 900_000,
            ("jpeg", 45, "0.75"): 500_000,
        }
    )
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 1.0, 700_000)
    assert (fmt, applied) == ("jpeg", 0.75)
    # every full-res quality rung was tried before the first downscale
    assert [c[2] for c in fake.calls[:4]] == [None, None, None, None]


def test_errors_when_nothing_fits(monkeypatch):
    fake = FakeGrim(
        {
            ("jpeg", 90, None): 9_000_000,
            ("jpeg", 75, None): 9_000_000,
            ("jpeg", 60, None): 9_000_000,
            ("jpeg", 45, None): 9_000_000,
            ("jpeg", 45, "0.75"): 8_000_000,
            ("jpeg", 40, "0.5"): 7_000_000,
        }
    )
    monkeypatch.setattr(screenshot, "_grim", fake)
    with pytest.raises(screenshot.ScreenshotError, match="window or region"):
        screenshot._grab_fitting(["-o", "eDP-1"], 1.0, 700_000)


def test_no_budget_returns_default_jpeg(monkeypatch):
    fake = FakeGrim({("jpeg", 90, None): 50_000_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 1.0, None)
    assert (fmt, applied) == ("jpeg", 1.0)
    assert len(fake.calls) == 1  # first rung, no budget to exceed


def test_explicit_scale_is_honored(monkeypatch):
    fake = FakeGrim({("jpeg", 90, "0.5"): 200_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    _, fmt, applied = screenshot._grab_fitting(["-o", "eDP-1"], 0.5, 700_000)
    assert (fmt, applied) == ("jpeg", 0.5)
    assert fake.calls == [("jpeg", 90, "0.5")]


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
    # every full-res jpeg quality rung is over budget, so it falls to the
    # 0.75-of-native rung; grim's -s is ABSOLUTE, so the flag value is
    # base_scale * 0.75 and the image is logical(1536x864) * 0.9375
    fake = FakeGrim(
        {
            ("jpeg", 90, None): 2_000_000,
            ("jpeg", 75, None): 1_500_000,
            ("jpeg", 60, None): 1_100_000,
            ("jpeg", 45, None): 900_000,
            ("jpeg", 45, "0.9375"): 400_000,
        }
    )
    monkeypatch.setattr(screenshot, "_grim", fake)
    monkeypatch.setattr(screenshot, "image_size", lambda data: (1440, 810))
    _, meta = screenshot.capture(max_bytes=700_000)
    assert meta["format"] == "jpeg"
    assert meta["scale"] == pytest.approx(1.25 * 0.75)
    assert meta["image"] == [1440, 810]
    # the round trip the metadata promises: image right edge -> logical width
    assert 1440 / meta["scale"] == pytest.approx(1920 / 1.25)


def test_hidpi_max_edge_cap_passes_absolute_scale_to_grim(monkeypatch):
    # 4K @ 2.0 (logical 1920x1080), image-mode edge cap 1920: the fraction
    # of native is 0.5, but grim needs the ABSOLUTE factor 2.0*0.5 = 1.
    # Passing the bare 0.5 (the old bug) would return a 960px-wide image
    # while metadata claimed scale 1.0, halving every mapped coordinate.
    monkeypatch.setattr(
        screenshot.hyprctl,
        "query",
        lambda cmd: {
            "monitors": [
                {"name": "DP-1", "x": 0, "y": 0, "width": 3840, "height": 2160,
                 "scale": 2.0, "focused": True}
            ]
        }[cmd],
    )
    fake = FakeGrim({("jpeg", 90, "1"): 300_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    monkeypatch.setattr(screenshot, "image_size", lambda data: (1920, 1080))
    _, meta = screenshot.capture(max_bytes=700_000, max_edge=1920)
    assert fake.calls == [("jpeg", 90, "1")]
    assert meta["scale"] == pytest.approx(1.0)
    assert meta["image"] == [1920, 1080]
    # right edge of the image maps to the monitor's logical right edge
    assert meta["geometry"][0] + 1920 / meta["scale"] == pytest.approx(1920)


def test_cross_seam_region_reports_grims_actual_scale(monkeypatch):
    # region straddling a 1.0/2.0 seam, top-left on the 1.0 side: grim
    # renders it at 2x (max intersected output scale), so meta scale must
    # be 2.0 even though no -s was passed (file mode, no caps)
    monkeypatch.setattr(
        screenshot.hyprctl,
        "query",
        lambda cmd: {
            "monitors": [
                {"name": "a", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
                {"name": "b", "x": 1920, "y": 0, "width": 3840, "height": 2160, "scale": 2.0},
            ]
        }[cmd],
    )
    fake = FakeGrim({("jpeg", 90, None): 100_000})
    monkeypatch.setattr(screenshot, "_grim", fake)
    monkeypatch.setattr(screenshot, "image_size", lambda data: (400, 200))
    _, meta = screenshot.capture(region="1900,0,200x100")
    assert fake.calls == [("jpeg", 90, None)]  # no -s: grim default applies
    assert meta["scale"] == pytest.approx(2.0)
    assert meta["image"] == [400, 200]  # logical 200x100 rendered at 2x


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
