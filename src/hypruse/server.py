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
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hypruse import __version__, a11y, events, hyprctl, journal, keymap, safety, session, trust
from hypruse import background as bg
from hypruse import clipboard as clip
from hypruse import input as hinput
from hypruse import screenshot as shot

INSTRUCTIONS = """\
hypruse controls a live Hyprland desktop. Workflow: call `desktop` first
and prefer `hypr`/`launch` (IPC, instant and exact) for anything window-
or workspace-shaped; use `screenshot` + `pointer`/`keyboard` only to see
and operate inside application windows. Launchers, bars, notification
popups, and on-screen keyboards are not windows: `desktop` reports them
under `layers`. A listed layer is one the compositor tracks, not one you
can see: it may be transparent or dormant, so `screenshot` when visibility
matters. To CLICK a named control inside an app, call `click_ui` FIRST:
for GTK/Qt apps it resolves the control in the accessibility tree and
clicks it in ONE call, no image and no pixel estimation (`ui` lists the
controls and their current values without clicking; `marks` returns a
numbered screenshot plus legend when you want to SEE what is clickable,
then `click_ui(mark=N)`). Use screenshot + zoom when the tree exposes
nothing (terminals, canvas UIs, Electron/Chrome without a flag). To
verify an effect without a second round-trip, pass `then='desktop'` (a
fresh snapshot), `then='ui'` (controls with current values, cheapest
after typing or toggling: the focused window, or for `click_ui` the
window it just clicked), or `then='screenshot'` to
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
finish what you start, and expect every action on their screen to be
visible. Before typing, pass `keyboard(window=<address>)` so keystrokes
never land in the wrong window."""

BACKGROUND_AUTO = """
Background mode is automatic. An action on the window the human is in
runs on their screen, as above. An action on any other window runs in the
background, and the human's cursor, keyboard focus and workspace stay
where they are: `click_ui` presses the control through its accessibility
action, `keyboard(window=...)` types through the accessibility interface
or sends the keys to that window alone, and `launch` with no workspace
opens the app on one of the agent's own workspaces ({workspaces}), out of
the human's sight. Each result says which method it used and whether the
human's focus stayed put; a check box, slider or field is read back, and
a WARNING means the human's focus moved during it, so stop and say so. Check
a button press or keys with `ui`. The pointer cannot work in the background,
so it is refused over any window but the human's: press controls there
with `click_ui`. A hidden window's screenshot can be stale (many apps stop
drawing while hidden), so read its state with `ui`. When something cannot
be done in the background, the refusal says why. `background=false` does
it on the human's screen instead and takes it over, so pass that only
when the human expects it."""

BACKGROUND_GUARANTEE = """
Background mode is GUARANTEED for this session (HYPRUSE_BACKGROUND=1).
You work only on windows on the agent's own workspaces ({workspaces}),
and nothing you do may move the human's cursor, take their keyboard focus
or switch their workspace. `launch` opens apps there, `click_ui` presses
controls through the accessibility tree, and `keyboard` needs window= and
types into that window in the background. The pointer, workspace
switches, focus_window, fullscreen and use_bind are refused, and
background=false cannot override that. `launch` still runs its command as
given, so never launch a command that drives the desktop itself, and an
app that is already running (a browser) opens its window on the human's
screen first: start a separate instance. A result with a WARNING means
the human's focus moved: stop and tell them. A hidden window's screenshot
can be stale (many apps stop drawing while hidden), so read its state
with `ui`. When something cannot be done, ask the human."""

BACKGROUND_OFF = """
Background mode is off for this session (HYPRUSE_BACKGROUND=0): every
action runs on the human's screen unless you pass background=true."""


def _background_instructions() -> str:
    """The background paragraph for this session's HYPRUSE_BACKGROUND."""
    try:
        mode = bg.mode()
    except trust.TrustError:
        return ""  # every action refuses with the reason until it is fixed
    if mode == bg.OFF:
        return BACKGROUND_OFF
    raw = os.environ.get("HYPRUSE_AGENT_WORKSPACES", "").strip()
    workspaces = f"HYPRUSE_AGENT_WORKSPACES={raw}" if raw else "workspaces 99 down to 90"
    text = BACKGROUND_GUARANTEE if mode == bg.GUARANTEE else BACKGROUND_AUTO
    return text.format(workspaces=workspaces)

READONLY_INSTRUCTIONS = """\
hypruse is attached to a live Hyprland desktop in READ-ONLY MODE: the
user enabled observation only, so the tools listed here are the whole
surface. hypruse cannot move the cursor, type, press keys, run
programs, or rearrange anything in this mode; the human at the desk
drives, hypruse watches and reports.

Workflow: call `desktop` first for the semantic state: monitors,
workspaces, windows (address, class, title, `at` + `size` in global
coords), the active window, cursor, and `layers` (launchers, bars,
notification popups, and on-screen keyboards are not windows and
appear only there). A listed layer is one the compositor tracks, not one
you can see: it may be transparent or dormant, so confirm with a
`screenshot` rather than inferring visibility from presence. To read what
an app shows, prefer `ui` (its accessibility tree: control names with
CURRENT values, a few hundred exact tokens and no image) when the app
exposes one; `marks` adds a numbered screenshot plus legend when you want
to SEE the controls. Use
`screenshot` (focused monitor, a window, or a region) and `zoom` (a
native-resolution crop around a point) when the tree exposes nothing
(terminals, canvas UIs, Electron/Chrome without a flag). `binds`
lists the owner's own keybinds, useful for understanding their
workflows. To notice a change (an app starting, a title updating, a
layer appearing), block on `wait_for` instead of re-capturing in a
loop.

Coordinates: one space everywhere, Hyprland global logical pixels
(window `at`, cursor). Screenshots are pixel space: map back with
global = geometry[:2] + image_pixel / scale, using the metadata's
`scale` and `image` [w,h] (they already account for any downscaling).
When screenshot returns a file path instead of an image, read that
file."""

DRYRUN_NOTE = """\

DRY RUN IS ON for this session (HYPRUSE_DRYRUN). Every acting tool still
validates its arguments and runs every trust guard, then reports what it
WOULD have done and delivers nothing: no click, no keystroke, no window
op reaches the desktop. Results say so, and the screen will not change,
so do NOT retry an action because "it did not work". Plan the work and
report the plan; the human replays it when they are satisfied with it."""

READONLY = os.environ.get("HYPRUSE_READONLY", "").lower() in ("1", "true", "yes", "on")
CLIPBOARD = os.environ.get("HYPRUSE_CLIPBOARD", "").lower() in ("1", "true", "yes", "on")

_instructions = READONLY_INSTRUCTIONS if READONLY else INSTRUCTIONS + _background_instructions()
if journal.dry_run() and not READONLY:  # read-only has nothing to simulate
    _instructions += DRYRUN_NOTE

# Content blocks. The MCP types are pydantic models that `mcp.types` builds
# at import, and that import is most of a shell verb's startup time, so the
# tools never name them: they ask for a block, and the CLI, which only ever
# prints text and file paths, gets a plain one instead.


class Block:
    """A content block for a caller that does not speak MCP (the CLI verbs):
    the fields the MCP types carry, and nothing imported to carry them."""

    __slots__ = ("type", "text", "data", "mimeType")

    def __init__(self, type: str, text: str = "", data: str = "", mimeType: str = "") -> None:
        self.type, self.text, self.data, self.mimeType = type, text, data, mimeType

    def __repr__(self) -> str:
        return f"Block({self.type!r}, {self.text[:40]!r})"


_plain_blocks = False


def use_plain_blocks() -> None:
    """Return Block instead of mcp.types objects from here on (the CLI)."""
    global _plain_blocks
    _plain_blocks = True


def _text(text: str) -> Any:
    if _plain_blocks:
        return Block("text", text=text)
    from mcp.types import TextContent

    return TextContent(type="text", text=text)


def _image(*, data: str, mimeType: str) -> Any:
    if _plain_blocks:
        return Block("image", data=data, mimeType=mimeType)
    from mcp.types import ImageContent

    return ImageContent(type="image", data=data, mimeType=mimeType)


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


@journal.journaled("observe")
def desktop() -> dict[str, Any]:
    """Semantic desktop snapshot: monitors, workspaces, windows (address,
    class, title, `at` + `size` in global coords), active window, cursor,
    and `layers`: launchers (wofi/rofi), bars, notification popups, and
    on-screen keyboards are NOT windows and appear only there, with a
    best-effort `kind` and global geometry you can screenshot by region or
    click into. A listed layer is one the compositor tracks, not one you
    can see: it may be transparent or dormant, so screenshot when
    visibility matters. Call first; act on the addresses it returns."""
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
        image_block = _image(data=base64.b64encode(data).decode(),
            mimeType=f"image/{meta['format']}",
        )
        return [image_block, _text(json.dumps(meta))]
    d = _runtime_dir()
    path = d / f"shot-{int(time.time() * 1000)}.{meta['format']}"
    path.write_bytes(data)
    _prune_shots(d)
    meta["path"] = str(path)  # the CLI reads it from here, not from the sentence
    return [
        _text(f"screenshot saved, read this file to view the screen: {path}"
        ),
        _text(json.dumps(meta)),
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


@journal.journaled("observe")
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
    with geometry/scale for pixel→global mapping. A window on a hidden
    workspace or under another window is still captured as itself
    (metadata `source` "window"; without grim -T a shown window is cropped
    from the screen, `source` "screen", and a hidden one is refused);
    `visible` says whether the compositor draws it, and
    a hidden one may show how it last looked (many apps stop drawing while
    hidden). Window captures are refused while the session is locked. `scale` 0.1-1.0:
    optional deliberate downscale, usually leave unset. `stable=true`
    waits (up to 2s) until two consecutive frames match, so a capture
    right after an action is not taken mid-animation; metadata gains
    `stable`. Captures are fast JPEG by default; `lossless=true` returns
    PNG for pixel-exact work. Before clicking a small control, follow with
    `zoom` at the estimated point."""
    safety.touch("screenshot")
    return _deliver_capture(window, region, scale=scale, stable=stable, lossless=lossless)


@journal.journaled("observe")
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
    (an address from desktop, on screen) clamps to that window instead. The metadata
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


def _active_address() -> str | None:
    """The address of the window the human is in, or None."""
    with contextlib.suppress(Exception):
        return (hyprctl.query("activewindow") or {}).get("address")
    return None


def _active_client() -> dict[str, Any] | None:
    """The focused window's client, or None when nothing is focused (a bare
    key press must still work). Used by the trust guards."""
    with contextlib.suppress(Exception):
        return _resolve_window("active")
    return None


def _guarded_active_client() -> dict[str, Any] | None:
    """The focused client for a guard that must fail CLOSED. Returns None
    only when nothing is genuinely focused (a bare key must still work);
    an IPC error propagates instead of being swallowed, so confinement is
    never skipped just because hyprctl could not be read (the virtual
    keyboard delivers keys regardless). This is the difference between
    'no active window' and 'the compositor is unreadable'."""
    try:
        return _resolve_window("active")
    except ValueError:
        return None  # genuinely nothing focused; a bare key still works
    # HyprctlError (IPC down/timeout) propagates: the guard fails closed


def _peers(client: dict[str, Any]) -> list[tuple[str, tuple[int, int]]] | None:
    """(title, size) of the other Hyprland windows of the client's process,
    or None when the client list cannot be read."""
    with contextlib.suppress(Exception):
        return [
            (c.get("title", ""), tuple(c.get("size") or (0, 0)))
            for c in hyprctl.query("clients")
            if c.get("pid") == client.get("pid") and c.get("address") != client.get("address")
        ]
    return None


def _proven_frame(
    bus: a11y.Bus, client: dict[str, Any], app: tuple[str, str] | None
) -> tuple[str, str] | None:
    """The accessibility toplevel that is provably this client's window, or
    None. The root must belong to the window's own app instance (a
    multi-process app is matched by title across every process), and the
    frame must be told apart from the app's other windows."""
    if app is None or not a11y.same_app(bus.conn_pid(app[0]), client.get("pid")):
        return None
    peers = _peers(client)
    if peers is None:
        return None
    return a11y.window_frame_for(
        bus, app[0], app[1], client.get("title", ""), tuple(client["size"]), peers)


