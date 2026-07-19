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
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from hypruse import __version__, a11y, events, hyprctl, safety, session, trust
from hypruse import clipboard as clip
from hypruse import input as hinput
from hypruse import screenshot as shot

INSTRUCTIONS = """\
hypruse controls a live Hyprland desktop. Workflow: call `desktop` first
and prefer `hypr`/`launch` (IPC, instant and exact) for anything window-
or workspace-shaped; use `screenshot` + `pointer`/`keyboard` only to see
and operate inside application windows. Launchers, bars, notification
popups, and lock screens are not windows: `desktop` reports them under
`layers`. To CLICK a named control inside an app, call `click_ui` FIRST:
for GTK/Qt apps it resolves the control in the accessibility tree and
clicks it in ONE call, no image and no pixel estimation (`ui` lists the
controls and their current values without clicking; `marks` returns a
numbered screenshot plus legend when you want to SEE what is clickable,
then `click_ui(mark=N)`). Use screenshot + zoom when the tree exposes
nothing (terminals, canvas UIs, Electron/Chrome without a flag). To
verify an effect without a second round-trip, pass `then='desktop'` (a
fresh snapshot), `then='ui'` (the focused window's controls with current
values, cheapest after typing or toggling), or `then='screenshot'` to
the acting call itself instead of calling `desktop` again. `binds` lists
the owner's own keybinds; to run one, call
`use_bind` with its combo (synthetic keypresses do NOT trigger compositor
binds, so `keyboard` is only for shortcuts the focused app handles). After
actions with delayed effects, block on `wait_for` (window_open,
title_change, layer_open) instead of sleeping. To run several actions at
once (click, type, press enter, wait), pass them to `sequence` as one
call: it stops if
the desktop changes structurally between steps, so you spend one
round-trip instead of one per step. It does not catch a bare focus change,
so give a keyboard step a window= address when typing matters.

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
finish what you start, and expect every action to be visible. Before
typing, pass `keyboard(window=<address>)` to focus the intended app first,
so keystrokes never land in the wrong window."""

READONLY = os.environ.get("HYPRUSE_READONLY", "").lower() in ("1", "true", "yes", "on")
CLIPBOARD = os.environ.get("HYPRUSE_CLIPBOARD", "").lower() in ("1", "true", "yes", "on")

_READONLY_NOTE = """

READ-ONLY MODE is active: only observation tools are available
(desktop, screenshot, zoom, ui, marks, binds, wait_for). Input,
window-management, and use_bind are disabled by the user."""

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
    class, title, `at` + `size` in global coords), active window, cursor,
    and `layers` when layer-shell surfaces are up: launchers (wofi/rofi),
    bars, notification popups, and lock screens are NOT windows and appear
    only there, with a best-effort `kind` and global geometry you can
    screenshot by region or click into. Call first; act on the addresses
    it returns."""
    snap = hyprctl.snapshot()
    # under HYPRUSE_STRICT a seat that moved without hypruse blocks every
    # acting tool until the agent re-observes; this read IS that
    # re-observation, so it re-arms the guard (as its error advises)
    trust.remember_seat()
    return snap


def _image_mode() -> bool:
    return os.environ.get("HYPRUSE_SCREENSHOT_MODE", "file") == "image"


def _grab_env(
    window: str = "",
    region: str = "",
    scale: float = 0.0,
    stable: bool = False,
    lossless: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Capture honoring the transport mode's limits: in image mode, fit the
    result budget (base64 adds ~33%; Claude Desktop caps results at 1 MB)
    by degrading format before resolution, and cap the long edge so the
    host never downscales the image under the model."""
    grab = shot.capture_stable if stable else shot.capture
    if _image_mode():
        budget = int(os.environ.get("HYPRUSE_MAX_IMAGE_BYTES", "700000"))
        max_edge = int(os.environ.get("HYPRUSE_MAX_IMAGE_EDGE", "1568"))
        return grab(
            window, region, scale=scale, max_bytes=budget, max_edge=max_edge, lossless=lossless
        )
    return grab(window, region, scale=scale, lossless=lossless)


def _package(data: bytes, meta: dict[str, Any]) -> list[Any]:
    """Package an image for MCP transport: inline + metadata in image mode,
    a saved file path + metadata otherwise."""
    if _image_mode():
        image_block = ImageContent(
            type="image",
            data=base64.b64encode(data).decode(),
            mimeType=f"image/{meta['format']}",
        )
        return [image_block, TextContent(type="text", text=json.dumps(meta))]
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


