"""hypruse MCP server, computer use for Hyprland.

Design: semantic-first. An agent should read `desktop` and act through
`hypr`/`launch` (IPC, milliseconds, deterministic) and reach for
`screenshot` + `pointer`/`keyboard` only to work *inside* application
windows. All coordinates are Hyprland's global logical layout pixels,
the same space cursorpos, window `at`, and movecursor use.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from hypruse import __version__, events, hyprctl, safety, session
from hypruse import clipboard as clip
from hypruse import input as hinput
from hypruse import screenshot as shot

INSTRUCTIONS = """\
hypruse controls a live Hyprland desktop. Workflow: call `desktop` first
and prefer `hypr`/`launch` (IPC, instant and exact) for anything window-
or workspace-shaped; use `screenshot` + `pointer`/`keyboard` only to see
and operate inside application windows. To verify an effect without a
second round-trip, pass `then='desktop'` (a fresh snapshot) or
`then='screenshot'` to the acting call itself instead of calling `desktop`
again. `binds` lists the owner's own keybinds; to run one, call
`use_bind` with its combo (synthetic keypresses do NOT trigger compositor
binds, so `keyboard` is only for shortcuts the focused app handles). After
actions with delayed effects, block on `wait_for` (window_open,
title_change) instead of sleeping.

Coordinates: one space everywhere, Hyprland global logical pixels (window
`at`, cursor, clicks). Screenshots are pixel space: map back with
global = geometry[:2] + image_pixel / scale, using the metadata's `scale`
and `image` [w,h] (they already account for any downscaling). When
screenshot returns a file path instead of an image, read that file.

Clicking precisely is hard: estimating a target's pixel from a full-screen
image is only accurate to within tens of pixels, which misses small
controls. For anything small, work coarse-to-fine: screenshot the window,
estimate the target's global point, then call `zoom` at that point and
re-estimate on the zoomed image before clicking. Zoomed captures come back
near 1:1 (scale ~1.0) with the target large and their origin in `geometry`,
so global = geometry[:2] + image_pixel lands cleanly. Estimate by
proportion (e.g. "60% across a 300px-wide crop → x≈180"), not absolute
guessing, and after a click that should change something, screenshot again
to confirm before continuing (stable=true waits out animations).

The cursor and keyboard focus are SHARED with the human at the desk:
finish what you start, and expect every action to be visible."""

READONLY = os.environ.get("HYPRUSE_READONLY", "").lower() in ("1", "true", "yes", "on")
CLIPBOARD = os.environ.get("HYPRUSE_CLIPBOARD", "").lower() in ("1", "true", "yes", "on")

_READONLY_NOTE = """