_UNSURE = ("its accessibility tree cannot tell which window is {addr} (another window of "
           "{cls} could match it, or its accessibility connection is another process's)")


def _ui_read(
    window: str = "", name: str = "", actionable: bool = True, handles: bool = False
) -> list[Any] | str:
    """Shared body of the `ui` tool: a window's accessible elements with
    global click points and current values, or a fall-back-to-vision
    message. Reused by `then='ui'` fusion and click-by-name resolution."""
    client = _resolve_window(window)
    title = client.get("title", "")
    cls = client.get("class", "the window")
    try:
        bus = a11y.connect()
        app = a11y.app_for_pid(bus, client.get("pid"), title, tuple(client["size"]))
        if app is None:
            return f"{cls} exposes no accessibility tree; use screenshot + zoom instead"
        frame = a11y.window_frame(bus, app[0], app[1], title, tuple(client["size"]))
        # a background press acts on the control itself, and a reading after
        # one must be of this window, not a same-titled one: prove the frame
        exact = _proven_frame(bus, client, app)
        frame = exact or frame
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
        for key in ("value", "percent", "checked", "action"):  # only where it applies
            if key in e:
                item[key] = e[key]
        if handles and exact is not None:  # what a background press needs
            item["_svc"], item["_path"] = e["svc"], e["path"]
            item["_field"] = bool(e.get("field"))
        elif handles:  # another window of the app could own this control
            item["_unsure"] = True
        out.append(item)
    # a name filter reads the same elements, so it is no way past the cut
    cut = (f"it stopped after reading {a11y.WALK_BUDGET} elements of a large tree, so "
           f"{'a match' if name else 'more controls'} may lie beyond them")
    if not out:
        what = f"matching {name!r}" if name else "actionable"
        tail = f" ({cut})" if truncated else ""
        return f"no {what} elements in {cls}{tail}"
    if exact is None and not handles:
        out.append({"note": "these controls may be another window's: "
                            + _UNSURE.format(addr=client["address"], cls=cls)})
    if truncated and not handles:
        out.append({"note": cut})
    return out


@journal.journaled("observe")
def ui(window: str = "", name: str = "", actionable: bool = True) -> list[Any] | str:
    """Read a window's accessibility tree (AT-SPI) and return its elements
    with GLOBAL click points, so you can target a control by NAME with no
    screenshot and no pixel guessing. `window` is an address from desktop
    (default: the focused window). `name` filters to elements whose
    accessible name contains it (case-insensitive); `actionable` (default)
    keeps only interactive roles (buttons, entries, menu items, ...); with
    a `name` that matches none of those, it keeps named elements in other
    roles that have an accessibility action (a web app's list row), with
    the action's name as `action`.
    Returns [{role, name, x, y, clickable}] where x,y is the click point:
    focus the window, then click it with `pointer` (the window must be
    visible to receive the click, and focusing can move it, so read again
    after focusing), or do both in one call with `click_ui`.
    Controls that carry a CURRENT VALUE also
    report it: `value` (text typed into an entry, or a slider/spinner
    number), `percent` for a slider's position, `checked` for a box or
    toggle. Password fields never report contents, and many dropdowns
    expose no value at all, so read the screen with screenshot when a
    rendered value matters. Not every app exposes a tree (terminals, and
    Electron/Chrome without --force-renderer-accessibility, expose little
    or nothing); when it does not, fall back to screenshot + zoom."""
    safety.touch("ui")
    view = _with_hidden_note(window, _ui_read(window, name, actionable))
    trust.remember_seat()  # a fresh read re-arms the strict seat guard
    return view


def _with_hidden_note(window: str, view: list[Any] | str) -> list[Any] | str:
    """A ui view, plus a note when the window is not on screen. Its points
    are where its controls sit inside it, and the screen at those points
    shows something else, usually the human's own window, which a pointer
    click there would hit. Background mode makes such windows common."""
    if isinstance(view, list):
        with contextlib.suppress(Exception):
            client = _resolve_window(window)
            monitors, clients = hyprctl.batch_query(["monitors", "clients"])
            reason = shot._hidden_reason(client, monitors, clients)
            if reason is not None:
                tail = "." if READONLY else (
                    ", so press its controls with click_ui; a pointer click at these "
                    "points would land on whatever is on screen there"
                )
                view.append({"note": f"{client['address']} is not on screen ({reason}): "
                                     f"these points are inside it, not on what the human "
                                     f"sees{tail}"})
    return view


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
# Offsets are WINDOW-RELATIVE and re-anchored to the window's position
# when click_ui resolves the mark, so a window moved since the capture does
# not invalidate them; one that moves when focused for the click is
# refused, since its controls may have reflowed.
_last_marks: dict[str, Any] = {}


def marks_state() -> dict[str, Any]:
    """The numbering the next click_ui(mark=N) resolves against; cli_state
    carries it across one-shot CLI processes."""
    return _last_marks


def restore_marks(state: Any) -> None:
    """Load what marks_state returned after a JSON round-trip (mark numbers
    come back as string keys). Anything malformed means no marks."""
    global _last_marks
    _last_marks = {}
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        return
    items: dict[int, dict[str, Any]] = {}
    for key, item in state["items"].items():
        with contextlib.suppress(ValueError, TypeError):
            if isinstance(item, dict) and {"dx", "dy", "label"} <= set(item):
                items[int(key)] = item
    if items and isinstance(state.get("window"), str):
        _last_marks = {"window": state["window"], "items": items}
        if isinstance(state.get("class"), str):
            _last_marks["class"] = state["class"]
        size = state.get("size")
        if isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) for v in size):
            _last_marks["size"] = size
        # a record without a timestamp is treated as expired, never as fresh
        ts = state.get("ts")
        _last_marks["ts"] = float(ts) if isinstance(ts, int | float) else 0.0


# how long a marks numbering stays clickable; the CLI persists it, and a
# number an agent copied from yesterday must not become a click today
_MARKS_TTL_S = 600.0


@journal.journaled("observe")
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
    elements = _ui_read(addr, name=name, handles=True)
    if isinstance(elements, str):
        return elements
    data, meta = _grab_env(window=addr, stable=True)
    trust.notify_capture()  # marks IS a capture: same HYPRUSE_MARK notice
    trust.remember_seat()  # and the same strict-guard re-arm as screenshot
    ox, oy = meta["geometry"][0], meta["geometry"][1]
    scale = meta["scale"]
    ax, ay = client["at"]
    points: list[tuple[int, int, int]] = []
    legend: list[dict[str, Any]] = []
    stored: dict[int, dict[str, Any]] = {}
    for i, e in enumerate(elements, start=1):
        points.append((i, round((e["x"] - ox) * scale), round((e["y"] - oy) * scale)))
        legend.append({"mark": i, **{k: v for k, v in e.items() if not k.startswith("_")}})
        stored[i] = {"dx": e["x"] - ax, "dy": e["y"] - ay,
                     "label": f"{e['role']} {e['name']!r}", "role": e["role"]}
        if "_svc" in e:  # a background press needs the control itself, not its point
            stored[i]["svc"], stored[i]["path"] = e["_svc"], e["_path"]
            stored[i]["field"] = e.get("_field", False)
        elif e.get("_unsure"):
            stored[i]["unsure"] = True
    global _last_marks
    # class and time travel with the numbering: a later click_ui(mark=N)
    # checks both, since the CLI carries this record across processes and an
    # address can be handed to a different window later
    _last_marks = {
        "window": addr, "class": client.get("class", ""), "ts": time.time(), "items": stored,
        "size": list(client.get("size") or []),  # a resized window invalidates the offsets
    }
    hint = (
        "numbered marks match the legend entries"
        if READONLY  # click_ui does not exist in read-only mode
        else "click_ui(mark=N) clicks a numbered mark"
    )
    legend_text = _text(json.dumps({"legend": legend, "hint": hint}),
    )
    marked = _draw_marks(data, meta["format"], points)
    if marked is None:
        return [
            _text("ImageMagick not found, returning the legend only (its "
                "coordinates are exact; install imagemagick for marked captures)",
            ),
            legend_text,
        ]
    meta["target"] = "marks"
    return [*_package(marked, meta), legend_text]


_OBSERVE_MODES = ("none", "desktop", "screenshot", "ui")


def _quiet_clients() -> list[dict[str, Any]]:
    try:
        return hyprctl.query("clients")
    except Exception:
        return []


def _acted(msg: str, then: str, window: str = "", fallback: bool = True) -> list[Any] | str:
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
    head = _text(msg)
    if then == "desktop":
        return [head, _text(json.dumps(hyprctl.snapshot()))]
    if then == "screenshot":
        try:
            return [head, *_deliver_capture(stable=True)]
        except Exception as exc:  # the observation must never mask the action's success
            return [head, _text(f"screenshot failed: {exc}")]
    if then == "ui":
        try:
            view = _with_hidden_note(window, _ui_read(window))
        except Exception as exc:  # the observation must never mask the action's success
            view = f"ui read failed: {exc}"
            gone = window and not any(
                c.get("address") == window for c in _quiet_clients())
            if window and not fallback and gone:
                # a background action: the focused window is the human's,
                # not the closed window's parent, so it is not shown
                view = (f"{window} is gone (the action closed it); the human's own window "
                        "is not shown, since this ran in the background")
            elif window and not fallback:
                pass  # the read failed on a window that is still there: say so
            elif window:
                # the acted-on window can be GONE: clicking Close/OK/Discard
                # destroys the very window whose controls we were asked to
                # re-read. Fall back to whatever holds focus now (usually
                # the parent), which is what the agent actually wants to see.
                with contextlib.suppress(Exception):
                    view = _ui_read()
        payload = view if isinstance(view, str) else json.dumps(view)
        return [head, _text(payload)]
    raise ValueError(f"unknown then {then!r}: {'|'.join(_OBSERVE_MODES)}")