def _deliver_capture(
    window: str = "",
    region: str = "",
    scale: float = 0.0,
    extra: dict[str, Any] | None = None,
    stable: bool = False,
    lossless: bool = False,
) -> list[Any]:
    data, meta = _grab_env(window, region, scale=scale, stable=stable, lossless=lossless)
    meta.update(extra or {})
    trust.notify_capture()  # HYPRUSE_MARK: flash an on-screen notice, rate-limited
    trust.remember_seat()  # a fresh capture re-arms the strict seat guard
    return _package(data, meta)


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


def _resolve_window(window: str) -> dict[str, Any]:
    """The hyprctl client for a window address ('' or 'active' = focused)."""
    clients = hyprctl.query("clients")
    target = window
    if not window or window == "active":
        target = (hyprctl.query("activewindow") or {}).get("address")
    if not target:
        raise ValueError("no active window; pass a window address from desktop()")
    client = next((c for c in clients if c.get("address") == target), None)
    if client is None:
        raise ValueError(f"window {target!r} not found, call desktop() for current addresses")
    return client


def _active_client() -> dict[str, Any] | None:
    """The focused window's client, or None when nothing is focused (a bare
    key press must still work). Used by the trust guards."""
    with contextlib.suppress(Exception):
        return _resolve_window("active")
    return None


def _ui_read(window: str = "", name: str = "", actionable: bool = True) -> list[Any] | str:
    """Shared body of the `ui` tool: a window's accessible elements with
    global click points and current values, or a fall-back-to-vision
    message. Reused by `then='ui'` fusion and click-by-name resolution."""
    client = _resolve_window(window)
    title = client.get("title", "")
    cls = client.get("class", "the window")
    try:
        bus = a11y.connect()
        app = a11y.app_for_pid(bus, client.get("pid"), title)
        if app is None:
            return f"{cls} exposes no accessibility tree; use screenshot + zoom instead"
        frame = a11y.window_frame(bus, app[0], app[1], title, tuple(client["size"]))
        elements, truncated = a11y.find_elements(
            bus, frame[0], frame[1], name=name, actionable=actionable
        )
    except a11y.A11yError as exc:
        return f"accessibility read failed: {exc}; use screenshot + zoom instead"
    ax, ay = client["at"]
    aw, ah = client["size"]
    out = []
    for e in elements:
        ex, ey, ew, eh = e["extent"]
        x, y = ax + ex + ew // 2, ay + ey + eh // 2
        # the window rect is authoritative: a point outside it belongs to a
        # widget the toolkit did not really lay out (an unrendered tab page),
        # and clicking it would land on some other window
        if not (ax <= x < ax + aw and ay <= y < ay + ah):
            continue
        item = {
            "role": e["role"],
            "name": e["name"],
            "x": x,
            "y": y,
            "clickable": e["clickable"],
        }
        for key in ("value", "percent", "checked"):  # present only where it applies
            if key in e:
                item[key] = e[key]
        out.append(item)
    if not out:
        what = f"matching {name!r}" if name else "actionable"
        tail = " (stopped after a large tree; try a name filter)" if truncated else ""
        return f"no {what} elements in {cls}{tail}"
    return out


@mcp.tool()
def ui(window: str = "", name: str = "", actionable: bool = True) -> list[Any] | str:
    """Read a window's accessibility tree (AT-SPI) and return its elements
    with GLOBAL click points, so you can target a control by NAME with no
    screenshot and no pixel guessing. `window` is an address from desktop
    (default: the focused window). `name` filters to elements whose
    accessible name contains it (case-insensitive); `actionable` (default)
    keeps only interactive roles (buttons, entries, menu items, ...).
    Returns [{role, name, x, y, clickable}] where x,y is the click point:
    focus the window, then click it with `pointer` (the window must be
    visible to receive the click), or do both in one call with `click_ui`.
    Controls that carry a CURRENT VALUE also
    report it: `value` (text typed into an entry, or a slider/spinner
    number), `percent` for a slider's position, `checked` for a box or
    toggle. Password fields never report contents, and many dropdowns
    expose no value at all, so read the screen with screenshot when a
    rendered value matters. Not every app exposes a tree (terminals, and
    Electron/Chrome without --force-renderer-accessibility, expose little
    or nothing); when it does not, fall back to screenshot + zoom."""
    safety.touch("ui")
    return _ui_read(window, name, actionable)


def _magick_exe() -> str | None:
    return next((exe for exe in ("magick", "convert") if shutil.which(exe)), None)