READ-ONLY MODE is active: only observation tools are available
(desktop, screenshot, zoom, binds, wait_for). Input, window-management,
and use_bind are disabled by the user."""

mcp = FastMCP("hypruse", instructions=INSTRUCTIONS + (_READONLY_NOTE if READONLY else ""))


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    d = Path(base) / "hypruse"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_shots(d: Path, keep: int = 20) -> None:
    """XDG_RUNTIME_DIR is tmpfs (RAM), cap stored captures to the newest N."""
    for old in sorted(d.glob("shot-*.*"))[:-keep]:
        with contextlib.suppress(OSError):
            old.unlink()


@mcp.tool()
def desktop() -> dict[str, Any]:
    """Semantic desktop snapshot: monitors, workspaces, windows (address,
    class, title, `at` + `size` in global coords), active window, cursor.
    Call first; act on the addresses it returns."""
    return hyprctl.snapshot()


def _deliver_capture(
    window: str = "",
    region: str = "",
    scale: float = 0.0,
    extra: dict[str, Any] | None = None,
    stable: bool = False,
    lossless: bool = False,
) -> list[Any]:
    """Capture and package for MCP transport: inline image + metadata in
    image mode, a saved file path + metadata otherwise."""
    grab = shot.capture_stable if stable else shot.capture
    if os.environ.get("HYPRUSE_SCREENSHOT_MODE", "file") == "image":
        # Fit the transport budget (base64 adds ~33%; Claude Desktop caps
        # results at 1 MB) by degrading format before resolution.
        budget = int(os.environ.get("HYPRUSE_MAX_IMAGE_BYTES", "700000"))
        max_edge = int(os.environ.get("HYPRUSE_MAX_IMAGE_EDGE", "1568"))
        data, meta = grab(
            window, region, scale=scale, max_bytes=budget, max_edge=max_edge, lossless=lossless
        )
        meta.update(extra or {})
        image_block = ImageContent(
            type="image",
            data=base64.b64encode(data).decode(),
            mimeType=f"image/{meta['format']}",
        )
        return [image_block, TextContent(type="text", text=json.dumps(meta))]
    data, meta = grab(window, region, scale=scale, lossless=lossless)
    meta.update(extra or {})
    d = _runtime_dir()
    path = d / f"shot-{int(time.time() * 1000)}.{meta['format']}"
    path.write_bytes(data)
    _prune_shots(d)
    return [
        TextContent(
            type="text", text=f"screenshot saved, read this file to view the screen: {path}"
        ),
        TextContent(type="text", text=json.dumps(meta)),
    ]


@mcp.tool()
def screenshot(
    window: str = "",
    region: str = "",
    scale: float = 0,
    stable: bool = False,
    lossless: bool = False,
) -> list[Any]:
    """Capture the focused monitor, a window (`window`: "active" or an
    address from desktop, cheapest for reading one app), or a `region`
    "x,y,WxH". Returns the image (or a file path to read) + JSON metadata
    with geometry/scale for pixel→global mapping. `scale` 0.1-1.0:
    optional deliberate downscale, usually leave unset. `stable=true`
    waits (up to 2s) until two consecutive frames match, so a capture
    right after an action is not taken mid-animation; metadata gains
    `stable`. Captures are fast JPEG by default; `lossless=true` returns
    PNG for pixel-exact work. Before clicking a small control, follow with
    `zoom` at the estimated point."""
    safety.touch("screenshot")
    return _deliver_capture(window, region, scale=scale, stable=stable, lossless=lossless)


@mcp.tool()
def zoom(
    x: float,
    y: float,
    size: str = "",
    window: str = "",
    stable: bool = False,
    lossless: bool = False,
) -> list[Any]:
    """Native-resolution re-capture around a point: the precision step of
    the coarse-to-fine loop. Screenshot first, estimate the target's global
    x,y, zoom there, re-estimate on the zoomed image (scale ~1.0, so
    global = geometry[:2] + image_pixel), then click. `size` "WxH" in
    logical pixels (default 480x360) is clamped to the screen; `window`
    (an address from desktop) clamps to that window instead. The metadata
    echoes the requested point back as `point`; `stable=true` waits for
    the frame to settle first; `lossless=true` returns PNG."""
    safety.touch("zoom")
    rx, ry, rw, rh = shot.zoom_region(x, y, size, window)
    return _deliver_capture(
        region=f"{rx},{ry},{rw}x{rh}",
        lossless=lossless,
        extra={"target": "zoom", "point": [x, y]},
        stable=stable,
    )


_OBSERVE_MODES = ("none", "desktop", "screenshot")


def _acted(msg: str, then: str) -> list[Any] | str:
    """Fuse an action with its observation: append a fresh view of the
    result to the action's own tool result, so the agent sees the effect
    without spending a second round-trip. `then='desktop'` appends a
    semantic snapshot (~30 ms, a few hundred tokens, best for window/focus
    changes); `'screenshot'` appends a stable capture of the focused
    monitor (best for visual changes); `'none'` appends nothing."""
    if then == "none":
        return msg
    head = TextContent(type="text", text=msg)
    if then == "desktop":
        return [head, TextContent(type="text", text=json.dumps(hyprctl.snapshot()))]
    if then == "screenshot":
        return [head, *_deliver_capture(stable=True)]
    raise ValueError(f"unknown then {then!r}: {'|'.join(_OBSERVE_MODES)}")


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
    then: str = "none",
) -> list[Any] | str:
    """Mouse in global coordinates. action='move' (x,y) | 'click' (optional
    x,y first; button left/right/middle; double=true) | 'drag' (x,y →
    to_x,to_y holding button) | 'scroll' (scroll_dy notches, positive =
    content down; optional x,y first). `then` appends the result to this
    call so you skip a round-trip: 'desktop' a fresh snapshot, 'screenshot'
    a stable capture, 'none' (default) nothing."""
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
    return _acted(f"{action} ok; cursor now at {hyprctl.cursor_pos()}", then)


def keyboard(action: str, text: str = "", keys: str = "", then: str = "none") -> list[Any] | str:
    """Keyboard to the FOCUSED APP (focus via hypr first). action='type'
    (text, unicode-safe) | 'key' (keys combo: 'ctrl+shift+t', 'esc', 'F5';
    aliases enter/esc/tab/backspace/pgup/pgdn/arrows, else XKB keysyms).
    This drives shortcuts the focused application handles (ctrl+t, ctrl+l).
    It does NOT trigger Hyprland's own keybinds (super+...): those go
    through `use_bind`, and workspace/window actions through `hypr`. `then`
    ('desktop'|'screenshot'|'none') appends the result to this call."""
    safety.touch(f"keyboard:{action}")
    if action == "type":
        if not text:
            raise ValueError("type needs text")
        hinput.type_text(text)
        return _acted(f"typed {len(text)} characters", then)
    if action == "key":
        if not keys:
            raise ValueError("key needs keys")
        hinput.key_combo(keys)
        return _acted(f"pressed {keys}", then)
    raise ValueError(f"unknown action {action!r}: type|key")


_ADDR = re.compile(r"^0x[0-9a-fA-F]+$")


def _addr(target: str) -> str:
    if not _ADDR.match(target):
        raise ValueError(
            f"{target!r} is not a window address, use the `address` field from desktop()"
        )
    return f"address:{target}"


def hypr(action: str, target: str = "", workspace: str = "", then: str = "none") -> list[Any] | str:
    """Window/workspace ops over IPC (instant, no vision).
    action='workspace' (workspace: number/name/'special:name') |
    'focus_window' (target: address) | 'move_window' (target + workspace,
    silent) | 'close_window' (target) | 'fullscreen' (target?) |
    'toggle_floating' (target?). `then` ('desktop'|'screenshot'|'none')
    appends the result to this call."""
    safety.touch(f"hypr:{action}")
    if action == "workspace":
        if not workspace:
            raise ValueError("workspace action needs `workspace`")
        hyprctl.dispatch("workspace", workspace)
        msg = f"on workspace {workspace}"
    elif action == "focus_window":
        hyprctl.dispatch("focuswindow", _addr(target))
        msg = f"focused {target}"
    elif action == "move_window":
        if not workspace:
            raise ValueError("move_window needs `workspace`")
        hyprctl.dispatch("movetoworkspacesilent", f"{workspace},{_addr(target)}")
        msg = f"moved {target} to workspace {workspace}"
    elif action == "close_window":
        hyprctl.dispatch("closewindow", _addr(target))
        msg = f"asked {target} to close"
    elif action == "fullscreen":
        if target:
            hyprctl.dispatch("focuswindow", _addr(target))
        hyprctl.dispatch("fullscreen", "0")
        msg = "fullscreen toggled"
    elif action == "toggle_floating":
        args = (_addr(target),) if target else ()
        hyprctl.dispatch("togglefloating", *args)
        msg = "floating toggled"
    else:
        raise ValueError(
            f"unknown action {action!r}: workspace|focus_window|move_window|"
            "close_window|fullscreen|toggle_floating"
        )
    return _acted(msg, then)


def _await_new_window(before: set[str], wait_s: float) -> dict[str, Any] | None:
    """Polling fallback when the event socket is unavailable."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(0.15)
        for c in hyprctl.query("clients"):
            if c["address"] not in before:
                return c
    return None