# Every dry-run result opens with this, so an agent reading its own tool
# output cannot mistake a simulated call for one that happened.
_DRY = "DRY RUN, nothing was delivered: would"


def _at(x: float | None, y: float | None) -> str:
    """A point for a dry run's plan. Omitted coordinates mean the action
    lands wherever the cursor is when it runs, which is deliberately NOT
    resolved to numbers here: reporting today's cursor position as the
    plan's target would be a guess, not a prediction."""
    if x is None or y is None:
        return "the cursor"
    return f"({x:.0f}, {y:.0f})"


def _layer_note(x: float | None, y: float | None) -> str:
    """A warning to append when a focus-stealing layer surface covers the
    point a pointer action lands on: the layer receives (or swallows) the
    input, so a silent 'ok' would misreport what happened to the window
    beneath. The action itself is not refused, because clicking INTO a
    launcher is the legitimate way to drive one and pointer cannot know
    the intent; click_ui, whose target is a window control, refuses."""
    if x is None or y is None:
        try:
            x, y = hyprctl.cursor_pos()
        except Exception:
            return ""
    covering = trust.covering_layer(x, y)
    if covering is None:
        return ""
    return (
        f"; NOTE: the {covering['kind']} layer {covering['namespace']!r} covers "
        "this point, so the input went to it, not to any window beneath"
    )


_POINTER_INSTEAD = (
    "to press a named control in another window, use click_ui with window= (it "
    "works there through the accessibility tree, without the pointer)"
)


def _pointer_policy(
    action: str,
    x: float | None,
    y: float | None,
    to_x: float | None,
    to_y: float | None,
    background: bool | None,
) -> None:
    """Background mode for the pointer. It moves the human's real cursor,
    so it never runs in the background: the guarantee and background=true
    refuse it, and automatic mode allows it only where the input lands on
    the window the human is in or on no window at all (a bar, a launcher).
    A move counts too, since focus follows the mouse by default."""
    if background is None and bg.mode() == bg.AUTO:
        points = [(x, y)] + ([(to_x, to_y)] if action == "drag" else [])
        active = _active_address()
        for px, py in points:
            if px is None or py is None:
                if action in ("move", "drag"):
                    continue  # the argument check below refuses this
                try:  # a click or scroll in place lands at the cursor
                    px, py = hyprctl.cursor_pos()
                except Exception:
                    continue
            other = trust.other_window_at(px, py, active)
            if other is not None:
                bg.refuse(
                    f"({px:.0f}, {py:.0f}) is on {other.get('class') or '?'} "
                    f"({other.get('address')}), not the window the human is in, and the "
                    "pointer would move their cursor and take their focus",
                    _POINTER_INSTEAD,
                )
        return
    if bg.decide(True, background):
        bg.refuse("the pointer moves the human's real cursor", _POINTER_INSTEAD)


@journal.journaled("act")
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
    background: bool | None = None,
) -> list[Any] | str:
    """Mouse in global coordinates. action='move' (x,y) | 'click' (optional
    x,y first; button left/right/middle; double=true) | 'drag' (x,y →
    to_x,to_y holding button) | 'scroll' (scroll_dy notches, positive =
    content down; optional x,y first). `then` appends the result to this
    call so you skip a round-trip: 'desktop' a fresh snapshot, 'screenshot'
    a stable capture, 'ui' the focused window's elements with current
    values, 'none' (default) nothing. `allow_auth=true` overrides the
    refusal to click over a system authentication dialog. The pointer is
    the human's own cursor, so it never runs in the background: it is
    refused where the input would land on a window other than the one the
    human is in (press controls there with click_ui), unless
    `background=false`."""
    safety.touch(f"pointer:{action}")
    trust.guard_seat()
    if action in ("move", "click", "drag", "scroll"):  # else the ValueError below
        _pointer_policy(action, x, y, to_x, to_y, background)
    # HYPRUSE_DRYRUN runs every check below and then delivers nothing, so
    # `plan` describes what each branch was about to do. The guards stay
    # exactly where they are: a dry run whose refusals differ from the
    # real one would be worth nothing.
    dry = journal.dry_run()
    plan = ""
    note = ""
    if action != "move":
        # a locked session routes every event to its credential prompt, so
        # no window can receive this (a bare click can legitimately AIM at
        # the prompt, so window_given=False: note on allow_auth, else
        # refuse); a move only shifts the cursor. A lock DOMINATES the
        # layer note below: when locked, the input never reached any layer.
        note = trust.guard_session_lock(False, allow_auth)
    # The input layer's own argument checks run HERE, not only inside the
    # hinput calls below, because a dry run stops before those and a
    # rehearsal that accepts a call the real run rejects is worse than no
    # rehearsal. Same functions, so the errors are identical either way.
    if action == "move":
        # a move only repositions the cursor; the input-delivering actions
        # below are the ones the confinement and auth guards gate
        if x is None or y is None:
            raise ValueError("move needs x and y")
        plan = f"move the cursor to ({x:.0f}, {y:.0f})"
        if not dry:
            hinput.move(x, y)
    elif action == "click":
        hinput.check_button(button)
        hinput.check_xy(x, y)
        trust.guard_pointer(x, y, allow_auth)  # None x/y = click at current cursor
        note = note or _layer_note(x, y)
        plan = f"{'double-' if double else ''}click {button} at {_at(x, y)}"
        if not dry:
            hinput.click(x, y, button=button, double=double)
    elif action == "drag":
        if None in (x, y, to_x, to_y):
            raise ValueError("drag needs x, y, to_x, to_y")
        hinput.check_button(button)
        trust.guard_pointer(x, y, allow_auth)
        trust.guard_pointer(to_x, to_y, allow_auth)  # the drag ends elsewhere; guard that too
        note = note or _layer_note(x, y)
        plan = f"drag {button} from {_at(x, y)} to {_at(to_x, to_y)}"
        if not dry:
            hinput.drag(x, y, to_x, to_y, button=button)  # type: ignore[arg-type]
    elif action == "scroll":
        hinput.check_scroll(scroll_dy, scroll_dx)
        hinput.check_xy(x, y)
        trust.guard_pointer(x, y, allow_auth)  # None x/y = scroll at current cursor
        note = note or _layer_note(x, y)
        plan = f"scroll dy={scroll_dy:g} dx={scroll_dx:g} at {_at(x, y)}"
        if not dry:
            hinput.scroll(dy=scroll_dy, dx=scroll_dx, x=x, y=y)
    else:
        raise ValueError(f"unknown action {action!r}: move|click|drag|scroll")
    if dry:
        return _acted(f"{_DRY} {plan}{note}", then)
    trust.remember_seat()
    return _acted(f"{action} ok; cursor now at {hyprctl.cursor_pos()}{note}", then)


@journal.journaled("act")
def keyboard(
    action: str,
    text: str = "",
    keys: str = "",
    window: str = "",
    then: str = "none",
    allow_auth: bool = False,
    background: bool | None = None,
) -> list[Any] | str:
    """Keyboard to an app. action='type' (text, unicode-safe) |
    'key' (keys combo: 'ctrl+shift+t', 'esc', 'F5'; aliases
    enter/esc/tab/backspace/pgup/pgdn/arrows, else XKB keysyms). Pass
    `window` (an address from desktop) so keystrokes land in the intended
    app rather than whatever currently holds focus. The window the human
    is in is focused and typed into on their screen. Any other window gets
    the keys in the background, with the human's focus left alone: text
    goes into its text field through the accessibility interface, or key
    by key through Hyprland's sendshortcut (a character the keyboard layout
    has no key for is refused on that path). Keys sent that way, and key
    combos, go to the control the app itself has focused, which is not the
    field text went into through accessibility: focus a field first with
    click_ui. Without `window`, keys go to
    the focused window. `background` true or false forces one way. This
    drives shortcuts the focused application handles
    (ctrl+t, ctrl+l). It does NOT trigger Hyprland's own keybinds
    (super+...): those go through `use_bind`, and workspace/window actions
    through `hypr`. `then` ('desktop'|'screenshot'|'ui'|'none') appends the
    result to this call. `allow_auth=true` overrides the default refusal to
    type into a password field or a system authentication dialog (only when
    a human intends that credential entry)."""
    safety.touch(f"keyboard:{action}")
    trust.guard_seat()
    # validate EVERY argument before any side effect: focusing the target
    # below is a visible seat change, and a call that goes on to raise
    # must not have moved the human's focus on its way out
    if action not in ("type", "key"):
        raise ValueError(f"unknown action {action!r}: type|key")
    if action == "type" and not text:
        raise ValueError("type needs text")
    if action == "key" and not keys:
        raise ValueError("key needs keys")
    if action == "key":
        # an unknown modifier used to surface from wtype AFTER the target
        # window had been focused, which is the seat change a refused call
        # must not have made; it is also what a dry run has to raise
        hinput.parse_combo(keys)
    addr_str = _addr(window) if window else ""  # validate the address format first
    if window:
        target_client = _resolve_window(window)
        if bg.decide(target_client["address"] == _active_address(), background):
            return _keyboard_in_background(target_client, action, text, keys, then, allow_auth)
    elif bg.decide(True, background):
        bg.refuse("keyboard needs window= on one of the agent's workspaces to type there")
    # a locked session eats all input, and a mapped launcher layer holds
    # the keyboard grab regardless of window focus: both refuse when that
    # breaks the caller's stated intent (a named window, or a confinement
    # scope), else report it. A lock DOMINATES a mere layer, so its note
    # (allow_auth path, windowless, unconfined) stands alone rather than
    # being joined by a keyboard-grab note about a launcher behind it.
    note = trust.guard_session_lock(bool(window), allow_auth)
    if not note:
        note = trust.guard_keyboard_layer(bool(window), allow_auth)
    # resolve the window keystrokes will land in, for the confinement and
    # auth guards, but only when a guard is active (it costs a query). When
    # no window= is given we best-effort the focused one; a bare key (esc,
    # F5) with nothing focused must still work, but a compositor we CANNOT
    # read must fail closed under an active guard (the wire delivers keys
    # even when hyprctl is down), never silently skip confinement.
    target = None
    if trust.guards_window_input():
        target = _resolve_window(window) if window else _guarded_active_client()
        if target is not None:
            trust.guard_client(target)  # confinement
            trust.guard_auth_client(target, allow_auth)
            if action == "type":
                # the a11y focused-role read is per-pid, independent of
                # compositor focus, so it runs BEFORE the focuswindow
                # dispatch: a refused call must not move the human's focus
                trust.guard_password_field(target, allow_auth)
    into = f" into {window}" if window else ""
    # focusing the target is itself a visible change to the seat, so the
    # dry run stops short of it, not just short of the keystrokes
    if journal.dry_run():
        plan = f"type {len(text)} characters" if action == "type" else f"press {keys}"
        return _acted(f"{_DRY} {plan}{into}{note}", then)
    if window:
        hyprctl.dispatch("focuswindow", addr_str)
        time.sleep(0.05)  # let keyboard focus settle before typing into it
    if action == "type":
        hinput.type_text(text)
        trust.remember_seat()
        return _acted(f"typed {len(text)} characters{into}{note}", then)
    hinput.key_combo(keys)
    trust.remember_seat()
    return _acted(f"pressed {keys}{into}{note}", then)


