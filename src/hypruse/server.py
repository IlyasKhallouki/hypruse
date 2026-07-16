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
import re
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage

from hypruse import __version__, hyprctl, safety
from hypruse import input as hinput
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

    Returns the saved PNG's path (READ THAT FILE to see the screen) plus a
    JSON metadata line. The image is in pixel space; convert an image pixel
    to a clickable global coordinate with: global = geometry[:2] + pixel /
    scale (scale is 1.0 unless the monitor uses fractional scaling).
    """
    safety.touch("screenshot")
    png, meta = shot.capture(window, region)
    if os.environ.get("HYPRUSE_SCREENSHOT_MODE", "file") == "image":
        return [MCPImage(data=png, format="png"), json.dumps(meta)]
    path = _runtime_dir() / f"shot-{int(time.time() * 1000)}.png"
    path.write_bytes(png)
    return [f"screenshot written to {path} — read that file to view it", json.dumps(meta)]


@mcp.tool()
def pointer(
    action: str,
    x: float | None = None,
    y: float | None = None,
    button: str = "left",
    to_x: float | None = None,
    to_y: float | None = None,
    scroll_dy: float = 0,
    scroll_dx: float = 0,
    double: bool = False,
) -> str:
    """Mouse control, in the same global coordinates `desktop` and
    `screenshot` metadata use. Actions:
    'move' — place cursor at (x, y).
    'click' — click `button` (left/right/middle), optionally moving to (x, y)
    first; set double=true for a double-click.
    'drag' — hold `button` from (x, y) to (to_x, to_y).
    'scroll' — wheel by scroll_dy notches (positive scrolls content down),
    optionally moving to (x, y) first.

    The cursor and keyboard focus are SHARED with the human at the desk:
    prefer `hypr`/`launch` for window management, keep pointer use inside
    application windows, and finish what you start.
    """
    safety.touch(f"pointer:{action}")
    if action == "move":
        if x is None or y is None:
            raise ValueError("move needs x and y")
        hinput.move(x, y)
    elif action == "click":
        hinput.click(x, y, button=button, double=double)
    elif action == "drag":
        if None in (x, y, to_x, to_y):
            raise ValueError("drag needs x, y, to_x, to_y")
        hinput.drag(x, y, to_x, to_y, button=button)  # type: ignore[arg-type]
    elif action == "scroll":
        hinput.scroll(dy=scroll_dy, dx=scroll_dx, x=x, y=y)
    else:
        raise ValueError(f"unknown action {action!r}: move|click|drag|scroll")
    return f"{action} ok; cursor now at {hyprctl.cursor_pos()}"


@mcp.tool()
def keyboard(action: str, text: str = "", keys: str = "") -> str:
    """Keyboard input to the FOCUSED window (focus one first via the `hypr`
    tool). Actions:
    'type' — type `text` literally (unicode-safe, layout-correct).
    'key' — press `keys`, e.g. 'ctrl+shift+t', 'super+enter', 'esc', 'F5'.
    Modifiers: ctrl, shift, alt, super. Common aliases (enter, esc, tab,
    backspace, pgup/pgdn, arrows) work; anything else is treated as an XKB
    keysym name (case-sensitive).
    """
    safety.touch(f"keyboard:{action}")
    if action == "type":
        if not text:
            raise ValueError("type needs text")
        hinput.type_text(text)
        return f"typed {len(text)} characters"
    if action == "key":
        if not keys:
            raise ValueError("key needs keys")
        hinput.key_combo(keys)
        return f"pressed {keys}"
    raise ValueError(f"unknown action {action!r}: type|key")


_ADDR = re.compile(r"^0x[0-9a-fA-F]+$")


def _addr(target: str) -> str:
    if not _ADDR.match(target):
        raise ValueError(
            f"{target!r} is not a window address — use the `address` field from desktop()"
        )
    return f"address:{target}"


@mcp.tool()
def hypr(action: str, target: str = "", workspace: str = "") -> str:
    """Native window/workspace management over Hyprland IPC — instant and
    exact, no vision needed. Actions:
    'workspace' — switch to `workspace` (a number, a name, or 'special:name').
    'focus_window' — focus the window `target` (address from desktop()).
    'move_window' — move window `target` to `workspace` silently (the user's
    view does not switch).
    'close_window' — ask window `target` to close (like clicking X).
    'fullscreen' — toggle fullscreen on `target` (or the active window).
    'toggle_floating' — toggle floating on `target` (or the active window).
    """
    safety.touch(f"hypr:{action}")
    if action == "workspace":
        if not workspace:
            raise ValueError("workspace action needs `workspace`")
        hyprctl.dispatch("workspace", workspace)
        return f"on workspace {workspace}"
    if action == "focus_window":
        hyprctl.dispatch("focuswindow", _addr(target))
        return f"focused {target}"
    if action == "move_window":
        if not workspace:
            raise ValueError("move_window needs `workspace`")
        hyprctl.dispatch("movetoworkspacesilent", f"{workspace},{_addr(target)}")
        return f"moved {target} to workspace {workspace}"
    if action == "close_window":
        hyprctl.dispatch("closewindow", _addr(target))
        return f"asked {target} to close"
    if action == "fullscreen":
        if target:
            hyprctl.dispatch("focuswindow", _addr(target))
        hyprctl.dispatch("fullscreen", "0")
        return "fullscreen toggled"
    if action == "toggle_floating":
        args = (_addr(target),) if target else ()
        hyprctl.dispatch("togglefloating", *args)
        return "floating toggled"
    raise ValueError(
        f"unknown action {action!r}: workspace|focus_window|move_window|"
        "close_window|fullscreen|toggle_floating"
    )


@mcp.tool()
def launch(command: str, workspace: str = "") -> dict[str, Any] | str:
    """Launch an application via Hyprland exec. If `workspace` is given
    ('3', 'name', 'special:x'), the app opens there silently without
    switching the user's view. Waits up to 3s for the new window and returns
    its address/class/title/workspace so you can focus or screenshot it
    immediately.
    """
    safety.touch("launch")
    before = {c["address"] for c in hyprctl.query("clients")}
    rule = f"[workspace {workspace} silent] " if workspace else ""
    hyprctl.dispatch("exec", rule + command)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        time.sleep(0.15)
        for c in hyprctl.query("clients"):
            if c["address"] not in before:
                return {
                    "address": c["address"],
                    "class": c.get("class", ""),
                    "title": c.get("title", ""),
                    "workspace": c.get("workspace", {}).get("id"),
                }
    return "launched, but no new window appeared within 3s — check desktop()"


def main() -> None:
    if "--version" in sys.argv:
        print(f"hypruse {__version__}")
        return
    safety.init()
    mcp.run()


if __name__ == "__main__":
    main()