def _client_by_address(address: str) -> dict[str, Any] | None:
    return next((c for c in hyprctl.query("clients") if c["address"] == address), None)


def _launch_and_wait(rule_command: str, wait_s: float) -> dict[str, Any] | None:
    """Dispatch exec and return the new window's client record.

    Preferred path: subscribe to the event socket BEFORE dispatching, then
    block on the openwindow event (no race, no polling). Falls back to
    clients-diff polling if socket2 is unreachable.
    """
    try:
        stream = events.EventStream()
    except events.EventError:
        before = {c["address"] for c in hyprctl.query("clients")}
        hyprctl.dispatch("exec", rule_command)
        return _await_new_window(before, wait_s)
    with stream:
        hyprctl.dispatch("exec", rule_command)
        hit = stream.wait_for({"openwindow"}, None, wait_s)
    if hit is None:
        return None
    address = hit[1]["address"]
    win = _client_by_address(address)
    if win is None:
        time.sleep(0.1)  # event can beat hyprctl's client list by a beat
        win = _client_by_address(address)
    return win


def launch(command: str, workspace: str = "", wait_s: float = 8.0) -> dict[str, Any] | str:
    """Run `command` via Hyprland exec. Optional `workspace` placement
    (silent, works even for single-instance apps like browsers, whose
    window gets moved after it appears) and `wait_s` (1-30, default 8;
    raise for slow apps). Returns the new window's
    address/class/title/workspace, or a timeout note."""
    safety.touch("launch")
    wait_s = min(max(wait_s, 1.0), 30.0)
    rule = f"[workspace {workspace} silent] " if workspace else ""
    win = _launch_and_wait(rule + command, wait_s)
    if win is None:
        return (
            f"launched, but no new window appeared within {wait_s:.0f}s, slow or "
            "single-instance apps may open late and on their own workspace; call "
            "desktop() to find the window, then hypr move_window if needed"
        )
    ws = win.get("workspace", {})
    result: dict[str, Any] = {
        "address": win["address"],
        "class": win.get("class", ""),
        "title": win.get("title", ""),
        "workspace": ws.get("id"),
    }
    landed = {str(ws.get("id")), str(ws.get("name", ""))}
    if workspace and workspace not in landed:
        hyprctl.dispatch("movetoworkspacesilent", f"{workspace},address:{win['address']}")
        result["workspace"] = workspace
        result["note"] = (
            "window opened elsewhere (single-instance app behavior); "
            "moved to the requested workspace"
        )
    return result