@journal.journaled("act")
def click_ui(
    name: str = "",
    mark: int = 0,
    window: str = "",
    index: int = -1,
    button: str = "left",
    double: bool = False,
    then: str = "none",
    allow_auth: bool = False,
    background: bool | None = None,
) -> list[Any] | str:
    """Click a control by its accessible NAME, or by a `mark` number from
    the last `marks` capture, in ONE call: the control comes from the
    accessibility tree, so no screenshot and no pixel estimation is spent.
    On the window the human is in, the window is focused and the click
    goes through the real pointer (visible cursor, same panic-kill
    guarantees). On any other window it runs in the background: the
    control's accessibility action presses it (a text field gets the app's
    own focus), the human's cursor and focus stay put, and the result reads
    the control back; a control with no action is refused there. When no
    control matches, a name also finds an element in another role that has
    an action (a web app's list row); if its action only focuses it, the
    result says so and whether the app reports it focused, which does not
    show what a key sent next will do: check with `ui`.
    `background` true or false forces one way (false takes over the human's
    screen). Pass exactly one of
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
        age = time.time() - _last_marks.get("ts", time.time())
        if age > _MARKS_TTL_S:
            raise ValueError(
                f"no mark {mark}: the last marks capture is {age / 60:.0f} minutes old and "
                "the window has surely changed; call marks() again"
            )
        client = _resolve_window(_last_marks["window"])  # raises if the window is gone
        if _last_marks.get("class") and client.get("class") != _last_marks["class"]:
            raise ValueError(
                f"no mark {mark}: the last marks capture was of {_last_marks['class']!r} and "
                f"that address now belongs to {client.get('class')!r}; call marks() again"
            )
        size = _last_marks.get("size")
        if size and list(client.get("size") or []) != list(size):
            raise ValueError(
                f"no mark {mark}: the window was resized since marks() (from {list(size)} "
                f"to {client.get('size')}), so its controls may have moved; call marks() again"
            )
        item = _last_marks["items"][mark]
        x, y = client["at"][0] + item["dx"], client["at"][1] + item["dy"]
        desc = f"mark {mark} ({item['label']})"
        control = {"_svc": item.get("svc"), "_path": item.get("path"),
                   "role": item.get("role", ""), "_unsure": item.get("unsure", False)}
        if "field" in item:
            control["_field"] = item["field"]
    else:
        client = _resolve_window(window)
        elements = _ui_read(client["address"], name=name, handles=True)
        if isinstance(elements, str):
            return elements
        exact = [e for e in elements if e["name"].lower() == name.lower()]
        pool = exact or elements
        if index >= 0:
            if index >= len(pool):
                raise ValueError(f"index {index} out of range: {len(pool)} candidates")
            pool = [pool[index]]
        if len(pool) > 1:
            public = [{k: v for k, v in c.items() if not k.startswith("_")} for c in pool]
            return [
                _text(f"{name!r} is ambiguous ({len(pool)} candidates); call again "
                    "with index=N (0-based) or a more specific name:",
                ),
                _text(json.dumps(public)),
            ]
        e = pool[0]
        x, y = e["x"], e["y"]
        desc = f"{e['role']} {e['name']!r}"
        control = e
    if bg.decide(client["address"] == _active_address(), background):
        if button != "left" or double:
            # the accessibility action is the control's primary one
            kind = "double click" if double else f"{button} click"
            bg.refuse(f"a {kind} has no accessibility action, only a left click does")
        return _click_in_background(client, control, desc, then, allow_auth)
    trust.guard_client(client)  # confinement
    trust.guard_auth_client(client, allow_auth)
    # a locked session, or a layer over the point, would swallow the click
    # while the result claimed success; refuse before any side effect.
    # click_ui always targets a specific window control, so under a lock
    # the click provably cannot reach it (window_given=True always refuses,
    # even with allow_auth: there is no "drive the prompt by control name").
    trust.guard_session_lock(True, allow_auth)
    trust.guard_covering_layer(x, y, allow_auth)
    trust.check_click_point(client, x, y, allow_auth)  # what will be over it
    if journal.dry_run():  # before the focus dispatch, which is a real change
        return _acted(
            f"{_DRY} click {desc} at ({x}, {y}) in {client.get('class', '')}",
            then,
            window=client["address"],
        )
    hyprctl.dispatch("focuswindow", f"address:{client['address']}")
    time.sleep(0.05)  # focus (and a possible workspace switch) settles first
    # focusing can move the window: Omarchy fullscreens a window focused
    # behind a fullscreen one, a scrolling layout fits its column into
    # view, a scratchpad follows the focused monitor. The point was worked
    # out from where it was, so a moved window means a stale point.
    now = _resolve_window(client["address"])
    if (now.get("at"), now.get("size")) != (client.get("at"), client.get("size")):
        again = "marks() again" if mark else "click_ui again"
        raise trust.TrustError(
            f"the window moved when it was focused (from {client.get('at')} "
            f"{client.get('size')} to {now.get('at')} {now.get('size')}); nothing was "
            f"clicked. Call {again}, so the point is worked out from where it is now."
        )
    trust.guard_covering_layer(x, y, allow_auth)
    trust.check_click_point(now, x, y, allow_auth, focused=True)
    hinput.click(x, y, button=button, double=double)
    trust.remember_seat()
    # observe the CLICKED window, not whatever holds focus after the click
    # (the click itself may have spawned a dialog that stole it)
    return _acted(
        f"clicked {desc} at ({x}, {y}) in {client.get('class', '')}",
        then,
        window=client["address"],
    )


# Hyprland's modifier names for sendshortcut, from input.MODS' wtype names
_HYPR_MODS = {"ctrl": "CTRL", "shift": "SHIFT", "alt": "ALT", "logo": "SUPER", "altgr": "MOD5"}


def _keyboard_in_background(
    client: dict[str, Any], action: str, text: str, keys: str, then: str, allow_auth: bool
) -> list[Any] | str:
    """keyboard with no focus change: text through the app's accessibility
    interface when its focused element is a text field, else key by key
    with sendshortcut, mapped through the active keyboard layout."""
    trust.guard_client(client)
    trust.guard_auth_client(client, allow_auth)
    trust.guard_session_lock(True, allow_auth)
    addr, cls = client["address"], client.get("class", "")
    layout = hyprctl.active_layout()
    kmap = keymap.for_layout(*layout)
    if action == "type":
        field, fields, unsure, complete = None, [], False, True
        bus = None
        try:
            bus = a11y.connect()
            title, size = client.get("title", ""), tuple(client["size"])
            app = a11y.app_for_pid(bus, client.get("pid"), title, size)
            frame = _proven_frame(bus, client, app)
            unsure = app is not None and frame is None
            if frame is not None:
                field = a11y.focused_text(bus, *frame)
                if field is None:  # GTK reports no focus inside an inactive window
                    fields, complete = a11y.editable_fields(bus, *frame)
                    # one field among the elements read is not the only one
                    field = fields[0] if len(fields) == 1 and complete else None
            elif unsure:  # whatever window it is, a password field anywhere counts
                fields, complete = a11y.editable_fields(bus, *app)
        except a11y.A11yError:
            field, fields = None, []  # no accessibility: keys are the way in
        password = (field is not None and field[2] == a11y.PASSWORD_ROLE) or (
            field is None and any(f[2] == a11y.PASSWORD_ROLE for f in fields))
        if password and not allow_auth:
            where = ("the focused field" if field is not None
                     else "a text field it cannot place" if unsure
                     else f"one of its {len(fields)} text fields, and it does not say which "
                     "has its focus,")
            raise trust.TrustError(
                f"{where} in {cls} is a password field; refusing to type into it. "
                "Pass allow_auth=true only if a human intends this credential entry."
            )
        keys_only = any(ch in keymap.CONTROL_CHARS for ch in text)
        if field is not None and bus is not None and not keys_only:
            if journal.dry_run():
                return _acted(f"{_DRY} type {len(text)} characters into the text field in "
                              f"{cls} through the accessibility interface", then, window=addr)
            seat = _seat_now()
            secret = field[2] == a11y.PASSWORD_ROLE  # never read, only typed with consent
            before = "" if secret else a11y.text_of(bus, field[0], field[1])
            if a11y.insert_text(bus, field[0], field[1], text):
                seen = ("not read back" if secret
                        else _read_back(before, a11y.text_of(bus, field[0], field[1]), text))
                untouched, warning = _seat_report(seat)
                paren = " (the human's focus untouched)" if untouched else ""
                kind = "password field" if secret else "text field"
                return _acted(
                    f"typed {len(text)} characters into the {kind} in {cls} through the "
                    f"accessibility interface{paren}; {seen}{warning}",
                    then, window=addr, fallback=False)
        if keys_only:
            why = "newlines, tabs and backspaces are keys, so the whole text goes key by key"
        elif field is not None:
            why = "its text field did not accept accessibility text"
        elif unsure:
            why = _UNSURE.format(addr=addr, cls=cls)
        elif not complete:
            why = (f"{cls} has more elements than hypruse reads ({a11y.WALK_BUDGET}), and its "
                   "focused text field was not among them")
        elif fields:
            why = f"{cls} does not say which of its {len(fields)} text fields has its focus"
        else:
            why = f"{cls} has no accessibility text field to put it in"
        chords = []
        for i, ch in enumerate(text, start=1):
            chord = kmap.chord(ch)
            if chord is None:  # by position: typed text never goes into a result
                bg.refuse(f"character {i} of the text has no key on the {layout[0]} "
                          f"keyboard layout, and {why}")
            chords.append(chord)
    else:
        mods, key = hinput.parse_combo(keys)
        if key is None:
            bg.refuse("a bare modifier tap cannot be sent to one window")
        names = [_HYPR_MODS.get(m, m.upper()) for m in mods]
        if len(key) == 1:  # a character key: its base key on this layout
            chord = kmap.chord(key)
            if chord is None:
                bg.refuse(f"{key!r} has no key on the {layout[0]} keyboard layout")
        else:  # a keysym name: a shifted one ("plus") goes through its character
            chord = kmap.chord_for_key(key)
        if chord is not None:
            names += chord[0].split()
            key = chord[1]
        chords = [(" ".join(dict.fromkeys(names)), key)]
    group, groups = hyprctl.layout_group()
    if group != 0:
        bg.refuse(
            f"the keyboard is on layout {group + 1} of {groups}, and Hyprland's sendshortcut "
            "finds a key on the layout in use but the app reads it on the first one, so "
            "other characters would arrive; switch to the first layout"
        )
    if client.get("xwayland"):
        bg.refuse(f"{cls} is an XWayland window, and Hyprland sends background keys between "
                  "two XWayland windows to the focused one instead")
    what = f"{len(chords)} keys" if action == "type" else f"{keys}"
    if journal.dry_run():
        return _acted(f"{_DRY} send {what} to {addr} with sendshortcut", then, window=addr)
    seat = _seat_now()
    for sent, (mods_, key_) in enumerate(chords):
        try:
            hyprctl.send_shortcut(mods_, key_, addr)
        except hyprctl.HyprctlError:  # say how much landed, so a retry does not repeat it
            # (not Hyprland's own message: it names the key, a typed character)
            raise hyprctl.HyprctlError(
                f"sent {sent} of {len(chords)} keys to {addr}, then Hyprland refused a key "
                "(the window may have closed)") from None
    untouched, warning = _seat_report(seat)
    paren = " (the human's focus untouched)" if untouched else ""
    # keys land on whatever control the app has focused, which in a hidden
    # window is not the field text went into through accessibility
    return _acted(f"sent {what} to {addr} ({cls}) with sendshortcut, to the control the app "
                  f"has focused{paren}{warning}", then, window=addr, fallback=False)


def _read_back(before: str, after: str, text: str) -> str:
    """What a text field shows after typing, without its contents: the
    journal keeps every result line, and typed text is only ever recorded
    as a length plus a digest. `ui` shows the value itself."""
    if after == before:
        return "no change seen"
    if len(after) == len(before) + len(text) and text in after:
        return f"the field now holds the typed text ({len(after)} characters)"
    return f"the field changed, but not by exactly the typed text ({len(after)} characters now)"


# roles whose "click" is taking keyboard focus, which GrabFocus does inside
# the app without touching the human's focus
_TEXT_ROLE_NAMES = frozenset(
    {"text", "entry", "password text", "terminal", "text box", "search box"})


def _click_in_background(
    client: dict[str, Any], control: dict[str, Any], desc: str, then: str, allow_auth: bool
) -> list[Any] | str:
    """click_ui with no pointer and no focus change: the control's own
    accessibility action (GrabFocus for a text field), read back after."""
    trust.guard_client(client)  # confinement (the agent's workspaces under the guarantee)
    trust.guard_auth_client(client, allow_auth)
    trust.guard_session_lock(True, allow_auth)
    cls = client.get("class", "")
    if control.get("_unsure"):
        bg.refuse(_UNSURE.format(addr=client["address"], cls=cls)
                  + ", and a press could land in the other window")
    svc, path = control.get("_svc"), control.get("_path")
    if not isinstance(svc, str) or not isinstance(path, str):
        bg.refuse("this control was not resolved through the accessibility tree")
    is_text = control.get("_field", control.get("role", "") in _TEXT_ROLE_NAMES)
    # the reads that can refuse come before the dry run, so a rehearsal
    # refuses what the real run would
    bus = a11y.connect()
    if not is_text and not a11y.has_action(bus, svc, path):
        bg.refuse("this control has no accessibility action")
    # an action that only focuses (Qt WebEngine's list rows offer just
    # SetFocus) does not press: the result must not say it did
    action = "" if is_text else a11y.action_name(bus, svc, path)
    focus_only = "focus" in action.lower()
    how = f"its accessibility action {action!r}" if focus_only else "its accessibility action"
    if is_text:
        kit, version = a11y.toolkit(bus, svc)
        kit = kit.lower()
        instead = "to type into a window with one text field, use keyboard with window= directly"
        if kit == "gtk" and version.startswith("4."):
            bg.refuse("GTK 4 does not let a field take focus over the accessibility interface",
                      instead)
        presents = kit == "gail" or (kit == "gtk" and version[:2] in ("2.", "3.")) or not kit
        if presents and _focus_on_activate(client["address"]) != 0:
            # GTK 2 and 3 present their window when a widget takes focus over
            # AT-SPI; a toolkit that cannot be read is treated the same
            bg.refuse(
                f"{cls} brings its window forward when a field takes focus (GTK 2 or 3, or a "
                "toolkit that cannot be read), and Hyprland's focus_on_activate is on for it "
                "(or cannot be read), so focusing this field would switch the human to it",
                instead,
            )
    if journal.dry_run():
        verb_ = "focus" if is_text or focus_only else "press"
        return _acted(f"{_DRY} {verb_} {desc} in {cls} through {how}",
                      then, window=client["address"])
    seat = _seat_now()
    before = a11y.current_value(bus, svc, path)
    ok = a11y.grab_focus(bus, svc, path) if is_text else a11y.do_action(bus, svc, path)
    if not ok:
        bg.refuse("the app did not accept the accessibility action")
    time.sleep(0.05)  # let the app apply it before reading back
    verb = "focused" if is_text or focus_only else "pressed"
    untouched, warning = _seat_report(seat)
    paren = "no pointer, the human's focus untouched" if untouched else "no pointer"
    msg = f"{verb} {desc} through {how} ({paren}) in {cls}"
    if not any(c.get("address") == client["address"] for c in hyprctl.query("clients")):
        msg += "; the window closed"
    elif focus_only:  # what the app reports, not proof a key reaches it or acts
        msg += ("; the app now reports it focused (it is not pressed; check what a key "
                "does with ui)" if a11y.is_focused(bus, svc, path) else
                "; the app does not report it focused")
    elif not is_text:  # focus is not a value: a field reads the same after it
        after = a11y.current_value(bus, svc, path)
        if after != before:
            msg += "; now: " + ", ".join(
                f"{k}={str(v).lower() if isinstance(v, bool) else repr(v)}"
                for k, v in after.items()
            )
        elif before:
            msg += "; no change seen"
    return _acted(msg + warning, then, window=client["address"], fallback=False)


def _seat_now() -> tuple[Any, Any] | None:
    """The human's active window and workspace, read around a background
    action so its result claims only what was checked; None if unreadable."""
    try:
        active, ws = hyprctl.batch_query(["activewindow", "activeworkspace"])
        return (active or {}).get("address"), (ws or {}).get("id", (ws or {}).get("name"))
    except Exception:
        return None


_SEAT_SETTLE = 0.2


def _seat_report(before: tuple[Any, Any] | None) -> tuple[bool, str]:
    """(untouched, warning) for a background result: it may say the human's
    focus was untouched only when the seat read the same before and after,
    and says what changed when it did not (an app can open or activate a
    window of its own in response)."""
    if before is None:
        return False, ""
    time.sleep(_SEAT_SETTLE)  # an app opens or activates a window a beat later
    after = _seat_now()
    if after is None:
        return False, ""
    if after == before:
        return True, ""
    return False, (f"; WARNING: the human's focused window changed during this action (now "
                   f"{after[0]} on workspace {after[1]}), so it may have opened or activated "
                   "a window")


def _focus_on_activate(address: str = "") -> int | None:
    """Hyprland's focus_on_activate for a window (a window rule overrides the
    misc: option, and getprop reports the result), else the option; None
    when neither can be read."""
    if address:
        with contextlib.suppress(Exception):
            reply = hyprctl.query(f"getprop address:{address} focus_on_activate")
            value = reply.get("focus_on_activate") if isinstance(reply, dict) else None
            if isinstance(value, int | bool):
                return int(value)
    try:
        reply = hyprctl.query("getoption misc:focus_on_activate")
    except Exception:
        return None
    value = reply.get("int", reply.get("bool")) if isinstance(reply, dict) else None
    return int(value) if isinstance(value, int | bool) else None


_ADDR = re.compile(r"^0x[0-9a-fA-F]+$")


def _addr(target: str) -> str:
    if not _ADDR.match(target):
        raise ValueError(
            f"{target!r} is not a window address, use the `address` field from desktop()"
        )
    return f"address:{target}"


# Hyprland accepts a lot of shapes here (3, name:notes, special:magic, +1,
# e+1, previous, empty) and a workspace NAME may contain spaces, so there is
# no grammar to match against; what this refuses is the punctuation a
# workspace identifier never contains. That keeps the argument inert as a
# Lua expression, which matters because `hyprctl dispatch` on a Lua config
# evaluates its argument inside the compositor's interpreter: this is the
# one dispatcher argument an agent supplies that is not an address, and it
# is deliberately the one `hypr` action that skips the confinement guard.
# The comma is refused for a second reason: move_window joins the workspace
# and the address with one.
_WORKSPACE_REFUSED = frozenset("()[]{}\"'`;=\\,\n\r\t")


def _workspace(name: str) -> str:
    bad = sorted(set(name) & _WORKSPACE_REFUSED)
    if bad:
        raise ValueError(
            f"{name!r} is not a workspace: {''.join(bad)!r} cannot appear in one. "
            "Use a number, a name, or special:name."
        )
    return name


_CLOSE_WAIT_S = 1.0


def _close_and_confirm(target: str) -> str:
    """Dispatch closewindow and wait, briefly, for the compositor to report
    the destroy. closewindow only ASKS the app to close (it may not comply
    at all: an unsaved-changes prompt), so an immediate `then` observation
    would still list the window and the strict seat baseline would capture
    a focus about to vanish. Subscribes before dispatching so the event
    cannot slip past; degrades to fire-and-forget without the socket."""
    addr = _addr(target)
    try:
        stream = events.EventStream()
    except events.EventError:
        hyprctl.dispatch("closewindow", addr)
        return f"asked {target} to close"
    with stream:
        hyprctl.dispatch("closewindow", addr)
        try:
            hit = stream.wait_for(
                {"closewindow"},
                lambda _n, p: str(p.get("address", "")).lower() == target.lower(),
                _CLOSE_WAIT_S,
            )
        except events.EventError:
            # the dispatch already fired; a socket dying mid-wait must not
            # turn a (probably successful) close into a tool error
            return f"asked {target} to close (event socket dropped before confirming)"
    if hit is None:
        return (
            f"asked {target} to close, but it is still open after "
            f"{_CLOSE_WAIT_S:.0f}s (it may be prompting to save)"
        )
    return f"closed {target}"


_HYPR_PLANS = {
    "workspace": "switch to workspace {workspace}",
    "focus_window": "focus {target}",
    "move_window": "move {target} to workspace {workspace}",
    "close_window": "ask {target} to close",
    "fullscreen": "toggle fullscreen on {target_or_active}",
    "toggle_floating": "toggle floating on {target_or_active}",
}


_HYPR_TAKES_VIEW = {
    "workspace": "switching the workspace changes what the human sees",
    "focus_window": "focusing a window takes the human's keyboard focus",
    "fullscreen": "fullscreen works on the focused window, so it takes the human's focus first",
}


@journal.journaled("act")
def hypr(action: str, target: str = "", workspace: str = "", then: str = "none") -> list[Any] | str:
    """Window/workspace ops over IPC (instant, no vision).
    action='workspace' (workspace: number/name/'special:name') |
    'focus_window' (target: address) | 'move_window' (target + workspace,
    silent) | 'close_window' (target) | 'fullscreen' (target?) |
    'toggle_floating' (target?). `then` ('desktop'|'screenshot'|'ui'|'none')
    appends the result to this call. While the background guarantee is on
    (HYPRUSE_BACKGROUND=1), workspace, focus_window and fullscreen are
    refused because they change the human's view, and move_window moves
    only to the agent's own workspaces."""
    safety.touch(f"hypr:{action}")
    workspace = workspace.strip()
    # every argument is checked before any guard runs and before anything
    # is dispatched, so a bad call cannot half-run and a dry run raises
    # exactly what the real call would
    if action not in _HYPR_PLANS:
        raise ValueError(
            f"unknown action {action!r}: workspace|focus_window|move_window|"
            "close_window|fullscreen|toggle_floating"
        )
    if action == "workspace" and not workspace:
        raise ValueError("workspace action needs `workspace`")
    if action == "move_window" and not workspace:
        raise ValueError("move_window needs `workspace`")
    if workspace:
        _workspace(workspace)
    if action in ("focus_window", "move_window", "close_window") or (
        target and action in ("fullscreen", "toggle_floating")
    ):
        _addr(target)  # the branches below re-derive it; this is the check
    if bg.mode() == bg.GUARANTEE:
        # what changes the human's view; the rest is confined to the agent's
        # windows by the guarantee's scope
        if action in _HYPR_TAKES_VIEW:
            bg.refuse(_HYPR_TAKES_VIEW[action])
        if action == "toggle_floating" and not target:
            bg.refuse("toggle_floating with no target acts on the human's focused window")
        if action == "move_window":
            bg.require_agent_workspace(workspace)
            if any(bg.matches(workspace, ws)
                   for ws in _shown_workspaces(hyprctl.query("monitors"))):
                bg.refuse(f"workspace {workspace} is on screen, so the window would show in the "
                          "human's sight")
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
    if journal.dry_run():
        return _acted(
            f"{_DRY} "
            + _HYPR_PLANS[action].format(
                target=target, workspace=workspace, target_or_active=target or "the active window"
            ),
            then,
        )
    if action == "workspace":
        hyprctl.dispatch("workspace", workspace)
        msg = f"on workspace {workspace}"
    elif action == "focus_window":
        hyprctl.dispatch("focuswindow", _addr(target))
        msg = f"focused {target}"
    elif action == "move_window":
        hyprctl.dispatch("movetoworkspacesilent", f"{workspace},{_addr(target)}")
        msg = f"moved {target} to workspace {workspace}"
    elif action == "close_window":
        msg = _close_and_confirm(target)
    elif action == "fullscreen":
        if target:
            hyprctl.dispatch("focuswindow", _addr(target))
        hyprctl.dispatch("fullscreen", "0")
        msg = "fullscreen toggled"
    else:  # toggle_floating, the last of _HYPR_PLANS
        args = (_addr(target),) if target else ()
        hyprctl.dispatch("togglefloating", *args)
        msg = "floating toggled"
    trust.remember_seat()  # our own focus/workspace change re-baselines the seat
    return _acted(msg, then)


