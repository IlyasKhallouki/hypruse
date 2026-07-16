"""hypruse MCP server — computer use for Hyprland.

Design: semantic-first. An agent should read `desktop` and act through
`hypr`/`launch` (IPC, milliseconds, deterministic) and reach for
`screenshot` + `pointer`/`keyboard` only to work *inside* application
windows. All coordinates are Hyprland's global logical layout pixels —
the same space cursorpos, window `at`, and movecursor use.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from hypruse import __version__, hyprctl

mcp = FastMCP("hypruse")


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


def main() -> None:
    if "--version" in sys.argv:
        print(f"hypruse {__version__}")
        return
    mcp.run()


if __name__ == "__main__":
    main()
