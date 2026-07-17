"""Accessibility tree via AT-SPI, read through the busctl CLI (no new deps).

AT-SPI publishes every accessible app's widget tree on a private D-Bus (the
"a11y bus"), independent of the display server, so it works on Wayland.
hypruse reads it by shelling out to busctl, the same pattern as
grim/wtype/hyprctl, and pairs it with hyprctl window geometry to turn
window-relative element positions into global click points.

Why window-relative, not screen: on Wayland an app does not know its own
global position, so AT-SPI SCREEN coordinates come back unreliable
(window-relative, zero-origin). WINDOW coordinates are reliable, and the
caller adds the window's global origin (hyprctl `at`) to get a real click
point. This module stays hyprctl-free and returns window-relative extents;
the server does the correlation and mapping.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class A11yError(RuntimeError):
    """The accessibility bus is unreachable or a busctl call failed."""


_ACCESSIBLE = "org.a11y.atspi.Accessible"
_COMPONENT = "org.a11y.atspi.Component"
_ACTION = "org.a11y.atspi.Action"
_REGISTRY_SVC = "org.a11y.atspi.Registry"
_ROOT = "/org/a11y/atspi/accessible/root"
_DBUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")

COORD_WINDOW = 1  # ATSPI_COORD_TYPE_WINDOW: extents relative to the toplevel

# AtspiStateType bit positions (GetState returns a 2-word uint32 bitfield)
_STATE_ENABLED, _STATE_SENSITIVE, _STATE_SHOWING, _STATE_VISIBLE = 8, 24, 25, 30

# Roles worth clicking or typing into, as AtspiRole ENUM NUMBERS (from
# GetRole). Numbers are matched, not GetRoleName strings, because the role
# name varies by toolkit (GTK reports a push button (enum 43) as "button",
# Qt as "push button") while the number is standardized.
ACTIONABLE_ROLE_NUMS = frozenset(
    {
        7,  # check box
        8,  # check menu item
        11,  # combo box
        33,  # menu
        35,  # menu item
        37,  # page tab
        40,  # password text
        43,  # push button
        44,  # radio button
        45,  # radio menu item
        51,  # slider
        52,  # spin button
        62,  # toggle button
        79,  # entry
        88,  # link
    }
)


def _busctl(address: str, verb: str, *args: str) -> Any:
    if shutil.which("busctl") is None:
        raise A11yError("busctl not found, install systemd for accessibility support")
    # the GetAddress broker lives on the session bus (--user); the a11y tree
    # itself lives on the private bus reached with --address
    where = ["--address", address] if address else ["--user"]
    argv = ["busctl", "--json=short", *where, verb, *args]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise A11yError(f"busctl {verb} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise A11yError(f"unparseable busctl output: {proc.stdout[:200]!r}") from exc


def bus_address() -> str:
    """Ask the session-bus broker for the private accessibility bus address.
    Raises A11yError if no a11y bus is running (no accessible apps)."""
    out = _busctl("", "call", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")
    data = out.get("data") or []
    if not data or not data[0]:
        raise A11yError("accessibility bus reported no address (no accessible apps?)")
    return str(data[0])


class Bus:
    """A connection to the a11y bus: every AT-SPI object is a (service,
    object-path) pair reached with these two calls."""

    def __init__(self, address: str):
        self.address = address

    def call(self, svc: str, path: str, iface: str, method: str, *sig_args: str) -> list[Any]:
        """Return the method's out-args as a list (data[0] is the first)."""
        return _busctl(self.address, "call", svc, path, iface, method, *sig_args)["data"]

    def prop(self, svc: str, path: str, iface: str, name: str) -> Any:
        """A property value (busctl returns it directly, not list-wrapped)."""
        return _busctl(self.address, "get-property", svc, path, iface, name)["data"]

    def conn_pid(self, svc: str) -> int | None:
        try:
            return int(self.call(*_DBUS, "GetConnectionUnixProcessID", "s", svc)[0])
        except (A11yError, ValueError, IndexError):
            return None


def connect() -> Bus:
    return Bus(bus_address())


def apps(bus: Bus) -> list[tuple[str, str]]:
    """Every registered application's root accessible, as (service, path)."""
    children = bus.call(_REGISTRY_SVC, _ROOT, _ACCESSIBLE, "GetChildren")[0]
    return [(svc, path) for svc, path in children]