def _await_new_window(
    before: set[str], wait_s: float, accept: Callable[[dict[str, Any]], bool] | None = None
) -> dict[str, Any] | None:
    """Polling fallback when the event socket is unavailable."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(0.15)
        for c in hyprctl.query("clients"):
            if c["address"] not in before and (accept is None or accept(c)):
                return c
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_WRAPPERS = frozenset({
    "env", "setsid", "nohup", "exec", "uwsm", "uwsm-app", "app", "systemd-run", "flatpak",
    "run", "gtk-launch", "sh", "bash", "dash", "zsh", "fish", "dbus-run-session",
})


def _program(command: str) -> str:
    """The program a launch command runs, past the usual wrappers (uwsm app
    --, setsid, flatpak run, sh -c '...'), their flags and VAR=value words.
    A Flatpak id (org.mozilla.firefox) still contains the app's class."""
    for word in command.replace("'", " ").replace('"', " ").split():
        if word in _WRAPPERS or word.startswith("-") or "=" in word:
            continue
        return os.path.basename(word)
    return ""


def _launched_window(workspace: str, command: str) -> Callable[[dict[str, Any]], bool]:
    """Which new window a placed launch waits for: one on its workspace, or
    one of the launched program's own class (a single-instance app's window
    opens from the running process, which ignores the rule). Any other new
    window is someone else's, often the human's, and must not be taken over
    and moved. `workspace` is an event's workspace name or a client's
    workspace record."""
    prog = _norm(_program(command))

    def on_it(ws: Any) -> bool:
        rec = ws if isinstance(ws, dict) else {"name": str(ws)}
        name = str(rec.get("name", ""))
        return bg.matches(workspace, rec) or name in (workspace, workspace.removeprefix("name:"))

    def same_app(cls: str) -> bool:
        c, last = _norm(cls), _norm(cls.rsplit(".", 1)[-1])
        if len(prog) < 3 or len(c) < 3:
            return False
        return prog in c or c in prog or (len(last) >= 3 and (last in prog or prog in last))

    def accept(win: dict[str, Any]) -> bool:
        if on_it(win.get("workspace")) or same_app(str(win.get("class", ""))):
            return True
        # an event names the workspace by NAME, which a workspace rule's
        # default name can make differ from its number: ask for the record
        if not isinstance(win.get("workspace"), dict) and win.get("address"):
            with contextlib.suppress(Exception):
                c = _client_by_address(win["address"])
                return bool(c and on_it(c.get("workspace")))
        return False

    return accept


