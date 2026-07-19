"""Synthetic input.

Pointer: positioning goes through `hyprctl dispatch movecursor` (global
logical coordinates, authoritative on any monitor layout); only button and
axis events go through the virtual-pointer wire client. This split
sidesteps the known multi-monitor mapping bugs of absolute virtual-pointer
motion (hyprwm/Hyprland#6749).

Keyboard: wtype, which uploads its own XKB keymap over
zwp_virtual_keyboard_v1, text lands layout-correct including unicode,
with no uinput scancode guessing.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from hypruse import hyprctl
from hypruse.wire import BUTTONS, PRESSED, RELEASED, VirtualPointer, WireError


class InputError(RuntimeError):
    """Invalid input request or missing input backend."""


MODS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "super": "logo",
    "meta": "logo",
    "win": "logo",
    "cmd": "logo",
    "altgr": "altgr",
}

KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pgup": "Page_Up",
    "pageup": "Page_Up",
    "pgdn": "Page_Down",
    "pagedown": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}


def parse_combo(combo: str) -> tuple[list[str], str | None]:
    """'ctrl+shift+t' → (['ctrl','shift'], 't'); 'super' alone is a bare-mod tap."""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise InputError("empty key combo")
    mods: list[str] = []
    key: str | None = None
    for i, part in enumerate(parts):
        lower = part.lower()
        last = i == len(parts) - 1
        if lower in MODS:
            mods.append(MODS[lower])
            if last:
                key = None
        elif not last:
            raise InputError(f"unknown modifier {part!r} in {combo!r}")
        elif lower in KEY_ALIASES:
            key = KEY_ALIASES[lower]
        elif len(part) == 1:
            key = part
        elif lower.startswith("f") and lower[1:].isdigit():
            key = part.upper()
        else:
            key = part  # assume a literal XKB keysym name (case-sensitive)
    return mods, key


def combo_to_wtype_args(mods: list[str], key: str | None) -> list[str]:
    args: list[str] = []
    for m in mods:
        args += ["-M", m]
    if key:
        args += ["-k", key]
    for m in reversed(mods):
        args += ["-m", m]
    if not args:
        raise InputError("combo resolved to nothing")
    return args


def _wtype(args: list[str], stdin: str | None = None) -> None:
    if shutil.which("wtype") is None:
        raise InputError("wtype not found, install wtype for keyboard input")
    proc = subprocess.run(
        ["wtype", *args], input=stdin, text=True, capture_output=True, timeout=15
    )
    if proc.returncode != 0:
        raise InputError(f"wtype failed: {proc.stderr.strip()}")


# There is ONE seat. MCP hosts can issue tool calls in parallel and the
# sync tool functions run on worker threads, so without serialization two
# overlapping calls could interleave a drag's press/move/release with
# another click's, or type into each other's focus. Reentrant because a
# locked drag calls the locked move for its path. The SIGTERM cleanup
# (release_held) deliberately does NOT take it: the handler can interrupt
# a holder on the same thread, and a dying process must not deadlock.
_seat_lock = threading.RLock()


def type_text(text: str) -> None:
    if text:
        with _seat_lock:
            _wtype(["-"], stdin=text)  # '-' reads stdin: safe for any content


def key_combo(combo: str) -> None:
    mods, key = parse_combo(combo)
    with _seat_lock:
        _wtype(combo_to_wtype_args(mods, key))


# --- pointer ----------------------------------------------------------------

_vp: VirtualPointer | None = None
_held_button: str | None = None


def release_held() -> None:
    """Best-effort release of a button an in-flight drag is holding.

    Registered on the SIGTERM path (safety.on_shutdown): a click's
    press/release pair never spans interpreter checkpoints where a signal
    could land with the button down, but a drag holds it across ~200 ms of
    cursor moves, and the kill switch must not end the process mid-hold."""
    global _held_button
    if _held_button and _vp is not None:
        with contextlib.suppress(Exception):
            _vp.button(_held_button, RELEASED)
    _held_button = None


def _with_pointer(fn: Callable[[VirtualPointer], Any]) -> Any:
    """Run fn with the shared virtual pointer, reconnecting once if stale."""
    global _vp
    for attempt in (0, 1):
        if _vp is None:
            _vp = VirtualPointer()
        try:
            return fn(_vp)
        except WireError:
            _vp = None
            if attempt:
                raise


def _check_button(button: str) -> None:
    if button not in BUTTONS:
        raise InputError(f"unknown button {button!r}; one of {sorted(BUTTONS)}")


def _check_xy(x: float | None, y: float | None) -> bool:
    if (x is None) != (y is None):
        raise InputError("give both x and y, or neither")
    return x is not None


def move(x: float, y: float) -> None:
    with _seat_lock:
        hyprctl.dispatch("movecursor", str(int(round(x))), str(int(round(y))))


def click(
    x: float | None = None,
    y: float | None = None,
    button: str = "left",
    double: bool = False,
) -> None:
    _check_button(button)
    has_xy = _check_xy(x, y)
    with _seat_lock:
        if has_xy:
            move(x, y)  # type: ignore[arg-type]
            time.sleep(0.02)
        _with_pointer(lambda p: p.click(button, double=double))


def drag(x1: float, y1: float, x2: float, y2: float, button: str = "left") -> None:
    _check_button(button)

    def run(p: VirtualPointer) -> None:
        global _held_button
        # flag BEFORE the press: the press blocks in a sync roundtrip with
        # the button already down compositor-side, and a SIGTERM landing
        # there must see the hold. Releasing an un-pressed button is
        # harmless; dying with one pressed is not.
        _held_button = button
        p.button(button, PRESSED)
        try:
            steps = 12
            for i in range(1, steps + 1):
                move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
                time.sleep(0.015)
        finally:
            p.button(button, RELEASED)
            _held_button = None

    with _seat_lock:
        move(x1, y1)
        time.sleep(0.03)
        _with_pointer(run)


def scroll(
    dy: float = 0.0, dx: float = 0.0, x: float | None = None, y: float | None = None
) -> None:
    if not dy and not dx:
        raise InputError("scroll needs a non-zero dy or dx")
    has_xy = _check_xy(x, y)
    with _seat_lock:
        if has_xy:
            move(x, y)  # type: ignore[arg-type]
            time.sleep(0.02)
        _with_pointer(lambda p: p.scroll(dy=dy, dx=dx))
