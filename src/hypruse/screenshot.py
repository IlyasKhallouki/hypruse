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
from typing import Any

from hypruse import hyprctl


class ScreenshotError(RuntimeError):
    """grim failed or the capture target does not exist."""


_REGION = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*[, ]\s*(\d+)\s*x\s*(\d+)\s*$")


def parse_region(region: str) -> tuple[int, int, int, int]:
    """Accepts 'x,y,WxH' or 'x,y WxH' (grim's own format)."""
    m = _REGION.match(region)
    if not m:
        raise ScreenshotError(f"bad region {region!r}, expected 'x,y,WxH'")
    x, y, w, h = map(int, m.groups())
    if w <= 0 or h <= 0:
        raise ScreenshotError(f"bad region {region!r}: empty size")
    return x, y, w, h


def _grim(args: list[str]) -> bytes:
    if shutil.which("grim") is None:
        raise ScreenshotError("grim not found — install grim for screenshots")
    proc = subprocess.run(["grim", *args, "-"], capture_output=True, timeout=10)
    if proc.returncode != 0 or not proc.stdout:
        raise ScreenshotError(f"grim failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _fit_ladder(explicit_scale: float) -> list[tuple[str, int | None, float]]:
    """(format, jpeg-quality, scale) attempts, best fidelity first.

    Full-resolution JPEG beats half-resolution PNG for reading UI text, so
    format degrades before resolution does.
    """
    if explicit_scale:
        s = explicit_scale
        return [("png", None, s), ("jpeg", 85, s), ("jpeg", 80, s * 0.75)]
    return [("png", None, 1.0), ("jpeg", 85, 1.0), ("jpeg", 85, 0.75), ("jpeg", 80, 0.5)]


def _grab_fitting(
    base_args: list[str], explicit_scale: float, max_bytes: int | None
) -> tuple[bytes, str, float]:
    """Capture within a byte budget; returns (data, format, applied_scale)."""
    for fmt, quality, s in _fit_ladder(explicit_scale):
        args = list(base_args)
        if s != 1.0:
            args = ["-s", f"{s:g}", *args]
        if fmt == "jpeg":
            args = ["-t", "jpeg", "-q", str(quality), *args]
        data = _grim(args)
        if max_bytes is None or len(data) <= max_bytes:
            return data, fmt, s
    raise ScreenshotError(
        f"even a downscaled JPEG exceeds the {max_bytes}-byte result budget — "
        "capture a window or region instead of the whole monitor"
    )


def _scale_at(x: int, y: int, monitors: list[dict[str, Any]]) -> float:
    for m in monitors:
        if m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]:
            return float(m.get("scale", 1.0))
    return 1.0


def _find_window(window: str, clients: list[dict[str, Any]], active: str | None) -> dict[str, Any]:
    target = active if window == "active" else window
    if not target:
        raise ScreenshotError("no active window")
    for c in clients:
        if c.get("address") == target:
            return c
    raise ScreenshotError(
        f"window {target!r} not found — call desktop() for current addresses"
    )


def capture(
    window: str = "",
    region: str = "",
    scale: float = 0.0,
    max_bytes: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Returns (image_bytes, metadata). Exactly one of window/region, or
    neither for the focused monitor. `scale` (0 = auto) forces a capture
    scale; `max_bytes` fits the result into a transport budget by degrading
    format before resolution. The applied scale is folded into metadata so
    the pixel→global mapping stays exact."""
    if window and region:
        raise ScreenshotError("pass window OR region, not both")
    if scale and not 0.1 <= scale <= 1.0:
        raise ScreenshotError(f"scale {scale} out of range (0.1–1.0, or 0 for auto)")

    monitors = hyprctl.query("monitors")

    if region:
        x, y, w, h = parse_region(region)
        base = ["-g", f"{x},{y} {w}x{h}"]
        meta: dict[str, Any] = {"target": "region", "geometry": [x, y, w, h]}
        base_scale = _scale_at(x, y, monitors)
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
        base_scale = _scale_at(x, y, monitors)
    else:
        m = next((m for m in monitors if m.get("focused")), monitors[0])
        base = ["-o", m["name"]]
        meta = {
            "target": "monitor",
            "monitor": m["name"],
            "geometry": [m["x"], m["y"], m["width"], m["height"]],
        }
        base_scale = float(m.get("scale", 1.0))

    data, fmt, applied = _grab_fitting(base, scale, max_bytes)
    meta["format"] = fmt
    meta["scale"] = base_scale * applied
    meta["coords"] = "global = geometry[:2] + image_pixel / scale"
    return data, meta