def _client_by_address(address: str) -> dict[str, Any] | None:
    return next((c for c in hyprctl.query("clients") if c["address"] == address), None)


def _launch_and_wait(
    rule_command: str, wait_s: float, accept: Callable[[dict[str, Any]], bool] | None = None
) -> dict[str, Any] | None:
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
        return _await_new_window(before, wait_s, accept)
    with stream:
        hyprctl.dispatch("exec", rule_command)
        matcher = None if accept is None else (lambda name, payload: accept(payload))
        hit = stream.wait_for({"openwindow"}, matcher, wait_s)
    if hit is None:
        return None
    address = hit[1]["address"]
    win = _client_by_address(address)
    if win is None:
        time.sleep(0.1)  # event can beat hyprctl's client list by a beat
        win = _client_by_address(address)
    return win


@journal.journaled("act")
def launch(
    command: str, workspace: str = "", wait_s: float = 8.0, background: bool | None = None
) -> dict[str, Any] | str:
    """Open an app: run `command` via Hyprland exec. Optional `workspace` placement
    (silent, works even for single-instance apps like browsers, whose
    window gets moved after it appears) and `wait_s` (1-30, default 8;
    raise for slow apps). Without `workspace`, the app opens silently on
    the next of the agent's own workspaces (HYPRUSE_AGENT_WORKSPACES, 99
    down to 90 by default) that is not on screen, so the agent can work on
    it in the background; `background=false` opens it on the human's
    workspace instead. It waits for its own window (one on that workspace,
    or of the program's own class), never one the human opens meanwhile.
    An app that is already running opens its window from the running
    process, on the human's screen, before it is moved: the note says so.
    Returns the new window's address/class/title/workspace, or a timeout
    note."""
    safety.touch("launch")
    wait_s = min(max(wait_s, 1.0), 30.0)
    # Background mode: with no workspace, automatic mode and the guarantee
    # open the app on the next agent workspace that is off screen
    # (background=false refuses under the guarantee, here); the guarantee
    # takes no other workspace, and none the human can see
    workspace = workspace.strip()
    to_agent = bg.decide(False, background)
    mode = bg.mode()
    note = ""
    if workspace:
        bg.require_agent_workspace(
            _workspace(workspace), "leave workspace out to use the next free agent workspace"
        )
        if (mode == bg.GUARANTEE or background is True) and any(
            bg.matches(workspace, ws) for ws in _shown_workspaces(hyprctl.query("monitors"))
        ):
            bg.refuse(
                f"workspace {workspace} is on screen, so the app would open in the human's "
                "sight and take their focus",
                "leave workspace out to use the next free agent workspace",
            )
    elif to_agent:
        allowed = _confine_allows()
        if allowed is not None and not any(allowed(sel) for sel in bg.agent_workspaces()):
            if mode == bg.GUARANTEE:
                bg.refuse("HYPRUSE_CONFINE=workspace:... holds none of the agent's workspaces, "
                          "so no app can open where the agent may work")
            note = (
                "HYPRUSE_CONFINE holds none of the agent's workspaces, so it opened on the "
                "human's workspace as before background mode"
            )
        else:
            workspace = bg.next_workspace(
                hyprctl.query("clients"), trust.owned(),
                shown=_shown_workspaces(hyprctl.query("monitors")), allowed=allowed,
            )
            note = (
                f"opened on workspace {workspace}, one of the agent's "
                "(HYPRUSE_AGENT_WORKSPACES), out of the human's sight"
                + ("" if mode == bg.GUARANTEE
                   else "; pass background=false to open it on the human's workspace")
            )
    rule = f"[workspace {_workspace(workspace)} silent] " if workspace else ""
    if journal.dry_run():
        return f"{_DRY} run {rule + command} and wait up to {wait_s:.0f}s for its window"
    active_before = _active_address() if rule else None
    accept = _launched_window(workspace, command) if rule else None
    win = _launch_and_wait(rule + command, wait_s, accept)
    if win is None:
        return (
            f"launched, but no new window appeared within {wait_s:.0f}s, slow or "
            "single-instance apps may open late and on their own workspace; call "
            "desktop() to find the window, then hypr move_window if needed"
        )
    # owned-set for `launched` confinement; tag + notify when HYPRUSE_MARK
    trust.note_launched(win["address"], win.get("class", ""))
    ws = win.get("workspace", {})
    result: dict[str, Any] = {
        "address": win["address"],
        "class": win.get("class", ""),
        "title": win.get("title", ""),
        "workspace": hyprctl.workspace_ref(ws),
    }
    if note:
        result["note"] = note
    moved = bool(workspace) and not _launched_window(workspace, "")(win)
    if moved:
        hyprctl.dispatch("movetoworkspacesilent", f"{workspace},address:{win['address']}")
        result["workspace"] = workspace
        landed = ws.get("name") or ws.get("id")
        on_screen = None
        with contextlib.suppress(Exception):
            refs = hyprctl.visible_workspaces(hyprctl.query("monitors"))
            on_screen = hyprctl.workspace_ref(ws) in refs
        what = (
            f"it opened on workspace {landed} first (single-instance app behavior) and was "
            f"moved to {workspace}"
        )
        if on_screen:
            what += "; while there it was on the human's screen and took their keyboard focus"
        elif on_screen is None:
            what += "; it may have been on the human's screen and taken their focus there"
        result["note"] = (f"{note}; " if note else "") + what
        if mode == bg.GUARANTEE and on_screen is not False:
            result["warning"] = (
                "this broke HYPRUSE_BACKGROUND=1: the app was already running, and its new "
                "window opened from the running process, which ignores the workspace rule; "
                "start a separate instance (a browser: a separate profile) or ask the human"
            )
    active_after = _active_address() if rule else None
    if moved and active_after != active_before:
        result["note"] += f" (the focused window is now {active_after})"
    took = bool(rule) and not moved and active_after == win["address"]
    if took:  # Hyprland dropped the silent rule (the workspace was on screen)
        result["note"] = (result["note"] + "; " if result.get("note") else "") + (
            "it opened on screen and took the human's keyboard focus")
        if mode == bg.GUARANTEE:
            result["warning"] = (
                "this broke HYPRUSE_BACKGROUND=1: the new window took the human's keyboard "
                "focus; tell the human"
            )
    if mode == bg.OFF or not rule or moved or took:
        # the launch moved the seat (the new window took focus, or ours
        # moved it): re-baseline the seat guard. Any other change since the
        # last action is the human's, for the guard to see. Off mode keeps
        # 0.12's behaviour.
        trust.remember_seat()
    return result