def _draw_marks(
    data: bytes, fmt: str, points: list[tuple[int, int, int]]
) -> bytes | None:
    """Draw numbered marks (a red dot with the number) at image-pixel
    points, via ImageMagick like the other shell-out backends (grim,
    wtype, busctl). Returns None when ImageMagick is not installed."""
    exe = _magick_exe()
    if exe is None:
        return None
    d = _runtime_dir()
    # unique per call: parallel tool calls run on worker threads, and fixed
    # names would let two concurrent marks() clobber each other's temp image
    stem = f"marks-{int(time.time() * 1000)}-{os.getpid()}-{id(points)}"
    src, dst = d / f"{stem}-src.{fmt}", d / f"{stem}-out.{fmt}"
    src.write_bytes(data)
    args = [exe, str(src)]
    for mark, px, py in points:
        label = str(mark)
        args += ["-fill", "#d92626", "-stroke", "white", "-strokewidth", "1"]
        args += ["-draw", f"circle {px},{py} {px + 9},{py}"]
        tx = px - (4 if len(label) == 1 else 8)
        args += ["-stroke", "none", "-fill", "white", "-pointsize", "13"]
        args += ["-draw", f"text {tx},{py + 5} '{label}'"]
    args.append(str(dst))
    try:
        proc = subprocess.run(args, capture_output=True, timeout=15)
        if proc.returncode != 0:
            return None
        return dst.read_bytes()
    finally:
        for f in (src, dst):
            with contextlib.suppress(OSError):
                f.unlink()


# the last marks capture, so click_ui(mark=N) can click a numbered control.
# Offsets are WINDOW-RELATIVE and re-anchored to the window's current
# position at click time, so a moved window does not invalidate the marks.
_last_marks: dict[str, Any] = {}


@mcp.tool()
def marks(window: str = "", name: str = "") -> list[Any] | str:
    """Set-of-Marks capture: a screenshot of the window WITH its accessible
    controls drawn as numbered red marks, plus a JSON legend mapping each
    number to the control's role, name, current value, and exact global
    click point. One glance replaces the estimate-zoom-estimate loop for
    every control the accessibility tree knows: read the number off the
    image and call `click_ui(mark=N)` (or `pointer` at the legend's x,y).
    `window` is an address from desktop (default: focused); `name` filters
    the marked controls. Falls back to the plain legend when ImageMagick is
    not installed, and to a fall-back-to-vision note when the app exposes
    no tree (then use screenshot + zoom)."""
    safety.touch("marks")
    client = _resolve_window(window)
    addr = client["address"]
    elements = _ui_read(addr, name=name)
    if isinstance(elements, str):
        return elements
    data, meta = _grab_env(window=addr, stable=True)
    ox, oy = meta["geometry"][0], meta["geometry"][1]
    scale = meta["scale"]
    ax, ay = client["at"]
    points: list[tuple[int, int, int]] = []
    legend: list[dict[str, Any]] = []
    stored: dict[int, dict[str, Any]] = {}
    for i, e in enumerate(elements, start=1):
        points.append((i, round((e["x"] - ox) * scale), round((e["y"] - oy) * scale)))
        legend.append({"mark": i, **e})
        stored[i] = {"dx": e["x"] - ax, "dy": e["y"] - ay,
                     "label": f"{e['role']} {e['name']!r}"}
    global _last_marks
    _last_marks = {"window": addr, "items": stored}
    legend_text = TextContent(
        type="text",
        text=json.dumps({"legend": legend, "hint": "click_ui(mark=N) clicks a numbered mark"}),
    )
    marked = _draw_marks(data, meta["format"], points)
    if marked is None:
        return [
            TextContent(
                type="text",
                text="ImageMagick not found, returning the legend only (its "
                "coordinates are exact; install imagemagick for marked captures)",
            ),
            legend_text,
        ]
    meta["target"] = "marks"
    return [*_package(marked, meta), legend_text]


_OBSERVE_MODES = ("none", "desktop", "screenshot", "ui")