_INTERACTIVE_HELP = """\
hypruse {version}, an MCP server, not an interactive program.

It speaks the MCP protocol over stdin/stdout and is meant to be launched by
an MCP client, so running it directly in a terminal just waits silently for
a client that never connects (Ctrl+C to quit).

Register it with Claude Code:
  claude mcp add -s user hypruse -- uvx hypruse

Or set `uvx hypruse` as a stdio server in your MCP client's config.
Check the install with:  hypruse --version
"""


@mcp.tool()
def binds() -> list[dict[str, Any]]:
    """The user's own Hyprland keybinds: combo, action, arg, and a
    description when the config provides one. This is how the desktop's
    owner drives it: to perform one of these workflows, call `use_bind`
    with the combo (it runs the bound action). NOTE: the `keyboard` tool
    canNOT trigger these compositor binds (synthetic keys reach apps, not
    Hyprland's bind matcher), so do not try to press them."""
    return hyprctl.binds()


def clipboard(action: str, text: str = "") -> str:
    """Clipboard access (opt-in surface: this tool exists only when the
    user set HYPRUSE_CLIPBOARD=1 in the server env). action='read'
    returns the clipboard's text content | 'write' (text) replaces it.
    Text only. The clipboard belongs to the human at the desk: treat its
    contents as sensitive, and read before overwriting."""
    safety.touch(f"clipboard:{action}")
    if action == "read":
        content = clip.read()
        if not content:
            return "(clipboard is empty)"
        if len(content) > 100_000:
            return content[:100_000] + f"\n[truncated, {len(content)} chars total]"
        return content
    if action == "write":
        if not text:
            raise ValueError("write needs text")
        clip.write(text)
        return f"copied {len(text)} characters to the clipboard"
    raise ValueError(f"unknown action {action!r}: read|write")


