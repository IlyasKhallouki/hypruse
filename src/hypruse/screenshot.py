"""Screenshots via grim (wlroots screencopy).

grim captures in *pixel* space while hypruse coordinates are global
*logical* pixels; on monitors with fractional scaling the two differ.
Every capture therefore returns metadata with its origin and scale so an
image pixel maps back to a clickable point:

    global_x = geometry[0] + pixel_x / scale
    global_y = geometry[1] + pixel_y / scale
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Any

from hypruse import hyprctl


class ScreenshotError(RuntimeError):
    """grim failed or the capture target does not exist."""


_REGION = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*[, ]\s*(\d+)\s*x\s*(\d+)\s*$")

_SIZE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$")

DEFAULT_ZOOM_SIZE = "480x360"


def parse_region(region: str) -> tuple[int, int, int, int]:
    """Accepts 'x,y,WxH' or 'x,y WxH' (grim's own format)."""
    m = _REGION.match(region)
    if not m:
        raise ScreenshotError(f"bad region {region!r}, expected 'x,y,WxH'")
    x, y, w, h = map(int, m.groups())
    if w <= 0 or h <= 0:
        raise ScreenshotError(f"bad region {region!r}: empty size")
    return x, y, w, h


def parse_size(size: str) -> tuple[int, int]:
    """Accepts 'WxH'."""
    m = _SIZE.match(size)
    if not m:
        raise ScreenshotError(f"bad size {size!r}, expected 'WxH'")
    w, h = map(int, m.groups())
    if w <= 0 or h <= 0:
        raise ScreenshotError(f"bad size {size!r}: empty size")
    return w, h


def clamp_box(
    x: float, y: float, w: int, h: int, bounds: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Center a w x h box on (x, y), slid fully inside bounds and shrunk
    only when the box is larger than the bounds themselves."""
    bx, by, bw, bh = bounds
    w, h = min(w, bw), min(h, bh)
    x0 = min(max(round(x - w / 2), bx), bx + bw - w)
    y0 = min(max(round(y - h / 2), by), by + bh - h)
    return x0, y0, w, h


def zoom_region(x: float, y: float, size: str = "", window: str = "") -> tuple[int, int, int, int]:
    """The capture box for zooming at a point of interest: `size` (default
    DEFAULT_ZOOM_SIZE) centered on (x, y) in global logical pixels, clamped
    inside the window (when given) or the monitor containing the point,
    falling back to the focused monitor for an off-layout estimate."""
    w, h = parse_size(size or DEFAULT_ZOOM_SIZE)
    if window:
        active = (hyprctl.query("activewindow") or {}).get("address")
        c = _find_window(window, hyprctl.query("clients"), active)
        (bx, by), (bw, bh) = c["at"], c["size"]
    else:
        monitors = hyprctl.query("monitors")
        m = hyprctl.monitor_at(monitors, x, y) or next(
            (m for m in monitors if m.get("focused")), monitors[0]
        )
        bx, by, bw, bh = hyprctl.logical_rect(m)
    return clamp_box(x, y, w, h, (bx, by, bw, bh))


def _grim(args: list[str]) -> bytes:
    if shutil.which("grim") is None:
        raise ScreenshotError("grim not found, install grim for screenshots")
    proc = subprocess.run(["grim", *args, "-"], capture_output=True, timeout=10)
    if proc.returncode != 0 or not proc.stdout:
        raise ScreenshotError(f"grim failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _fit_ladder(start_scale: float, lossless: bool = False) -> list[tuple[str, int | None, float]]:
    """(format, jpeg-quality, scale) attempts, best fidelity first, for
    fitting a byte budget.

    The default is full-resolution JPEG. grim's PNG path is dominated by
    zlib and is ~13x slower to encode than JPEG on a 1080p frame (measured
    ~640 ms vs ~50 ms), while full-res q90 reads UI text well, so quality
    degrades before resolution ever does. Resolution is the last resort
    because grim's downscale filter is a convolution that is slower than a
    full-res capture. `lossless` leads with PNG for callers that need exact
    pixels, degrading to JPEG only if PNG blows the budget.
    """
    s = start_scale
    jpeg = [
        ("jpeg", 90, s),
        ("jpeg", 75, s),
        ("jpeg", 60, s),
        ("jpeg", 45, s),
        ("jpeg", 45, s * 0.75),
        ("jpeg", 40, s * 0.5),
    ]
    return [("png", None, s), *jpeg] if lossless else jpeg


def _grab_fitting(
    base_args: list[str],
    start_scale: float,
    max_bytes: int | None,
    lossless: bool = False,
    base_scale: float = 1.0,
) -> tuple[bytes, str, float]:
    """Capture within a byte budget; returns (data, format, applied_scale).

    `start_scale` and the returned applied scale are fractions of the
    capture's NATIVE pixels, but grim's `-s` flag is an ABSOLUTE
    logical-to-pixel factor (grim's default equals the output scale, so
    image = logical_size * factor). The flag therefore carries
    base_scale * s; passing the bare fraction would shrink the image by
    base_scale twice on any scaled monitor and desync the metadata."""
    for fmt, quality, s in _fit_ladder(start_scale, lossless):
        args = list(base_args)
        if abs(s - 1.0) > 1e-6:
            args = ["-s", f"{base_scale * s:g}", *args]
        if fmt == "jpeg":
            args = ["-t", "jpeg", "-q", str(quality), *args]
        data = _grim(args)
        if max_bytes is None or len(data) <= max_bytes:
            return data, fmt, s
    raise ScreenshotError(
        f"even a downscaled JPEG exceeds the {max_bytes}-byte result budget; "
        "capture a window or region instead of the whole monitor"
    )


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_size(data: bytes) -> tuple[int, int]:
    """(width, height) of PNG or JPEG bytes, without a decode library.

    The model reasons about the image it is actually shown, so hypruse
    reports the true output dimensions rather than assuming the geometry
    it asked grim for.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    if data[:2] == b"\xff\xd8":  # JPEG: find a Start-Of-Frame segment
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in _SOF_MARKERS:
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                w = int.from_bytes(data[i + 7 : i + 9], "big")
                return w, h
            i += 2 + seg
    raise ScreenshotError("could not read image dimensions")


def _cap_scale(physical_long_edge: float, max_edge: int | None) -> float:
    """Downscale factor so the output's long edge fits max_edge, capped at 1.0.

    Prevents the API from silently downscaling a too-large image *under* the
    model, which would desync the reported scale from what the model sees.
    """
    if not max_edge or physical_long_edge <= max_edge:
        return 1.0
    return max_edge / physical_long_edge


def _rect_intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _scale_for_rect(x: int, y: int, w: int, h: int, monitors: list[dict[str, Any]]) -> float:
    """The buffer scale grim renders a `-g` rect at: the GREATEST scale
    among the outputs the rect intersects. Using the scale under one corner
    would misreport any capture straddling a seam between differently
    scaled monitors, doubling (or scaling) every mapped coordinate."""
    scales = [
        float(m.get("scale", 1.0))
        for m in monitors
        if _rect_intersects(hyprctl.logical_rect(m), (x, y, w, h))
    ]
    return max(scales, default=1.0)


def _find_window(window: str, clients: list[dict[str, Any]], active: str | None) -> dict[str, Any]:
    target = active if window == "active" else window
    if not target:
        raise ScreenshotError("no active window")
    for c in clients:
        if c.get("address") == target:
            return c
    raise ScreenshotError(
        f"window {target!r} not found, call desktop() for current addresses"
    )


def capture(
    window: str = "",
    region: str = "",
    scale: float = 0.0,
    max_bytes: int | None = None,
    max_edge: int | None = None,
    lossless: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Returns (image_bytes, metadata). Exactly one of window/region, or
    neither for the focused monitor. `scale` (0 = auto) forces a capture
    scale; `max_edge` caps the output's long edge (so the API never
    downscales it under the model); `max_bytes` fits a transport budget by
    degrading format before resolution. The default format is JPEG q90
    (fast, small, reads UI text well); `lossless` leads with PNG instead.
    Any applied downscale is folded into the metadata `scale`, so the
    pixel→global mapping stays exact."""
    if window and region:
        raise ScreenshotError("pass window OR region, not both")
    if scale and not 0.1 <= scale <= 1.0:
        raise ScreenshotError(f"scale {scale} out of range (0.1-1.0, or 0 for auto)")

    monitors = hyprctl.query("monitors")

    if region:
        x, y, w, h = parse_region(region)
        base = ["-g", f"{x},{y} {w}x{h}"]
        meta: dict[str, Any] = {"target": "region", "geometry": [x, y, w, h]}
        base_scale = _scale_for_rect(x, y, w, h, monitors)
        physical_long = max(w, h) * base_scale
    elif window:
        active = (hyprctl.query("activewindow") or {}).get("address")
        c = _find_window(window, hyprctl.query("clients"), active)
        (x, y), (w, h) = c["at"], c["size"]
        base = ["-g", f"{x},{y} {w}x{h}"]
        meta = {
            "target": "window",
            "window": c["address"],
            "class": c.get("class", ""),
            "geometry": [x, y, w, h],
        }
        base_scale = _scale_for_rect(x, y, w, h, monitors)
        physical_long = max(w, h) * base_scale
    else:
        m = next((m for m in monitors if m.get("focused")), monitors[0])
        base = ["-o", m["name"]]
        # geometry in logical coords like everything else; grim captures the
        # physical output, so the physical long edge drives the byte-budget cap
        meta = {
            "target": "monitor",
            "monitor": m["name"],
            "geometry": list(hyprctl.logical_rect(m)),
        }
        base_scale = float(m.get("scale", 1.0))
        physical_long = max(m["width"], m["height"])

    # explicit scale wins; otherwise fit the long edge to keep the mapping honest
    start_scale = scale or _cap_scale(physical_long, max_edge)
    data, fmt, applied = _grab_fitting(base, start_scale, max_bytes, lossless, base_scale)
    iw, ih = image_size(data)
    meta["image"] = [iw, ih]
    meta["format"] = fmt
    meta["scale"] = round(base_scale * applied, 6)
    meta["coords"] = "click a target at global = geometry[:2] + image_pixel / scale"
    return data, meta


def capture_stable(
    window: str = "",
    region: str = "",
    scale: float = 0.0,
    max_bytes: int | None = None,
    max_edge: int | None = None,
    lossless: bool = False,
    interval: float = 0.15,
    timeout: float = 2.0,
) -> tuple[bytes, dict[str, Any]]:
    """Capture until two consecutive frames are byte-identical, so a
    screenshot taken right after an action does not land mid-animation.
    Returns the settled frame with meta['stable'] = True, or the last
    frame with False when the content never settles within `timeout`
    (blinking cursors, video). The byte-compare relies on the encoder being
    deterministic: both grim JPEG and PNG map identical pixels to identical
    bytes, so the default JPEG format is safe here."""
    data, meta = capture(window, region, scale, max_bytes, max_edge, lossless)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        nxt, nmeta = capture(window, region, scale, max_bytes, max_edge, lossless)
        if nxt == data:
            nmeta["stable"] = True
            return nxt, nmeta
        data, meta = nxt, nmeta
    meta["stable"] = False
    return data, meta