def _acted(msg: str, then: str, window: str = "") -> list[Any] | str:
    """Fuse an action with its observation: append a fresh view of the
    result to the action's own tool result, so the agent sees the effect
    without spending a second round-trip. `then='desktop'` appends a
    semantic snapshot (~30 ms, a few hundred tokens, best for window/focus
    changes); `'screenshot'` appends a stable capture of the focused
    monitor (best for visual changes); `'ui'` appends the accessible
    elements of `window` (the acted-on window when the caller knows it,
    else the focused one) with their CURRENT values (a few hundred exact
    tokens instead of an image: best after typing or toggling in an app
    that exposes a tree, degrades to a note when it does not);
    `'none'` appends nothing."""
    if then == "none":
        return msg
    head = TextContent(type="text", text=msg)
    if then == "desktop":
        return [head, TextContent(type="text", text=json.dumps(hyprctl.snapshot()))]
    if then == "screenshot":
        return [head, *_deliver_capture(stable=True)]
    if then == "ui":
        try:
            view = _ui_read(window)
        except Exception as exc:  # the observation must never mask the action's success
            view = f"ui read failed: {exc}"
        payload = view if isinstance(view, str) else json.dumps(view)
        return [head, TextContent(type="text", text=payload)]
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
    allow_auth: bool = False,
) -> list[Any] | str:
    """Mouse in global coordinates. action='move' (x,y) | 'click' (optional
    x,y first; button left/right/middle; double=true) | 'drag' (x,y →
    to_x,to_y holding button) | 'scroll' (scroll_dy notches, positive =
    content down; optional x,y first). `then` appends the result to this
    call so you skip a round-trip: 'desktop' a fresh snapshot, 'screenshot'
    a stable capture, 'ui' the focused window's elements with current
    values, 'none' (default) nothing. `allow_auth=true` overrides the
    refusal to click over a system authentication dialog."""
    safety.touch(f"pointer:{action}")
    trust.guard_seat()
    if action == "move":
        # a move only repositions the cursor; the input-delivering actions
        # below are the ones the confinement and auth guards gate
        if x is None or y is None:
            raise ValueError("move needs x and y")
        hinput.move(x, y)
    elif action == "click":
        trust.guard_pointer(x, y, allow_auth)  # None x/y = click at current cursor
        hinput.click(x, y, button=button, double=double)
    elif action == "drag":
        if None in (x, y, to_x, to_y):
            raise ValueError("drag needs x, y, to_x, to_y")
        trust.guard_pointer(x, y, allow_auth)
        trust.guard_pointer(to_x, to_y, allow_auth)  # the drag ends elsewhere; guard that too
        hinput.drag(x, y, to_x, to_y, button=button)  # type: ignore[arg-type]
    elif action == "scroll":
        trust.guard_pointer(x, y, allow_auth)  # None x/y = scroll at current cursor
        hinput.scroll(dy=scroll_dy, dx=scroll_dx, x=x, y=y)
    else:
        raise ValueError(f"unknown action {action!r}: move|click|drag|scroll")
    trust.remember_seat()
    return _acted(f"{action} ok; cursor now at {hyprctl.cursor_pos()}", then)


def keyboard(
    action: str,
    text: str = "",
    keys: str = "",
    window: str = "",
    then: str = "none",
    allow_auth: bool = False,
) -> list[Any] | str:
    """Keyboard to the focused app. action='type' (text, unicode-safe) |
    'key' (keys combo: 'ctrl+shift+t', 'esc', 'F5'; aliases
    enter/esc/tab/backspace/pgup/pgdn/arrows, else XKB keysyms). Pass
    `window` (an address from desktop) to focus that window first, so
    keystrokes land in the intended app rather than whatever currently
    holds focus. This drives shortcuts the focused application handles
    (ctrl+t, ctrl+l). It does NOT trigger Hyprland's own keybinds
    (super+...): those go through `use_bind`, and workspace/window actions
    through `hypr`. `then` ('desktop'|'screenshot'|'ui'|'none') appends the
    result to this call. `allow_auth=true` overrides the default refusal to
    type into a password field or a system authentication dialog (only when
    a human intends that credential entry)."""
    safety.touch(f"keyboard:{action}")
    trust.guard_seat()
    addr_str = _addr(window) if window else ""  # validate the address format first
    # resolve the window keystrokes will land in, for the confinement and
    # auth guards, but only when a guard is active (it costs a query). When
    # no window= is given we best-effort the focused one; a bare key (esc,
    # F5) with nothing focused must still work.
    target = None
    if trust.guards_window_input():
        target = _resolve_window(window) if window else _active_client()
        if target is not None:
            trust.guard_client(target)  # confinement
            trust.guard_auth_client(target, allow_auth)
    if window:
        hyprctl.dispatch("focuswindow", addr_str)
        time.sleep(0.05)  # let keyboard focus settle before typing into it
    into = f" into {window}" if window else ""
    if action == "type":
        if not text:
            raise ValueError("type needs text")
        if target is not None:
            trust.guard_password_field(target, allow_auth)  # refuse typing a password
        hinput.type_text(text)
        trust.remember_seat()
        return _acted(f"typed {len(text)} characters{into}", then)
    if action == "key":
        if not keys:
            raise ValueError("key needs keys")
        hinput.key_combo(keys)
        trust.remember_seat()
        return _acted(f"pressed {keys}{into}", then)
    raise ValueError(f"unknown action {action!r}: type|key")