def use_bind(combo: str, then: str = "none") -> list[Any] | str:
    """Run one of the user's own Hyprland keybinds by its combo (from the
    `binds` tool), e.g. 'SUPER+F'. This executes the bound action directly
    (the only reliable way: synthetic keypresses do not trigger compositor
    binds). Use it to drive the owner's configured workflows: launchers,
    layout shortcuts, scratchpads. `then` ('desktop'|'screenshot'|'none')
    appends the result to this call (handy after a launcher bind)."""
    safety.touch("use_bind")
    bind = hyprctl.find_bind(combo)
    if bind is None:
        raise ValueError(f"no keybind {combo!r}; call binds() for the exact combos")
    action, arg = bind["action"], bind.get("arg", "")
    hyprctl.dispatch(action, *([arg] if arg else []))
    return _acted(f"ran {bind['combo']}: {action} {arg}".rstrip(), then)


_WAIT_EVENTS = {
    "window_open": {"openwindow"},
    "window_close": {"closewindow"},
    "workspace": {"workspace"},
    "title_change": {"windowtitlev2", "windowtitle"},
}


def _already_satisfied(event: str, needle: str) -> dict[str, Any] | None:
    """Level-triggered pre-check: has the awaited condition already
    happened? A trigger tool call returns before the agent can call
    wait_for, so a fast event fires before we subscribe. Checking current
    state first catches that. Only unambiguous cases: a close is 'already
    done' if nothing matches; a workspace wait is done if it is active."""
    if event == "window_close" and needle:
        for c in hyprctl.query("clients"):
            hay = f"{c.get('address', '')} {c.get('class', '')} {c.get('title', '')}".lower()
            if needle in hay:
                return None  # still open, wait for the real event
        return {"event": "closewindow", "already": True}
    if event == "workspace":
        ws = hyprctl.query("activeworkspace") or {}
        name = str(ws.get("name", ""))
        if not needle or needle in name.lower():
            return {"event": "workspace", "name": name, "already": True}
    return None


@mcp.tool()
def wait_for(event: str, match: str = "", timeout_s: float = 10) -> dict[str, Any] | str:
    """Block until a desktop event happens (real compositor events, not
    polling). event: 'window_open' | 'window_close' | 'workspace' |
    'title_change'. match: optional case-insensitive substring filter over
    the event's fields (class/title/workspace name/address). timeout_s
    1-60, default 10. Returns the event payload, or a timeout note. Use it
    after actions with delayed effects: app startups, page loads that
    change a window title."""
    names = _WAIT_EVENTS.get(event)
    if names is None:
        raise ValueError(f"unknown event {event!r}: {'|'.join(_WAIT_EVENTS)}")
    safety.touch(f"wait_for:{event}")
    timeout_s = min(max(timeout_s, 1.0), 60.0)
    needle = match.lower()

    already = _already_satisfied(event, needle)
    if already is not None:
        return already

    def matcher(_name: str, payload: dict[str, Any]) -> bool:
        if not needle:
            return True
        return needle in " ".join(str(v) for v in payload.values()).lower()

    try:
        with events.EventStream() as stream:
            hit = stream.wait_for(names, matcher, timeout_s)
    except events.EventError as exc:
        return f"event socket unavailable: {exc}"
    if hit is None:
        return f"timeout: no matching {event} event within {timeout_s:.0f}s"
    return {"event": hit[0], **hit[1]}


# Acting tools register only outside read-only mode; observation tools
# (desktop, screenshot, zoom, binds, wait_for) are decorated above and
# always on. Clipboard is double-gated: opt-in env flag, never read-only.
if not READONLY:
    for _acting_tool in (pointer, keyboard, hypr, launch, use_bind):
        mcp.tool()(_acting_tool)
    if CLIPBOARD:
        mcp.tool()(clipboard)


def main() -> None:
    if "--version" in sys.argv:
        print(f"hypruse {__version__}")
        return
    # A human ran it by hand (stdin is a terminal, not a client pipe), an
    # MCP stdio client always connects stdin to a pipe, so a TTY here means
    # nobody is going to talk to us. Explain instead of hanging.
    if sys.stdin is None or sys.stdin.isatty():
        print(_INTERACTIVE_HELP.format(version=__version__), file=sys.stderr)
        return
    session.ensure_session_env()
    safety.init()
    mcp.run()


if __name__ == "__main__":
    main()