def app_for_pid(bus: Bus, pid: int, title: str = "") -> tuple[str, str] | None:
    """The application accessible whose connection PID matches the window
    (exact for single-process apps). Falls back to matching a frame's name
    to the window title, which covers multi-process apps (e.g. Electron/Qt
    whose a11y connection PID differs from the window PID)."""
    registered = apps(bus)
    for svc, path in registered:
        if bus.conn_pid(svc) == pid:
            return (svc, path)
    if title:
        for svc, path in registered:
            if _has_frame_named(bus, svc, path, title):
                return (svc, path)
    return None


def _has_frame_named(bus: Bus, svc: str, path: str, title: str) -> bool:
    return any(_name(bus, cs, cp) == title for cs, cp in _children(bus, svc, path))


def _name(bus: Bus, svc: str, path: str) -> str:
    try:
        return str(bus.prop(svc, path, _ACCESSIBLE, "Name") or "")
    except A11yError:
        return ""


def _role_num(bus: Bus, svc: str, path: str) -> int:
    try:
        return int(bus.call(svc, path, _ACCESSIBLE, "GetRole")[0])
    except (A11yError, IndexError, ValueError):
        return -1


def _role_name(bus: Bus, svc: str, path: str) -> str:
    try:
        return str(bus.call(svc, path, _ACCESSIBLE, "GetRoleName")[0])
    except (A11yError, IndexError):
        return ""


def _children(bus: Bus, svc: str, path: str) -> list[tuple[str, str]]:
    try:
        return [(cs, cp) for cs, cp in bus.call(svc, path, _ACCESSIBLE, "GetChildren")[0]]
    except (A11yError, IndexError, ValueError):
        return []


def _window_extents(bus: Bus, svc: str, path: str) -> tuple[int, int, int, int] | None:
    try:
        x, y, w, h = bus.call(svc, path, _COMPONENT, "GetExtents", "u", str(COORD_WINDOW))[0]
        return int(x), int(y), int(w), int(h)
    except (A11yError, IndexError, ValueError):
        return None


def _states(bus: Bus, svc: str, path: str) -> set[int]:
    try:
        words = bus.call(svc, path, _ACCESSIBLE, "GetState")[0]
    except (A11yError, IndexError):
        return set()
    out: set[int] = set()
    for wi, word in enumerate(words):
        for bit in range(32):
            if word >> bit & 1:
                out.add(wi * 32 + bit)
    return out


def _clickable_now(states: set[int]) -> bool:
    return {_STATE_SHOWING, _STATE_VISIBLE, _STATE_SENSITIVE, _STATE_ENABLED} <= states


def find_elements(
    bus: Bus,
    app_svc: str,
    app_path: str,
    name: str = "",
    actionable: bool = True,
    max_nodes: int = 400,
    max_results: int = 60,
) -> list[dict[str, Any]]:
    """Depth-first over an app's tree, returning matching elements with
    WINDOW-relative extents (the caller adds the window origin). Filters by
    `name` substring (case-insensitive) and, when `actionable`, to
    interactive roles. Bounded by max_nodes / max_results so a huge app
    cannot hang the call."""
    needle = name.lower()
    results: list[dict[str, Any]] = []
    stack: list[tuple[str, str]] = [(app_svc, app_path)]
    visited = 0
    while stack and visited < max_nodes and len(results) < max_results:
        svc, path = stack.pop()
        visited += 1
        nm = _name(bus, svc, path)
        kids = _children(bus, svc, path)
        stack.extend(reversed(kids))  # keep document order under a LIFO stack
        if needle and needle not in nm.lower():
            continue
        if actionable and _role_num(bus, svc, path) not in ACTIONABLE_ROLE_NUMS:
            continue
        if not nm and not needle:
            continue  # unnamed elements are not targetable in a dump
        ext = _window_extents(bus, svc, path)
        if ext is None:
            continue
        states = _states(bus, svc, path)
        results.append(
            {
                "role": _role_name(bus, svc, path),
                "name": nm,
                "extent": ext,
                "clickable": _clickable_now(states),
                "svc": svc,
                "path": path,
            }
        )
    return results


def do_action(bus: Bus, svc: str, path: str, index: int = 0) -> bool:
    """Invoke an accessible's action (default index 0, usually the click),
    with no pointer, so it works even when the window is not visible."""
    try:
        return bool(bus.call(svc, path, _ACTION, "DoAction", "i", str(index))[0])
    except (A11yError, IndexError):
        return False