def click_ui(
    name: str = "",
    mark: int = 0,
    window: str = "",
    index: int = -1,
    button: str = "left",
    double: bool = False,
    then: str = "none",
    allow_auth: bool = False,
) -> list[Any] | str:
    """Click a control by its accessible NAME, or by a `mark` number from
    the last `marks` capture, in ONE call: the exact coordinate comes from
    the accessibility tree, the window is focused first, and the click goes
    through the real pointer (visible cursor, same panic-kill guarantees),
    so no screenshot and no pixel estimation is spent. Pass exactly one of
    `name` (matched against `window`'s controls, exact accessible name
    preferred, substring otherwise) or `mark`. An ambiguous name returns
    the candidates instead of guessing: disambiguate with `index` (0-based
    into that list) or a more specific name. Falls back with a note when
    the app exposes no tree (use screenshot + zoom + pointer then).
    `then` ('desktop'|'screenshot'|'ui'|'none') appends the result;
    'ui' shows the click's effect on the controls in the same call.
    `allow_auth=true` overrides the refusal to click a system
    authentication dialog."""
    safety.touch("click_ui")
    trust.guard_seat()
    if bool(name) == bool(mark):
        raise ValueError("pass exactly one of `name` or `mark`")
    if mark:
        if not _last_marks or mark not in _last_marks["items"]:
            known = sorted(_last_marks.get("items", {}))
            raise ValueError(
                f"no mark {mark}; current marks: {known or 'none, call marks() first'}"
            )
        client = _resolve_window(_last_marks["window"])  # raises if the window is gone
        item = _last_marks["items"][mark]
        x, y = client["at"][0] + item["dx"], client["at"][1] + item["dy"]
        desc = f"mark {mark} ({item['label']})"
    else:
        client = _resolve_window(window)
        elements = _ui_read(client["address"], name=name)
        if isinstance(elements, str):
            return elements
        exact = [e for e in elements if e["name"].lower() == name.lower()]
        pool = exact or elements
        if index >= 0:
            if index >= len(pool):
                raise ValueError(f"index {index} out of range: {len(pool)} candidates")
            pool = [pool[index]]
        if len(pool) > 1:
            return [
                TextContent(
                    type="text",
                    text=f"{name!r} is ambiguous ({len(pool)} candidates); call again "
                    "with index=N (0-based) or a more specific name:",
                ),
                TextContent(type="text", text=json.dumps(pool)),
            ]
        e = pool[0]
        x, y = e["x"], e["y"]
        desc = f"{e['role']} {e['name']!r}"
    trust.guard_client(client)  # confinement
    trust.guard_auth_client(client, allow_auth)
    hyprctl.dispatch("focuswindow", f"address:{client['address']}")
    time.sleep(0.05)  # focus (and a possible workspace switch) settles first
    hinput.click(x, y, button=button, double=double)
    trust.remember_seat()
    # observe the CLICKED window, not whatever holds focus after the click
    # (the click itself may have spawned a dialog that stole it)
    return _acted(
        f"clicked {desc} at ({x}, {y}) in {client.get('class', '')}",
        then,
        window=client["address"],
    )


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
    'toggle_floating' (target?). `then` ('desktop'|'screenshot'|'ui'|'none')
    appends the result to this call."""
    safety.touch(f"hypr:{action}")
    # confinement: any action naming a specific window must stay in scope;
    # fullscreen/toggle_floating with no target hit the ACTIVE window, so
    # resolve and guard that too, and check the seat has not moved under us
    # (in strict mode, so we do not fullscreen a window the human just
    # refocused). Address-targeted actions name their window, so they skip
    # the seat guard to avoid false refusals.
    if target and action != "workspace":
        trust.guard_window(target)
    elif not target and action in ("fullscreen", "toggle_floating"):
        trust.guard_seat()
        active = _active_client()
        if active is not None:
            trust.guard_client(active)
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
    trust.remember_seat()  # our own focus/workspace change re-baselines the seat
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
    # owned-set for `launched` confinement; tag + notify when HYPRUSE_MARK
    trust.note_launched(win["address"], win.get("class", ""))
    trust.remember_seat()  # the new window took focus; re-baseline for the seat guard
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
    layout shortcuts, scratchpads. `then` ('desktop'|'screenshot'|'ui'|'none')
    appends the result to this call (handy after a launcher bind). Refused
    while HYPRUSE_CONFINE is set: a bind runs an arbitrary compositor action
    that cannot be scoped to a window."""
    safety.touch("use_bind")
    trust.guard_seat()
    trust.guard_use_bind()  # an arbitrary compositor action escapes confinement
    bind = hyprctl.find_bind(combo)
    if bind is None:
        raise ValueError(f"no keybind {combo!r}; call binds() for the exact combos")
    action, arg = bind["action"], bind.get("arg", "")
    hyprctl.dispatch(action, *([arg] if arg else []))
    trust.remember_seat()  # the bind may have moved focus/workspace on our behalf
    return _acted(f"ran {bind['combo']}: {action} {arg}".rstrip(), then)


_WAIT_EVENTS = {
    "window_open": {"openwindow"},
    "window_close": {"closewindow"},
    "workspace": {"workspace"},
    # windowtitlev2 only: the plain windowtitle event carries just an
    # address, so it can never satisfy a title match (and an unfiltered
    # wait would return a payload with no title in it)
    "title_change": {"windowtitlev2"},
    "layer_open": {"openlayer"},
    "layer_close": {"closelayer"},
    "urgent": {"urgent"},
    "screencast": {"screencast"},
}


