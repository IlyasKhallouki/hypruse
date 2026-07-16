"""hypruse MCP server — computer use for Hyprland.

Design: semantic-first. An agent should read `desktop` and act through
`hypr`/`launch` (IPC, milliseconds, deterministic) and reach for
`screenshot` + `pointer`/`keyboard` only to work *inside* application
windows. All coordinates are Hyprland's global logical layout pixels —
the same space cursorpos, window `at`, and movecursor use.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage

from hypruse import __version__, hyprctl
from hypruse import screenshot as shot

mcp = FastMCP("hypruse")


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    d = Path(base) / "hypruse"
    d.mkdir(parents=True, exist_ok=True)
    return d


@mcp.tool()
def desktop() -> dict[str, Any]:
    """Full semantic state of the Hyprland desktop in one call: monitors,
    workspaces, windows (address, class, title, position `at`, size,
    workspace), the active window and the cursor position.

    Call this first and after actions that change the desktop. Prefer the
    `hypr` and `launch` tools for anything window/workspace-shaped — they are
    instant and exact; use `screenshot` + `pointer`/`keyboard` only to see and
    operate *inside* an application window. Window `at` + `size` tell you
    where a window's pixels live in global coordinates.
    """
    return hyprctl.snapshot()


@mcp.tool()
def screenshot(window: str = "", region: str = "") -> list[Any]:
    """See the screen. With no arguments, captures the focused monitor.
    `window`: "active" or a window address from `desktop` — crops exactly to
    that window (cheaper and sharper for reading one app). `region`:
    "x,y,WxH" in global coordinates, for zooming into small details.

    Returns the image plus a JSON metadata line. The image is in pixel
    space; convert an image pixel to a clickable global coordinate with:
    global = geometry[:2] + pixel / scale (scale is 1.0 unless the monitor
    uses fractional scaling).
    """
    png, meta = shot.capture(window, region)
    if os.environ.get("HYPRUSE_SCREENSHOT_MODE", "image") == "file":
        path = _runtime_dir() / f"shot-{int(time.time() * 1000)}.png"
        path.write_bytes(png)
        return [f"screenshot written to {path} — read that file to view it", json.dumps(meta)]
    return [MCPImage(data=png, format="png"), json.dumps(meta)]


def main() -> None:
    if "--version" in sys.argv:
        print(f"hypruse {__version__}")
        return
    mcp.run()


if __name__ == "__main__":
    main()