def _shown_workspaces(monitors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The workspace records on screen: each monitor's active one and any
    scratchpad pulled up over it."""
    out = []
    for m in monitors:
        for slot in ("activeWorkspace", "specialWorkspace"):
            ws = m.get(slot) or {}
            if hyprctl.workspace_ref(ws) is not None:
                out.append(ws)
    return out


def _confine_allows() -> Callable[[str], bool] | None:
    """HYPRUSE_CONFINE=workspace:... narrows the agent workspaces an app may
    open on: one outside the scope could be neither used, moved nor closed."""
    scope = trust._explicit_scope()
    if scope is None or scope[0] != "workspace":
        return None
    vals = scope[1]
    return lambda sel: sel in vals or sel.removeprefix("name:") in vals


_INTERACTIVE_HELP = """\
hypruse {version}, an MCP server, not an interactive program.

It speaks the MCP protocol over stdin/stdout and is meant to be launched by
an MCP client, so running it directly in a terminal just waits silently for
a client that never connects (Ctrl+C to quit).

Register it with Claude Code:
  claude mcp add -s user hypruse -- uvx hypruse

Or set `uvx hypruse` as a stdio server in your MCP client's config.
Check the install with:  hypruse --version
The same tools as shell verbs (for agents that run commands): hypruse --help
"""


@journal.journaled("observe")
def binds() -> list[dict[str, Any]]:
    """The user's own Hyprland keybinds: combo, action, arg, and a
    description when the config provides one. This is how the desktop's
    owner drives it: to perform one of these workflows, call `use_bind`
    with the combo (it runs the bound action). An action of `lua` means the
    bind is a closure in a Lua Hyprland config, which nothing can run from
    outside: read its description and do the same thing with `hypr` or
    `launch`. NOTE: the `keyboard` tool canNOT trigger these compositor
    binds (synthetic keys reach apps, not Hyprland's bind matcher), so do
    not try to press them."""
    return hyprctl.binds()


# The one tool that both reads and writes: a clipboard READ is the agent
# looking at something the human owns (an observation a privacy audit
# wants), a WRITE changes state (an action replay re-issues).
@journal.journaled(lambda args: "act" if args.get("action") == "write" else "observe")
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
        if journal.dry_run():
            return f"{_DRY} copy {len(text)} characters to the clipboard"
        clip.write(text)
        return f"copied {len(text)} characters to the clipboard"
    raise ValueError(f"unknown action {action!r}: read|write")


@journal.journaled("act")
def use_bind(combo: str, then: str = "none") -> list[Any] | str:
    """Run one of the user's own Hyprland keybinds by its combo (from the
    `binds` tool), e.g. 'SUPER+F'. This executes the bound action directly
    (the only reliable way: synthetic keypresses do not trigger compositor
    binds). Use it to drive the owner's configured workflows: launchers,
    layout shortcuts, scratchpads. `then` ('desktop'|'screenshot'|'ui'|'none')
    appends the result to this call (handy after a launcher bind). Refused
    while HYPRUSE_CONFINE is set or the background guarantee is on
    (HYPRUSE_BACKGROUND=1): a bind runs an arbitrary compositor action
    that cannot be scoped to a window."""
    safety.touch("use_bind")
    trust.guard_seat()
    trust.guard_use_bind()  # an arbitrary compositor action escapes confinement
    bind = hyprctl.find_bind(combo)
    if bind is None:
        raise ValueError(f"no keybind {combo!r}; call binds() for the exact combos")
    # Refused before the dry run, not after: a rehearsal that reported a
    # plan for this would be describing something that can never happen.
    if bind["action"] == hyprctl.LUA_BIND:
        described = f" It is described as {bind['description']!r}." if bind.get(
            "description"
        ) else ""
        raise ValueError(
            f"{bind['combo']} is bound in a Lua Hyprland config, where a bind is an "
            "anonymous closure the compositor exposes no way to call. No tool can run "
            f"it and retrying will not help.{described} Do the action directly instead: "
            "`hypr` for window and workspace ops, `launch` for apps."
        )
    action, arg = bind["action"], bind.get("arg", "")
    if journal.dry_run():
        return _acted(f"{_DRY} run {bind['combo']}: {action} {arg}".rstrip(), then)
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


def _event_matcher(needle: str) -> Callable[[str, dict[str, Any]], bool]:
    """Whether an event matches a wait's `match` text (lowercased). Hyprland
    0.57 events name a workspace by its address, its number for a numbered
    workspace (hyprwm/Hyprland#16234), while `match` may be the display name
    a rename gave it; when the event text misses, its workspace is looked
    up and its display name tried too."""

    def matches(_name: str, payload: dict[str, Any]) -> bool:
        if not needle:
            return True
        if needle in " ".join(str(v) for v in payload.values()).lower():
            return True
        ref = payload.get("workspace") if "workspace" in payload else (
            payload.get("name") if _name == "workspace" else None
        )
        return ref is not None and needle in _workspace_aliases(str(ref)).lower()

    return matches


def _workspace_aliases(ref: str) -> str:
    """Every name hyprctl knows a workspace by, given the one an event used."""
    try:
        for w in hyprctl.query("workspaces") or []:
            if ref in (str(w.get("address", "")), str(w.get("id", "")), str(w.get("name", ""))):
                names = [str(w.get("name", "")), str(w.get("address", ""))]
                # a workspace's number, but not 0.56's negative ids for named
                # and special workspaces (-1337), whose digits nobody filters on
                if isinstance(w.get("id"), int) and w["id"] > 0:
                    names.append(str(w["id"]))
                return " ".join(names)
    except Exception:
        pass
    return ""


def _workspace_signatures(ws: dict[str, Any]) -> set[str]:
    """The event signatures a switch to this workspace can carry: its
    display name, and on Hyprland 0.57 its address, which events use."""
    sigs = {f"workspace:{ws.get('name', '')}"}
    if ws.get("address"):
        sigs.add(f"workspace:{ws['address']}")
    return sigs


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
        # Hyprland 0.57 names it by display name and by address (its number)
        if any(needle in str(ws.get(k, "")).lower() for k in ("name", "address")):
            return {"event": "workspace", "name": name, "already": True}
    # layer_open is the classic late-subscribe case: use_bind returns, the
    # launcher maps instantly, and only then does the agent call
    # wait_for(layer_open); without this check that wait can never succeed.
    # Filtered only, like workspace: some layer (a bar) is always mapped,
    # so an unfiltered pre-check would answer instantly without waiting.
    # layer_close deliberately has NO pre-check: 'nothing matching is
    # mapped' also describes a layer whose trigger fired but whose surface
    # has not mapped yet, and answering 'already closed' there acts on
    # false state (inside sequence the trigger-to-wait gap is only the
    # 0.2s settle, well under a launcher's startup); a late-subscribed
    # close wait times out honestly instead.
    if event == "layer_open" and needle:
        mapped = next(
            (
                s
                for s in hyprctl.parse_layers(hyprctl.query("layers"))
                if needle in s.get("namespace", "").lower()
            ),
            None,
        )
        if mapped is not None:
            return {"event": "openlayer", "namespace": mapped["namespace"], "already": True}
    return None


@journal.journaled("observe")
def wait_for(event: str, match: str = "", timeout_s: float = 10) -> dict[str, Any] | str:
    """Block until a desktop event happens (real compositor events, not
    polling). event: 'window_open' | 'window_close' | 'workspace' |
    'title_change' | 'layer_open' | 'layer_close' (layer-shell surfaces:
    launchers, notification popups; match on the namespace, e.g. 'wofi') |
    'urgent' (a window demands attention) | 'screencast' (screen sharing
    started/stopped). match: optional case-insensitive substring filter
    over the event's fields (class/title/workspace name/address/namespace).
    timeout_s 1-60, default 10. Returns the event payload, or a timeout
    note; a filtered wait whose condition ALREADY holds (the window is
    already gone, the workspace already active, the layer already mapped)
    returns instantly with `already: true` instead of missing an event
    that fired before it could subscribe. Use it after actions with
    delayed effects: app startups, page loads that change a window title,
    a launcher bind that pops a layer."""
    names = _WAIT_EVENTS.get(event)
    if names is None:
        raise ValueError(f"unknown event {event!r}: {'|'.join(_WAIT_EVENTS)}")
    safety.touch(f"wait_for:{event}")
    timeout_s = min(max(timeout_s, 1.0), 60.0)
    needle = match.lower()

    already = _already_satisfied(event, needle)
    if already is not None:
        return already

    matcher = _event_matcher(needle)
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
# Layer events count only for kinds that take the seat over: a launcher
# popping up mid-sequence swallows the next keystrokes, and an on-screen
# keyboard, while it does not grab the keyboard, covers the area the next
# click was aimed at. Notification popups and bars are noise, not a
# takeover.
_WATCHED_EVENTS = {
    "openwindow", "closewindow", "movewindow", "workspace", "openlayer", "closelayer",
}

def _structural(name: str, payload: dict[str, Any]) -> bool:
    if name in ("openlayer", "closelayer"):
        return hyprctl.layer_kind(payload.get("namespace", "")) in hyprctl.FOCUS_STEALING_KINDS
    return name in _WATCHED_EVENTS

_SEQ_MAX_STEPS = 20
_SEQ_PLACED = ("pointer", "keyboard", "click_ui")  # the steps that take background=
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

    matcher = _event_matcher(needle)
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


@journal.journaled("act")
def sequence(
    steps: list[dict[str, Any]],
    stop_on_change: bool = True,
    then: str = "desktop",
    background: bool | None = None,
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
    workspace, or a seat-taking layer surface (a launcher or on-screen
    keyboard) appearing, so later steps do not act on stale state.
    Notification popups and bars are not treated as changes. It does NOT catch
    a bare focus change, so to type into a specific window reliably give
    that keyboard step a window= address (the keys go to that window).
    Bounded to 20
    steps and ~30s total. `then` observes the final state ('desktop'
    default, 'screenshot', 'ui', 'none'). `background` applies to every
    pointer, keyboard and click_ui step that does not set its own, and
    background=true refuses a hypr step that takes the human's view."""
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
            if background is not None and step.get("op") in _SEQ_PLACED:
                run.setdefault("background", background)  # a step's own choice wins
            if step.get("op") == "wait_for":  # never let one wait outlast the budget
                run["timeout_s"] = min(float(step.get("timeout_s", 10)), max(budget_left, 1.0))
            try:
                if background is True and step.get("op") == "hypr":
                    if step.get("action") in _HYPR_TAKES_VIEW:
                        bg.refuse(_HYPR_TAKES_VIEW[step["action"]])
                    if step.get("action") == "toggle_floating" and not step.get("target"):
                        bg.refuse("toggle_floating with no target acts on the human's "
                                  "focused window")
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
                    prev_expected |= _workspace_signatures(ws)
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
    # each step already says DRY RUN, but the summary line is what gets
    # skimmed, and "all 4/4 steps ran" reads like four things happened.
    # A dry run also cannot cause the changes its own later steps expect,
    # so any wait_for step in it times out honestly rather than being
    # satisfied by an effect that was never delivered.
    # then='ui' reads the last window a step named: in the background the
    # focused window is the human's, not the one the steps worked in
    named = [s.get("window") for s in steps[:len(results)] if s.get("window")]
    tag = " (DRY RUN, nothing was delivered)" if journal.dry_run() else ""
    if stopped is None:
        head = f"sequence{tag}: all {ran}/{len(steps)} steps ran\n" + "\n".join(results)
    else:
        head = (
            f"sequence{tag}: stopped after {ran}/{len(steps)} steps, {stopped}\n"
            + "\n".join(results)
        )
    return _acted(head, then, window=named[-1] if named else "", fallback=not named)


# Read-only mode strips the seven acting tools, so the observation tools'
# own descriptions must not send the agent to them: these variants replace
# the acting references with observation-only guidance. Tools not listed
# here (wait_for) reference no acting tool and keep their docstring.
_READONLY_DOCS = {
    "desktop": """Semantic desktop snapshot: monitors, workspaces, windows (address,
    class, title, `at` + `size` in global coords), active window, cursor,
    and `layers`: launchers (wofi/rofi), bars, notification popups, and
    on-screen keyboards are NOT windows and appear only there, with a
    best-effort `kind` and global geometry you can screenshot by region. A
    listed layer is one the compositor tracks, not one you can see: it may
    be transparent or dormant, so screenshot when visibility matters. Call
    first; pass the addresses it returns to the other observation tools.""",
    "screenshot": """Capture the focused monitor, a window (`window`: "active" or an
    address from desktop, cheapest for reading one app), or a `region`
    "x,y,WxH". Returns the image (or a file path to read) + JSON metadata
    with geometry/scale for pixel→global mapping. A window on a hidden
    workspace or under another window is still captured as itself
    (metadata `source` "window"; without grim -T a shown window is cropped
    from the screen, `source` "screen", and a hidden one is refused);
    `visible` says whether the compositor draws it, and
    a hidden one may show how it last looked (many apps stop drawing while
    hidden). Window captures are refused while the session is locked. `scale` 0.1-1.0:
    optional deliberate downscale, usually leave unset. `stable=true`
    waits (up to 2s) until two consecutive frames match, so a capture
    right after a change is not taken mid-animation; metadata gains
    `stable`. Captures are fast JPEG by default; `lossless=true` returns
    PNG for pixel-exact work. To inspect a small control closely, follow
    with `zoom` at the estimated point.""",
    "zoom": """Native-resolution re-capture around a point: the precision step of
    the coarse-to-fine loop. Screenshot first, estimate the target's global
    x,y, zoom there, and read the fine detail on the zoomed image (scale
    ~1.0, so global = geometry[:2] + image_pixel). `size` "WxH" in
    logical pixels (default 480x360) is clamped to the screen; `window`
    (an address from desktop, on screen) clamps to that window instead. The metadata
    echoes the requested point back as `point`; `stable=true` waits for
    the frame to settle first; `lossless=true` returns PNG.""",
    "ui": """Read a window's accessibility tree (AT-SPI) and return its elements
    with GLOBAL coordinates, so you can read an app's controls and their
    state by NAME with no screenshot and no pixel guessing. `window` is an
    address from desktop (default: the focused window). `name` filters to
    elements whose accessible name contains it (case-insensitive);
    `actionable` (default) keeps only interactive roles (buttons, entries,
    menu items, ...); with a `name` that matches none of those, it keeps
    named elements in other roles that have an accessibility action (a web
    app's list row), with the action's name as `action`. Returns
    [{role, name, x, y, clickable}]. Controls
    that carry a CURRENT VALUE also report it: `value` (text typed into an
    entry, or a slider/spinner number), `percent` for a slider's position,
    `checked` for a box or toggle. Password fields never report contents,
    and many dropdowns expose no value at all, so read the screen with
    screenshot when a rendered value matters. Not every app exposes a tree
    (terminals, and Electron/Chrome without --force-renderer-accessibility,
    expose little or nothing); when it does not, fall back to
    screenshot + zoom.""",
    "marks": """Set-of-Marks capture: a screenshot of the window WITH its accessible
    controls drawn as numbered red marks, plus a JSON legend mapping each
    number to the control's role, name, current value, and exact global
    position. One glance shows every control the accessibility tree knows:
    read the number off the image and look it up in the legend. `window`
    is an address from desktop (default: focused); `name` filters the
    marked controls. Falls back to the plain legend when ImageMagick is
    not installed, and to a fall-back-to-vision note when the app exposes
    no tree (then use screenshot + zoom).""",
    "binds": """The user's own Hyprland keybinds: combo, action, arg, and a
    description when the config provides one. This is how the desktop's
    owner drives it; read them to understand the owner's workflows. An
    action of `lua` means the bind is a closure in a Lua Hyprland config,
    so only its combo and description are readable. Running a bind is an
    acting operation and is disabled in read-only mode.""",
}

# Observation tools register in BOTH modes (docstrings become the MCP tool
# descriptions at registration time, hence the swap first). Acting tools
# register only outside read-only mode; clipboard is double-gated: opt-in
# env, never read-only.
_OBSERVE_TOOLS = (desktop, screenshot, zoom, ui, marks, binds, wait_for)
if READONLY:
    for _observe_tool in _OBSERVE_TOOLS:
        _observe_tool.__doc__ = _READONLY_DOCS.get(
            _observe_tool.__name__, _observe_tool.__doc__
        )


# Display names for the MCP tool annotations; clients show these in tool
# lists and approval prompts, where a bare `hypr` or `ui` says little.
_TITLES = {
    "desktop": "Desktop state",
    "screenshot": "Screenshot",
    "zoom": "Zoom in on a point",
    "ui": "Read app controls",
    "marks": "Screenshot with numbered controls",
    "binds": "List keybinds",
    "wait_for": "Wait for a desktop event",
    "pointer": "Mouse",
    "keyboard": "Keyboard",
    "click_ui": "Click a control by name",
    "hypr": "Windows and workspaces",
    "launch": "Launch an app",
    "use_bind": "Run a keybind",
    "sequence": "Run several actions",
    "clipboard": "Clipboard",
}


# One tool call at a time. mcp 1 ran a plain function tool on the event loop,
# so calls never overlapped; mcp 2 runs each on a worker thread, and two at
# once would share the held keys, the seat baseline and the journal.
_TOOL_LOCK = threading.Lock()


def _one_at_a_time(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)  # the SDK reads the signature and docstring through this
    def call(*args: Any, **kwargs: Any) -> Any:
        with _TOOL_LOCK:
            return fn(*args, **kwargs)
    return call


def _server(**kwargs: Any) -> Any:
    """The SDK's server class: MCPServer in mcp 2, FastMCP in mcp 1 (where
    the server reports the SDK's version, as it takes no version of its own)."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP("hypruse", **kwargs)
    return MCPServer("hypruse", version=__version__, **kwargs)


def build_app() -> Any:
    """The MCP application with the tools registered for this mode.
    Observation tools are annotated read-only and closed-world (they read
    the local desktop and change nothing); acting tools are annotated
    destructive, since a click or a keypress can do anything the user
    could."""
    from mcp.types import ToolAnnotations

    app_ = _server(instructions=_instructions)
    for observe_tool in _OBSERVE_TOOLS:
        name = observe_tool.__name__
        app_.tool(annotations=ToolAnnotations(
            title=_TITLES[name], readOnlyHint=True, openWorldHint=False
        ))(_one_at_a_time(observe_tool))
    if not READONLY:
        acting = [pointer, keyboard, click_ui, hypr, launch, use_bind, sequence]
        if CLIPBOARD:
            acting.append(clipboard)
        for acting_tool in acting:
            name = acting_tool.__name__
            app_.tool(annotations=ToolAnnotations(
                title=_TITLES[name], readOnlyHint=False, destructiveHint=True
            ))(_one_at_a_time(acting_tool))
    return app_


_app: Any = None  # assigned here so a reload (the read-only tests) starts over


def app() -> Any:
    """The application, built on first use rather than at import: importing
    FastMCP costs about a second, which the MCP server pays once and a
    shell verb, which never serves anything, must not pay at all. Reachable
    as `server.mcp` too, through the module __getattr__ below."""
    global _app
    if _app is None:
        _app = build_app()
    return _app


def __getattr__(name: str) -> Any:
    if name == "mcp":
        return app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    journal.start(__version__)  # HYPRUSE_JOURNAL: open the session record
    safety.on_shutdown(journal.stop)  # closed on the SIGTERM path too
    trust.init_marking()  # HYPRUSE_MARK: install the agent-owned border rule
    app().run()


if __name__ == "__main__":
    main()