def _already_satisfied(event: str, needle: str) -> dict[str, Any] | None:
    """Level-triggered pre-check: has the awaited condition already
    happened? A trigger tool call returns before the agent can call
    wait_for, so a fast event fires before we subscribe. Checking current
    state first catches that. Only unambiguous cases: a close is 'already
    done' if nothing matches; a filtered workspace wait is done if that
    workspace is active. An UNFILTERED workspace wait means 'the next
    switch, whatever it is': the current workspace always exists, so
    pre-checking it would answer instantly without ever blocking."""
    if event == "window_close" and needle:
        for c in hyprctl.query("clients"):
            hay = f"{c.get('address', '')} {c.get('class', '')} {c.get('title', '')}".lower()
            if needle in hay:
                return None  # still open, wait for the real event
        return {"event": "closewindow", "already": True}
    if event == "workspace" and needle:
        ws = hyprctl.query("activeworkspace") or {}
        name = str(ws.get("name", ""))
        if needle in name.lower():
            return {"event": "workspace", "name": name, "already": True}
    # layer waits are the classic late-subscribe case: use_bind returns,
    # the launcher maps instantly, and only then does the agent call
    # wait_for(layer_open); without this check that wait can never succeed.
    # Filtered only, like workspace: some layer (a bar) is always mapped,
    # so an unfiltered pre-check would answer instantly without waiting.
    if event in ("layer_open", "layer_close") and needle:
        mapped = next(
            (
                s
                for s in hyprctl.parse_layers(hyprctl.query("layers"))
                if needle in s.get("namespace", "").lower()
            ),
            None,
        )
        if event == "layer_open" and mapped is not None:
            return {"event": "openlayer", "namespace": mapped["namespace"], "already": True}
        if event == "layer_close" and mapped is None:
            return {"event": "closelayer", "already": True}
    return None


@mcp.tool()
def wait_for(event: str, match: str = "", timeout_s: float = 10) -> dict[str, Any] | str:
    """Block until a desktop event happens (real compositor events, not
    polling). event: 'window_open' | 'window_close' | 'workspace' |
    'title_change' | 'layer_open' | 'layer_close' (layer-shell surfaces:
    launchers, notification popups; match on the namespace, e.g. 'wofi') |
    'urgent' (a window demands attention) | 'screencast' (screen sharing
    started/stopped). match: optional case-insensitive substring filter
    over the event's fields (class/title/workspace name/address/namespace).
    timeout_s 1-60, default 10. Returns the event payload, or a timeout
    note. Use it after actions with delayed effects: app startups, page
    loads that change a window title, a launcher bind that pops a layer."""
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


_SEQ_HANDLERS = {
    "pointer": pointer,
    "keyboard": keyboard,
    "hypr": hypr,
    "wait_for": wait_for,
    "click_ui": click_ui,
}

# Only STRUCTURAL changes invalidate a half-run plan. Focus changes
# (activewindow/activewindowv2) and title updates are intentionally NOT
# watched: a normal click focuses a window, so watching focus events would
# abort the sequence on its own clicks. The consequence is that a bare
# focus steal is NOT caught; a keyboard step's window= (which focuses
# first) is the reliable guard against typing into the wrong window.
# Layer events count only for kinds that grab the keyboard (a launcher or
# lock screen popping up mid-sequence would swallow the next keystrokes);
# notification popups and bars are noise, not a takeover.
_WATCHED_EVENTS = {
    "openwindow", "closewindow", "movewindow", "workspace", "openlayer", "closelayer",
}

_FOCUS_STEALING_LAYERS = {"launcher", "lock", "osk"}


def _structural(name: str, payload: dict[str, Any]) -> bool:
    if name in ("openlayer", "closelayer"):
        return hyprctl.layer_kind(payload.get("namespace", "")) in _FOCUS_STEALING_LAYERS
    return name in _WATCHED_EVENTS

_SEQ_MAX_STEPS = 20
_SEQ_SETTLE = 0.2  # between-step window to let a structural change surface
_SEQ_BUDGET = 30.0  # total wall-clock ceiling so a sequence cannot hold the seat


def _event_signature(name: str, payload: dict[str, Any]) -> str:
    """A watched event identified by type AND target, so the human doing
    the same KIND of action to a DIFFERENT target (switching to another
    workspace, moving another window) is not mistaken for the step's own
    expected change."""
    if name == "workspace":
        return f"workspace:{payload.get('name', '')}"
    if name in ("openwindow", "closewindow", "movewindow"):
        return f"{name}:{payload.get('address', '')}"
    if name in ("openlayer", "closelayer"):
        return f"{name}:{payload.get('namespace', '')}"
    return name


def _step_expected_signatures(step: dict[str, Any]) -> set[str]:
    """Watched-event signatures a hypr step is expected to cause, so they
    do not count as the desktop changing under the sequence."""
    if step.get("op") != "hypr":
        return set()  # pointer/keyboard: any structural event is a real change
    action = step.get("action", "")
    if action == "workspace":
        return {f"workspace:{step.get('workspace', '')}"}
    if action == "move_window":
        return {f"movewindow:{step.get('target', '')}"}
    if action == "close_window":
        return {f"closewindow:{step.get('target', '')}"}
    return set()  # focus_window/fullscreen/floating cause only unwatched focus churn


def _step_may_switch_workspace(step: dict[str, Any]) -> bool:
    """Whether a step can pull the compositor to another workspace by
    focusing a window that lives there. Focusing a window on a non-visible
    workspace switches to it, emitting a `workspace` event that is the
    sequence's OWN doing, not a human takeover."""
    op, action = step.get("op"), step.get("action", "")
    if op == "hypr":
        return action in ("workspace", "focus_window", "fullscreen")
    if op == "keyboard":
        return bool(step.get("window"))  # window= focuses the target first
    return op == "click_ui"  # click_ui always focuses its target window first


def _step_wait_names(step: dict[str, Any]) -> set[str]:
    """Watched event NAMES an upcoming wait_for step will consume: a change
    the sequence explicitly plans to wait for (click a launcher, then wait
    for its window) is expected, not an abort. Matched by type because the
    target address is not known ahead of time."""
    if step.get("op") != "wait_for":
        return set()
    return set(_WAIT_EVENTS.get(step.get("event", ""), set())) & _WATCHED_EVENTS


def _dispatch_step(step: dict[str, Any]) -> Any:
    op = step.get("op")
    handler = _SEQ_HANDLERS.get(op)
    if handler is None:
        raise ValueError(f"unknown step op {op!r}: {'|'.join(_SEQ_HANDLERS)}")
    # `then` is handled once for the whole sequence, never per step
    params = {k: v for k, v in step.items() if k not in ("op", "then")}
    try:
        return handler(**params)
    except TypeError as exc:
        raise ValueError(f"bad args for {op!r} step: {exc}") from exc


def _unexpected(drained, expected_sigs, wait_names):
    """Watched events that are neither caused by the prior step nor awaited
    by the next one."""
    return [
        (n, p)
        for n, p in drained
        if _structural(n, p)
        and _event_signature(n, p) not in expected_sigs
        and n not in wait_names
    ]


def _seq_wait_for(
    stream: events.EventStream,
    backlog: list[tuple[str, dict[str, Any]]],
    step: dict[str, Any],
    budget_left: float,
) -> dict[str, Any] | str:
    """A wait_for step inside a sequence, satisfied from the sequence's OWN
    event stream. The between-step settle drain has already consumed any
    event that fired during the previous step, so a fresh EventStream (what
    the standalone tool opens) could never see it and a fast app would
    always 'time out'. Check the drained backlog first, then keep listening
    on the same connection."""
    event = step.get("event", "")
    names = _WAIT_EVENTS.get(event)
    if names is None:
        raise ValueError(f"unknown event {event!r}: {'|'.join(_WAIT_EVENTS)}")
    safety.touch(f"wait_for:{event}")
    needle = str(step.get("match", "")).lower()
    timeout_s = min(max(float(step.get("timeout_s", 10)), 1.0), 60.0, max(budget_left, 1.0))

    already = _already_satisfied(event, needle)
    if already is not None:
        return already

    def matcher(_name: str, payload: dict[str, Any]) -> bool:
        if not needle:
            return True
        return needle in " ".join(str(v) for v in payload.values()).lower()

    for i, (n, p) in enumerate(backlog):
        if n in names and matcher(n, p):
            del backlog[i]
            return {"event": n, **p}
    try:
        hit = stream.wait_for(names, matcher, timeout_s)
    except events.EventError as exc:
        return f"event socket unavailable: {exc}"
    if hit is None:
        return f"timeout: no matching {event} event within {timeout_s:.0f}s"
    return {"event": hit[0], **hit[1]}


def sequence(
    steps: list[dict[str, Any]], stop_on_change: bool = True, then: str = "desktop"
) -> list[Any] | str:
    """Run an ordered list of actions in ONE call, so a click/type/enter
    micro-sequence costs one round-trip instead of several. Each step is
    {"op": "pointer"|"keyboard"|"click_ui"|"hypr"|"wait_for", ...that tool's
    args}, e.g. [{"op":"pointer","action":"click","x":800,"y":60},
    {"op":"keyboard","action":"type","text":"hello","window":"0x.."},
    {"op":"keyboard","action":"key","keys":"enter"}]. With stop_on_change
    (default) the run stops, best-effort, when it notices a STRUCTURAL
    change between steps that the step did not intend: a window opening
    (e.g. a dialog), closing, or moving, a switch to an unexpected
    workspace, or a keyboard-grabbing layer surface (a launcher or lock
    screen) appearing, so later steps do not act on stale state.
    Notification popups and bars are not treated as changes. It does NOT catch
    a bare focus change, so to type into a specific window reliably give
    that keyboard step a window= address (it focuses first). Bounded to 20
    steps and ~30s total. `then` observes the final state ('desktop'
    default, 'screenshot', 'ui', 'none')."""
    safety.touch("sequence")
    if not steps:
        raise ValueError("sequence needs at least one step")
    if len(steps) > _SEQ_MAX_STEPS:
        raise ValueError(f"sequence too long ({len(steps)} steps, max {_SEQ_MAX_STEPS})")

    stream = None
    if stop_on_change:
        with contextlib.suppress(events.EventError):
            stream = events.EventStream()

    results: list[str] = []
    stopped: str | None = None
    prev_expected: set[str] = set()
    backlog: list[tuple[str, dict[str, Any]]] = []  # drained events a wait step may consume
    deadline = time.monotonic() + _SEQ_BUDGET
    try:
        for i, step in enumerate(steps):
            if stream is not None and i > 0:
                drained = stream.drain(_SEQ_SETTLE)
                # wait-consumable events are only THIS window's, minus what
                # the sequence itself caused: REPLACING (not extending)
                # keeps a later wait from being served a stale event from
                # two steps ago, and an own-change the abort check excused
                # (prev_expected) must not double as a wait hit
                backlog[:] = [
                    (n, p) for n, p in drained if _event_signature(n, p) not in prev_expected
                ]
                changed = _unexpected(drained, prev_expected, _step_wait_names(step))
                if changed:
                    names = ", ".join(sorted({n for n, _ in changed}))
                    stopped = f"desktop changed ({names}) before step {i}"
                    break
            budget_left = deadline - time.monotonic()
            if budget_left <= 0:
                stopped = f"time budget ({_SEQ_BUDGET:.0f}s) reached before step {i}"
                break
            run = dict(step)
            if step.get("op") == "wait_for":  # never let one wait outlast the budget
                run["timeout_s"] = min(float(step.get("timeout_s", 10)), max(budget_left, 1.0))
            try:
                if step.get("op") == "wait_for" and stream is not None:
                    res = _seq_wait_for(stream, backlog, run, budget_left)
                else:
                    res = _dispatch_step(run)
            except Exception as exc:
                results.append(f"[{i}] {step.get('op')}: ERROR {exc}")
                stopped = f"step {i} raised: {exc}"
                break
            label = step.get("action") or step.get("event") or ""
            results.append(f"[{i}] {step.get('op')} {label}: {res}".rstrip())
            prev_expected = _step_expected_signatures(step)
            if _step_may_switch_workspace(step):
                # A step that focuses a window (or `hypr workspace` with a
                # relative/alias target like '+1') lands on a workspace whose
                # emitted name we cannot predict from the arguments; ask the
                # compositor what our own action landed on and excuse it, so
                # the drain does not mistake our focus-induced switch for a
                # human takeover.
                with contextlib.suppress(Exception):
                    ws = hyprctl.query("activeworkspace") or {}
                    prev_expected.add(f"workspace:{ws.get('name', '')}")
    finally:
        if stream is not None:
            # the last step can change the desktop too; a `then` observation
            # would show it, but report it explicitly so nothing is masked
            if stopped is None:
                tail = _unexpected(stream.drain(_SEQ_SETTLE), prev_expected, set())
                if tail:
                    names = ", ".join(sorted({n for n, _ in tail}))
                    stopped = f"desktop changed ({names}) after the last step"
            stream.close()

    ran = len(results)
    if stopped is None:
        head = f"sequence: all {ran}/{len(steps)} steps ran\n" + "\n".join(results)
    else:
        head = f"sequence: stopped after {ran}/{len(steps)} steps, {stopped}\n" + "\n".join(results)
    return _acted(head, then)


# Acting tools register only outside read-only mode; observation tools
# (desktop, screenshot, zoom, ui, marks, binds, wait_for) are decorated
# above and always on. Clipboard is double-gated: opt-in env, never read-only.
if not READONLY:
    for _acting_tool in (pointer, keyboard, click_ui, hypr, launch, use_bind, sequence):
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
    safety.on_shutdown(hinput.release_held)  # kill switch mid-drag: release first
    trust.init_marking()  # HYPRUSE_MARK: install the agent-owned border rule
    mcp.run()


if __name__ == "__main__":
    main()
