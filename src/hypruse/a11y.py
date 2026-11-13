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
import math
import shutil
import subprocess
from typing import Any

from hypruse import dbuswire, journal


class A11yError(RuntimeError):
    """The accessibility bus is unreachable, or a call to it failed."""


class CallRefused(A11yError):
    """The app answered with an error: the object is gone, or lacks that
    interface. The readers below take it as "not there". Any other
    A11yError (no answer in time, a broken connection) propagates, so a
    failure is never reported as an empty name or a childless element."""


_ACCESSIBLE = "org.a11y.atspi.Accessible"
_COMPONENT = "org.a11y.atspi.Component"
_ACTION = "org.a11y.atspi.Action"
_TEXT = "org.a11y.atspi.Text"
_VALUE = "org.a11y.atspi.Value"
_REGISTRY_SVC = "org.a11y.atspi.Registry"
_ROOT = "/org/a11y/atspi/accessible/root"
_DBUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")

COORD_WINDOW = 1  # ATSPI_COORD_TYPE_WINDOW: extents relative to the toplevel

# AtspiStateType bit positions (GetState returns a 2-word uint32 bitfield)
_STATE_ENABLED, _STATE_SENSITIVE, _STATE_SHOWING, _STATE_VISIBLE = 8, 24, 25, 30
_STATE_FOCUSED = 12
_STATE_EDITABLE = 7

# AtspiRelationType: a label names a field through these
_REL_LABEL_FOR, _REL_LABELLED_BY = 1, 2
_LABEL_ROLE = 29

PASSWORD_ROLE = 40  # AtspiRole PASSWORD_TEXT: never read, never type without consent

# libatspi's names for the AtspiRole numbers, for an app that answers GetRole
# but not GetRoleName (AccessKit through at least 0.24)
_ROLE_NAMES = (
    "invalid", "accelerator label", "alert", "animation", "arrow", "calendar",
    "canvas", "check box", "check menu item", "color chooser", "column header",
    "combo box", "date editor", "desktop icon", "desktop frame", "dial", "dialog",
    "directory pane", "drawing area", "file chooser", "filler", "focus traversable",
    "font chooser", "frame", "glass pane", "html container", "icon", "image",
    "internal frame", "label", "layered pane", "list", "list item", "menu", "menu bar",
    "menu item", "option pane", "page tab", "page tab list", "panel", "password text",
    "popup menu", "progress bar", "button", "radio button", "radio menu item",
    "root pane", "row header", "scroll bar", "scroll pane", "separator", "slider",
    "spin button", "split pane", "status bar", "table", "table cell",
    "table column header", "table row header", "tearoff menu item", "terminal", "text",
    "toggle button", "tool bar", "tool tip", "tree", "tree table", "unknown",
    "viewport", "window", "extended", "header", "footer", "paragraph", "ruler",
    "application", "autocomplete", "editbar", "embedded", "entry", "chart", "caption",
    "document frame", "heading", "page", "section", "redundant object", "form", "link",
    "input method window", "table row", "tree item", "document spreadsheet",
    "document presentation", "document text", "document web", "document email",
    "comment", "list box", "grouping", "image map", "notification", "info bar",
    "level bar", "title bar", "block quote", "audio", "video", "definition", "article",
    "landmark", "log", "marquee", "math", "rating", "timer", "static", "math fraction",
    "math root", "subscript", "superscript", "description list", "description term",
    "description value", "footnote", "content deletion", "content insertion", "mark",
    "suggestion", "push button menu", "switch",
)

# Window-relative coordinates beyond this are toolkit noise, not geometry
_EXTENT_SANITY = 20000

# Roles whose CURRENT VALUE is worth reading, and how to read it. Reading is
# gated on role so the common case (find a button) costs no extra busctl
# calls. PASSWORD_TEXT (40) is deliberately absent: never read its contents.
_STATE_CHECKED, _STATE_PRESSED = 4, 20
_CHECKABLE_ROLES = frozenset({7, 8, 44, 45, 62})  # check box/menu item, radio, toggle
_TEXT_ROLES = frozenset({61, 79})  # text, entry
_VALUE_ROLES = frozenset({51, 52})  # slider, spin button
_MAX_TEXT = 200  # a text area can hold a whole document; report a readable head
_VALUE_BEARING_ROLES = _CHECKABLE_ROLES | _TEXT_ROLES | _VALUE_ROLES

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
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise A11yError(f"busctl {verb} timed out (unresponsive app?)") from exc
    if proc.returncode != 0:
        raise CallRefused(f"busctl {verb} failed: {proc.stderr.strip() or proc.stdout.strip()}")
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


# busctl-style string arguments, typed for the wire
_INT_TYPES = frozenset("ynqiuxth")


def _typed(sig: str, args: tuple[str, ...]) -> list[Any]:
    types = dbuswire.split_signature(sig)
    if len(types) != len(args):
        raise A11yError(f"signature {sig!r} takes {len(types)} arguments, got {len(args)}")
    out: list[Any] = []
    for t, v in zip(types, args, strict=True):
        if t in _INT_TYPES:
            out.append(int(v))
        elif t == "b":
            out.append(str(v).lower() in ("true", "yes", "1"))
        elif t == "d":
            out.append(float(v))
        elif t in ("s", "o", "g"):
            out.append(str(v))
        else:
            raise A11yError(f"cannot pass a {t!r} argument")
    return out


class Bus:
    """A connection to the a11y bus: every AT-SPI object is a (service,
    object-path) pair reached with these two calls. They go over one
    socket held open for the process (dbuswire), or through busctl when
    that cannot connect."""

    def __init__(self, address: str):
        self.address = address
        self._busctl = False  # set once the socket could not be reached

    def _wire(self) -> dbuswire.Connection | None:
        if self._busctl:
            return None
        try:
            return dbuswire.connection(self.address)
        except dbuswire.Unreachable:
            self._busctl = True
            return None

    def call(self, svc: str, path: str, iface: str, method: str, *sig_args: str) -> list[Any]:
        """Return the method's out-args as a list (data[0] is the first).
        `sig_args` is busctl's form: the signature, then each argument as a
        string."""
        wire = self._wire()
        if wire is None:
            return _busctl(self.address, "call", svc, path, iface, method, *sig_args)["data"]
        sig, args = (sig_args[0], sig_args[1:]) if sig_args else ("", ())
        try:
            return wire.call(svc, path, iface, method, sig, _typed(sig, args))
        except dbuswire.CallError as exc:
            raise CallRefused(f"{method} failed: {exc}") from None
        except dbuswire.DBusError as exc:
            raise A11yError(f"{method} failed: {exc}") from None

    def call_many(self, calls: list[tuple[str, ...]]) -> list[Any]:
        """Several calls, each written as for `call`, sent together over the
        socket and answered in one round trip; through busctl, one by one.
        Each result is the out-args list or the CallRefused that call would
        have raised; a lost connection raises A11yError for the batch."""
        wire = self._wire()
        if wire is None:
            out: list[Any] = []
            for c in calls:
                try:
                    if c[2] == _PROPERTIES and c[3] == "Get":  # busctl call keeps the variant
                        out.append([self.prop(c[0], c[1], c[5], c[6])])
                    else:
                        out.append(self.call(*c))
                except CallRefused as exc:
                    out.append(exc)
            return out
        batch = []
        for svc, path, iface, method, *sig_args in calls:
            if sig_args:
                sig = sig_args[0]
                batch.append((svc, path, iface, method, sig, tuple(_typed(sig, sig_args[1:]))))
            else:
                batch.append((svc, path, iface, method, "", ()))
        try:
            replies = wire.call_many(batch)
        except dbuswire.DBusError as exc:
            raise A11yError(f"{len(calls)} calls failed: {exc}") from None
        return [CallRefused(f"{c[3]} failed: {r}") if isinstance(r, dbuswire.CallError) else r
                for c, r in zip(calls, replies, strict=True)]

    def prop(self, svc: str, path: str, iface: str, name: str) -> Any:
        """A property value (not list-wrapped)."""
        if self._wire() is None:
            return _busctl(self.address, "get-property", svc, path, iface, name)["data"]
        return self.call(svc, path, "org.freedesktop.DBus.Properties", "Get", "ss", iface, name)[0]

    def conn_pid(self, svc: str) -> int | None:
        try:
            return int(self.call(*_DBUS, "GetConnectionUnixProcessID", "s", svc)[0])
        except (CallRefused, ValueError, IndexError):
            return None


def connect() -> Bus:
    return Bus(bus_address())


def apps(bus: Bus) -> list[tuple[str, str]]:
    """Every registered application's root accessible, as (service, path)."""
    children = bus.call(_REGISTRY_SVC, _ROOT, _ACCESSIBLE, "GetChildren")[0]
    return [(svc, path) for svc, path in children]


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1]) or None
    except (OSError, ValueError, IndexError):
        pass
    return None


def _flatpak_instance(pid: int) -> str | None:
    """The Flatpak instance id of a sandboxed process, or None."""
    try:
        with open(f"/proc/{pid}/root/.flatpak-info") as f:
            for line in f:
                if line.startswith("instance-id="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _cgroup(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return f.read().strip() or None
    except OSError:
        return None


def same_app(conn_pid: int | None, window_pid: int | None) -> bool:
    """Whether an accessibility connection's process belongs to the same
    app instance as a window's process: the same process, one an ancestor
    of the other, one Flatpak instance (the bus sees Flatpak's D-Bus proxy,
    not the app), or one systemd scope or service of the app's own (uwsm
    and Flatpak start each app in one). A session-wide cgroup proves
    nothing. Used before acting on an object found under another process's
    root, which is how a multi-process app is matched by title."""
    if not conn_pid or not window_pid:
        return False
    if conn_pid == window_pid:
        return True
    for a, b in ((conn_pid, window_pid), (window_pid, conn_pid)):
        p: int | None = a
        for _ in range(32):
            p = _ppid(p) if p else None
            if p is None or p <= 1:
                break
            if p == b:
                return True
    fa, fb = _flatpak_instance(conn_pid), _flatpak_instance(window_pid)
    if fa is not None and fa == fb:
        return True
    ca, cb = _cgroup(conn_pid), _cgroup(window_pid)
    leaf = (ca or "").rsplit("/", 1)[-1]
    return ca is not None and ca == cb and leaf.startswith("app-") and (
        leaf.endswith(".scope") or leaf.endswith(".service"))


def app_for_pid(
    bus: Bus, pid: int, title: str = "", size: tuple[int, int] | None = None
) -> tuple[str, str] | None:
    """The application accessible whose connection PID matches the window
    (exact for single-process apps). A process can register several roots
    under one PID (Qt with QT_QPA_PLATFORMTHEME=gtk3 bridges through GTK and
    registers a second, toolkit-native root), and registration order says
    nothing about which one carries the window: the GTK bridge root comes
    first and exposes no children, until the app opens a native GTK file,
    color or font dialog, whose frame then hangs off it. So the first
    same-PID root holding THIS window (a frame named like `title`, or of
    the window's `size`) wins, and the scan stops there: each PID lookup is
    a busctl call, and the usual one-root app should not pay one per
    registered root. Without a match, the first same-PID root with
    children, then the first empty one. Falls back to matching a frame's
    name to the window title across all roots, which covers multi-process
    apps (e.g. Electron/Qt whose a11y connection PID differs from the
    window PID)."""
    registered = apps(bus)
    nonempty = empty = None
    for svc, path in registered:
        if bus.conn_pid(svc) != pid:
            continue
        frames = _children(bus, svc, path)
        if not frames:  # an empty root cannot hold the window
            empty = empty or (svc, path)
            continue
        if not (title or size) or _holds_window(bus, frames, title, size):
            return (svc, path)
        nonempty = nonempty or (svc, path)
    if nonempty or empty:
        return nonempty or empty
    if title:
        for svc, path in registered:
            if _has_frame_named(bus, svc, path, title):
                return (svc, path)
    return None


def _holds_window(
    bus: Bus,
    frames: list[tuple[str, str]],
    title: str,
    size: tuple[int, int] | None,
) -> bool:
    """Whether one of these toplevel frames is the window, by window_frame's
    own criteria: its name is the title, or its extent is the window's size
    (Chrome's frame names never equal its titles)."""
    if title and any(_name(bus, fs, fp) == title for fs, fp in frames):
        return True
    if size:
        for fs, fp in frames:
            ext = _window_extents(bus, fs, fp)
            if ext and (ext[2], ext[3]) == tuple(size):
                return True
    return False


def _has_frame_named(bus: Bus, svc: str, path: str, title: str) -> bool:
    return any(_name(bus, cs, cp) == title for cs, cp in _children(bus, svc, path))


def window_frame(
    bus: Bus,
    app_svc: str,
    app_path: str,
    title: str = "",
    size: tuple[int, int] | None = None,
) -> tuple[str, str]:
    """The app's toplevel matching a SPECIFIC window (by accessible name ==
    title, then by extent size), so a multi-window app's OTHER windows are
    not walked and mapped with the wrong origin. Returns the app root when
    there is a single toplevel or no confident match (walking from the root
    is then correct or the best available)."""
    frames = _children(bus, app_svc, app_path)
    if len(frames) <= 1:
        return (app_svc, app_path)
    if title:
        for fs, fp in frames:
            if _name(bus, fs, fp) == title:
                return (fs, fp)
    if size:
        for fs, fp in frames:
            ext = _window_extents(bus, fs, fp)
            if ext and (ext[2], ext[3]) == tuple(size):
                return (fs, fp)
    return (app_svc, app_path)


def window_frame_for(
    bus: Bus,
    app_svc: str,
    app_path: str,
    title: str,
    size: tuple[int, int] | None,
    peers: list[tuple[str, tuple[int, int]]],
) -> tuple[str, str] | None:
    """The toplevel that is provably THIS window, or None when that cannot
    be told. The background paths act on the object directly, so a guess
    would press or type in another window of the same app, the human's
    included. `peers` are the (title, size) of the app's other windows in
    Hyprland: a title or size one of them shares could pick its frame, so
    it proves nothing. Proof is exactly one frame named `title`, or else
    exactly one frame of `size`. A lone frame is the window when the app has
    no other window."""
    frames = _children(bus, app_svc, app_path)
    if len(frames) == 1 and not peers:
        return (app_svc, app_path)
    if title and all(t != title for t, _ in peers):
        named = [f for f in frames if _name(bus, *f) == title]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            return None
    if size and all(tuple(z) != tuple(size) for _, z in peers):
        sized = [f for f in frames
                 if (ext := _window_extents(bus, *f)) and (ext[2], ext[3]) == tuple(size)]
        if len(sized) == 1:
            return sized[0]
    return None


def _name(bus: Bus, svc: str, path: str) -> str:
    try:
        return str(bus.prop(svc, path, _ACCESSIBLE, "Name") or "")
    except CallRefused:
        return ""


def _role_num(bus: Bus, svc: str, path: str) -> int:
    try:
        return int(bus.call(svc, path, _ACCESSIBLE, "GetRole")[0])
    except (CallRefused, IndexError, ValueError):
        return -1


def _role_name(bus: Bus, svc: str, path: str, role_num: int) -> str:
    try:
        reply: Any = bus.call(svc, path, _ACCESSIBLE, "GetRoleName")
    except CallRefused as exc:
        reply = exc
    return _role_name_from(reply, role_num)


def _role_name_from(reply: Any, role_num: int) -> str:
    """A GetRoleName reply (or its CallRefused), named from the role number
    when the toolkit gives none (AccessKit has no GetRoleName)."""
    try:
        name = "" if isinstance(reply, CallRefused) else str(reply[0])
    except IndexError:
        name = ""
    if name or not 0 <= role_num < len(_ROLE_NAMES):
        return name
    return _ROLE_NAMES[role_num]


def _children(bus: Bus, svc: str, path: str) -> list[tuple[str, str]]:
    try:
        return [(cs, cp) for cs, cp in bus.call(svc, path, _ACCESSIBLE, "GetChildren")[0]]
    except (CallRefused, IndexError, ValueError):
        return []


def _window_extents(bus: Bus, svc: str, path: str) -> tuple[int, int, int, int] | None:
    """Window-relative extents, or None when the widget has no usable
    geometry. Toolkits report degenerate or wild extents for things that are
    not rendered (GTK notebook scroll arrows come back 8x0; widgets on an
    unrendered tab page report absurd origins), and those must not be
    offered as click targets."""
    try:
        reply: Any = bus.call(svc, path, _COMPONENT, "GetExtents", "u", str(COORD_WINDOW))
    except CallRefused as exc:
        reply = exc
    return _extents_from(reply)


def _extents_from(reply: Any) -> tuple[int, int, int, int] | None:
    """A GetExtents reply (or its CallRefused) read as _window_extents reads it."""
    try:
        if isinstance(reply, CallRefused):
            return None
        x, y, w, h = reply[0]
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (IndexError, ValueError, TypeError):
        return None
    if w <= 0 or h <= 0:
        return None
    if not (-_EXTENT_SANITY <= x <= _EXTENT_SANITY and -_EXTENT_SANITY <= y <= _EXTENT_SANITY):
        return None
    return x, y, w, h


def _states(bus: Bus, svc: str, path: str) -> set[int]:
    try:
        words = bus.call(svc, path, _ACCESSIBLE, "GetState")[0]
    except (CallRefused, IndexError):
        return set()
    out: set[int] = set()
    for wi, word in enumerate(words):
        for bit in range(32):
            if word >> bit & 1:
                out.add(wi * 32 + bit)
    return out


def _interfaces(bus: Bus, svc: str, path: str) -> set[str]:
    """Short interface names the object supports, so Text/Value are only
    called where they exist (calling them blind raises UnknownMethod)."""
    try:
        reply: Any = bus.call(svc, path, _ACCESSIBLE, "GetInterfaces")
    except CallRefused as exc:
        reply = exc
    return _interfaces_from(reply)


def _interfaces_from(reply: Any) -> set[str]:
    try:
        if isinstance(reply, CallRefused):
            return set()
        return {i.rsplit(".", 1)[-1] for i in reply[0]}
    except (IndexError, TypeError, AttributeError):
        return set()


def element_value(bus: Bus, svc: str, path: str, role: int, states: set[int]) -> dict[str, Any]:
    """The element's CURRENT VALUE, as opposed to its label: what is typed
    into it, where a slider sits, whether a box is ticked. Returns the keys
    that apply ({} for a plain button), because the accessible name alone
    says a control called "Volume" exists without saying it is at 66%.

    Gated on role: checkable state is free (already in `states`), Text and
    Value cost one call each and only for roles that can carry them. A
    password field's contents are never read."""
    if role in _CHECKABLE_ROLES:
        return {"checked": _STATE_CHECKED in states or _STATE_PRESSED in states}
    if role in _TEXT_ROLES:
        if "Text" not in _interfaces(bus, svc, path):
            return {}
        try:
            count = int(bus.prop(svc, path, _TEXT, "CharacterCount"))
            if count <= 0:
                return {"value": ""}
            end = min(count, _MAX_TEXT)
            text = str(bus.call(svc, path, _TEXT, "GetText", "ii", "0", str(end))[0])
            return {"value": text + ("..." if count > end else "")}
        except (CallRefused, IndexError, ValueError, TypeError):
            return {}
    if role in _VALUE_ROLES:
        if "Value" not in _interfaces(bus, svc, path):
            return {}
        try:
            current = float(bus.prop(svc, path, _VALUE, "CurrentValue"))
            low = float(bus.prop(svc, path, _VALUE, "MinimumValue"))
            high = float(bus.prop(svc, path, _VALUE, "MaximumValue"))
        except (CallRefused, ValueError, TypeError):
            return {}
        out: dict[str, Any] = {"value": current}
        if high > low:  # raw units are toolkit-specific; a percent is readable
            out.update(_percent(current, low, high))
        return out
    return {}


def _percent(current: float, low: float, high: float) -> dict[str, int]:
    """Where a value sits in its range, or nothing when the toolkit reports
    NaN or infinity (round() raises on them, which failed the whole read)."""
    pct = (current - low) / (high - low) * 100
    return {"percent": round(pct)} if math.isfinite(pct) else {}


def _clickable_now(states: set[int]) -> bool:
    """Showing, visible, and responsive to input. SENSITIVE and ENABLED
    overlap but toolkits do not set them together (GTK notebook tabs report
    SENSITIVE without ENABLED yet click fine), so either satisfies the
    responsive half rather than requiring both."""
    if not {_STATE_SHOWING, _STATE_VISIBLE} <= states:
        return False
    return _STATE_SENSITIVE in states or _STATE_ENABLED in states


# How many elements one walk of a window reads before it stops. Reading in
# batches made a walk about 5 times faster on a wide tree (a GTK 3 list) and
# about 2 times on a deep one (GTK 4, Qt 6), so a full walk of 6000 takes
# about what 2000 took before on a wide tree and up to twice it on a deep
# one, while the checks before typing see three times as much of an app.
WALK_BUDGET = 6000


_PROPERTIES = "org.freedesktop.DBus.Properties"
_PREFETCH = 64  # objects whose basics one round trip asks for


def _state_bits(words: Any) -> set[int]:
    out: set[int] = set()
    for wi, word in enumerate(words):
        for bit in range(32):
            if int(word) >> bit & 1:
                out.add(wi * 32 + bit)
    return out


class _Walk:
    """Depth-first over a subtree in document order, each object once, at
    most `max_nodes` of them. Iterating yields (svc, path); `skip()` during
    an iteration step leaves that object's children out. `truncated` says
    afterwards whether objects were left unread, so a caller can tell
    "not there" from "not reached". Each object once, because a toolkit can
    list one under two parents (Qt WebEngine does with a web page's
    subtrees), and walking the copy spends the budget twice.

    What a caller reads of each object (`fields`: name, role, children,
    states) comes in batches: the object about to be yielded and the next
    ones on the stack, every call of the batch in one round trip, read with
    the same rules as _name, _role_num, _children and _states. Everything is
    asked of the app live. Not the AT-SPI cache: GTK 4's keeps hidden
    widgets it once realized and leaves out shown ones it has not, so a
    cached child list can hide a password field the live one shows."""

    def __init__(self, bus: Bus, root: tuple[str, str], max_nodes: int,
                 fields: tuple[str, ...] = ("name", "role", "children"),
                 state_roles: frozenset[int] = frozenset()):
        self._bus, self._root, self._max = bus, root, max_nodes
        self._state_roles = state_roles
        self.parents: dict[tuple[str, str], tuple[str, str]] = {}
        self._want = tuple(dict.fromkeys((*fields, "children")))  # the walk itself needs children
        self._info: dict[tuple[str, str], dict[str, Any]] = {}
        self._skip = False
        self.truncated = False

    def skip(self) -> None:
        self._skip = True

    def name(self, node: tuple[str, str]) -> str:
        return self._get(node, "name")

    def role(self, node: tuple[str, str]) -> int:
        return self._get(node, "role")

    def states(self, node: tuple[str, str]) -> set[int]:
        return self._get(node, "states")

    def children(self, node: tuple[str, str]) -> list[tuple[str, str]]:
        return self._get(node, "children")

    def _get(self, node: tuple[str, str], key: str) -> Any:
        if key not in self._info.get(node, {}):
            self._fetch([node], (key,))
        return self._info[node][key]

    def _fetch(self, nodes: list[tuple[str, str]], keys: tuple[str, ...]) -> None:
        calls: list[tuple[str, ...]] = []
        plan: list[tuple[tuple[str, str], str]] = []
        for node in nodes:
            have = self._info.setdefault(node, {})
            svc, path = node
            for key in keys:
                if key in have:
                    continue
                if key == "name":
                    calls.append((svc, path, _PROPERTIES, "Get", "ss", _ACCESSIBLE, "Name"))
                elif key == "role":
                    calls.append((svc, path, _ACCESSIBLE, "GetRole"))
                elif key == "children":
                    calls.append((svc, path, _ACCESSIBLE, "GetChildren"))
                else:
                    calls.append((svc, path, _ACCESSIBLE, "GetState"))
                plan.append((node, key))
        for (node, key), reply in zip(plan, self._bus.call_many(calls), strict=True):
            self._info[node][key] = _read(key, reply)
        if "role" in keys and self._state_roles:
            text_nodes = [n for n in nodes if self._info[n].get("role") in self._state_roles
                          and "states" not in self._info[n]]
            if text_nodes:
                self._fetch(text_nodes, ("states",))

    def __iter__(self):
        stack: list[tuple[str, str]] = [self._root]
        done: set[tuple[str, str]] = set()
        while stack:
            node = stack.pop()
            if node in done:
                continue
            if len(done) >= self._max:
                self.truncated = True
                return
            done.add(node)
            if any(k not in self._info.get(node, {}) for k in self._want):
                # this object and the next ones the walk will reach, together
                batch = [node]
                for nxt in reversed(stack):
                    if len(batch) >= _PREFETCH:
                        break
                    if nxt not in done and nxt not in batch:
                        batch.append(nxt)
                self._fetch(batch, self._want)
            self._skip = False
            yield node
            if not self._skip:  # reversed keeps document order under a LIFO stack
                children = self.children(node)
                for child in children:
                    if child not in done:
                        self.parents[child] = node
                stack.extend(reversed(children))


def _read(key: str, reply: Any) -> Any:
    """A batched reply read as _name, _role_num, _children or _states reads
    its own call's reply, CallRefused included."""
    refused = isinstance(reply, CallRefused)
    try:
        if key == "name":
            return "" if refused else str(reply[0] or "")
        if key == "role":
            return -1 if refused else int(reply[0])
        if key == "children":
            return [] if refused else [(cs, cp) for cs, cp in reply[0]]
        return set() if refused else _state_bits(reply[0])
    except (IndexError, ValueError, TypeError):
        return {"name": "", "role": -1, "children": [], "states": set()}[key]


_ExtentTransform = tuple[int, int, int, int, float, float]


def _document_transform(
    bus: Bus, node: tuple[str, str], parent: tuple[str, str]
) -> _ExtentTransform | None:
    """Calibrate Chromium's physical-pixel web subtree against its logical
    Views container. Browser chrome and GTK/Qt must keep their own units.
    Compare both dimensions rather than assuming the monitor scale: newer
    Chromium versions may already return logical web bounds."""
    doc, host = _window_extents(bus, *node), _window_extents(bus, *parent)
    if doc is None or host is None:
        return None
    px, py, pw, ph = doc
    lx, ly, lw, lh = host
    sx, sy = pw / lw, ph / lh
    if abs(sx - 1) <= 0.01 or abs(sx - sy) > 2 / min(lw, lh):
        return None
    try:
        attrs = bus.call(*parent, _ACCESSIBLE, "GetAttributes")[0]
    except (CallRefused, IndexError):
        return None
    if not isinstance(attrs, dict) or not attrs.get("class") or "tag" in attrs:
        return None  # an iframe's web parent is not a logical Views container
    return px, py, lx, ly, sx, sy


def find_elements(
    bus: Bus,
    app_svc: str,
    app_path: str,
    name: str = "",
    actionable: bool = True,
    max_nodes: int = WALK_BUDGET,
    max_results: int = 60,
) -> tuple[list[dict[str, Any]], bool]:
    """Depth-first over a window's subtree, returning (matching elements,
    truncated). Elements carry logical WINDOW-relative extents (the caller
    adds the window origin). Chromium web bounds are calibrated against
    their native container; the toolkit's raw units stay inside this module.
    Filters by `name` substring (case-insensitive) and, when
    `actionable`, to interactive roles. `truncated` is True when the walk hit
    the max_nodes budget before exhausting the tree, so the caller can tell
    'nothing there' from 'stopped early' instead of reporting a false
    absence."""
    needle = name.lower()
    results: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []  # named, with an action, in a role not listed
    seen: set[tuple[str, str]] = set()  # a field found through its label is not listed twice
    # elements the main path will build, in walk order, built a batch at a
    # time: a result's states, extents, role name and value in one round
    # trip with the others' instead of one call after another
    pending: list[tuple[str, str, int, str]] = []

    def flush() -> None:
        for (svc, path, _r, _n), item in zip(pending, _elements(bus, pending), strict=True):
            if item is not None and len(results) < max_results:
                seen.add((svc, path))
                results.append(item)
        pending.clear()

    walk = _Walk(bus, (app_svc, app_path), max_nodes, state_roles=_TEXT_ROLES)
    transforms: dict[tuple[str, str], _ExtentTransform] = {}
    chromium = None
    for svc, path in walk:
        if len(results) >= max_results:
            break
        node = (svc, path)
        role_num = walk.role(node)  # also gates the value read below
        parent = walk.parents.get(node)
        transform = transforms.get(parent)
        if role_num == 95 and parent is not None and transform is None:  # document web
            if chromium is None:
                chromium = toolkit(bus, app_svc)[0].lower() == "chromium"
            if chromium:
                transform = _document_transform(bus, node, parent)
        if transform is not None:
            transforms[node] = transform
        nm = walk.name((svc, path))
        if (needle and needle not in nm.lower()) or (svc, path) in seen:
            continue
        if needle and role_num == _LABEL_ROLE:
            # a name search: the field this label names (GTK's entries are
            # unnamed text, named only by the label beside them). The queue
            # goes first, so results keep walk order and `seen` is current.
            flush()
            if len(results) >= max_results:
                break
            for fs, fp in _related(bus, svc, path, _REL_LABEL_FOR):
                if (fs, fp) in seen:
                    continue
                field = _element(bus, fs, fp, _role_num(bus, fs, fp), nm, actionable=True)
                if field is not None:
                    seen.add((fs, fp))
                    results.append(field)
            if actionable:
                continue  # the label itself is not something to click
        editable = role_num in _TEXT_ROLES and _STATE_EDITABLE in walk.states(node)
        if actionable and role_num not in ACTIONABLE_ROLE_NUMS and not editable:
            # a name can still find a control in a role the list leaves out (a
            # web app's list row or cell) when it has an action, but only when
            # no listed control matches: a name that found one keeps finding it
            if needle and len(fallback) < max_results and _has_actions(bus, svc, path):
                item = _element(bus, svc, path, role_num, nm, actionable=False)
                if item is not None:
                    item["action"] = action_name(bus, svc, path)
                    fallback.append(item)
            continue
        if editable and not nm:
            nm = next((_name(bus, ls, lp) for ls, lp in
                       _related(bus, svc, path, _REL_LABELLED_BY)), "")
        if not nm and not needle and role_num not in _VALUE_BEARING_ROLES:
            # unnamed and valueless: nothing to target or report. An unnamed
            # slider or checkbox still carries a reading worth returning.
            continue
        pending.append((svc, path, role_num, nm))
        # never more queued than the cap has room for: when they all build,
        # the walk stops where reading one at a time stopped
        if len(pending) >= min(_ELEMENT_BATCH, max_results - len(results)):
            flush()
    flush()
    results = results or fallback
    for item in results:
        node = (item["svc"], item["path"])
        # A label relation can return a sibling field before the walk gets
        # to it, including the last result before the cap stops traversal.
        transform = transforms.get(node) or transforms.get(walk.parents.get(node))
        if transform is not None:
            px, py, lx, ly, sx, sy = transform
            x, y, w, h = item["extent"]
            item["extent"] = (round(lx + (x - px) / sx), round(ly + (y - py) / sy),
                              max(1, round(w / sx)), max(1, round(h / sy)))
    return results, walk.truncated and len(results) < max_results


_ELEMENT_BATCH = 32  # elements whose details one batch of calls reads


def _elements(bus: Bus, cands: list[tuple[str, str, int, str]]) -> list[dict[str, Any] | None]:
    """_element(..., actionable=False) for each (svc, path, role, name), its
    calls batched: states, extents and role name in one round trip, then the
    interfaces of the text and value elements, then their value reads. Each
    reply is read by the same helpers the one-call path uses, so every record
    is the one _element would build."""
    if not cands:
        return []
    calls: list[tuple[str, ...]] = []
    for svc, path, _role, _nm in cands:
        calls += [(svc, path, _ACCESSIBLE, "GetState"),
                  (svc, path, _COMPONENT, "GetExtents", "u", str(COORD_WINDOW)),
                  (svc, path, _ACCESSIBLE, "GetRoleName")]
    replies = bus.call_many(calls)
    parts: list[tuple[set[int], tuple[int, int, int, int], str] | None] = []
    for i, (_svc, _path, role, _nm) in enumerate(cands):
        st, ext, rn = replies[3 * i:3 * i + 3]
        extents = _extents_from(ext)
        parts.append(None if extents is None else
                     (_read("states", st), extents, _role_name_from(rn, role)))
    values = _values_of(bus, [(i, cands[i], parts[i][0]) for i in range(len(cands))
                              if parts[i] is not None])
    out: list[dict[str, Any] | None] = []
    for i, (svc, path, role, nm) in enumerate(cands):
        part = parts[i]
        if part is None:
            out.append(None)
            continue
        states, extents, role_name = part
        out.append({
            "role": role_name,
            "name": nm,
            "extent": extents,
            "clickable": _clickable_now(states),
            **values[i],
            "svc": svc,
            "path": path,
            "field": (role in _TEXT_ROLES or role == PASSWORD_ROLE) and _STATE_EDITABLE in states,
        })
    return out


def _values_of(
    bus: Bus, items: list[tuple[int, tuple[str, str, int, str], set[int]]]
) -> dict[int, dict[str, Any]]:
    """element_value for each (index, candidate, states), its calls batched
    the same way: interfaces of the text and value elements in one round
    trip, then CharacterCount or the three Value properties, then GetText."""
    out: dict[int, dict[str, Any]] = {}
    asked: list[tuple[int, str, str, int]] = []
    for i, (svc, path, role, _nm), states in items:
        if role in _CHECKABLE_ROLES:
            out[i] = {"checked": _STATE_CHECKED in states or _STATE_PRESSED in states}
        elif role in _TEXT_ROLES or role in _VALUE_ROLES:
            asked.append((i, svc, path, role))
        else:
            out[i] = {}
    if not asked:
        return out
    ifaces = bus.call_many([(svc, path, _ACCESSIBLE, "GetInterfaces")
                            for _i, svc, path, _r in asked])
    props: list[tuple[int, str, str, int]] = []
    for (i, svc, path, role), reply in zip(asked, ifaces, strict=True):
        if ("Text" if role in _TEXT_ROLES else "Value") in _interfaces_from(reply):
            props.append((i, svc, path, role))
        else:
            out[i] = {}
    calls: list[tuple[str, ...]] = []
    for _i, svc, path, role in props:
        if role in _TEXT_ROLES:
            calls.append((svc, path, _PROPERTIES, "Get", "ss", _TEXT, "CharacterCount"))
        else:
            calls += [(svc, path, _PROPERTIES, "Get", "ss", _VALUE, name)
                      for name in ("CurrentValue", "MinimumValue", "MaximumValue")]
    replies = bus.call_many(calls)
    texts: list[tuple[int, str, str, int, int]] = []  # (index, svc, path, count, end)
    at = 0
    for i, svc, path, role in props:
        if role in _TEXT_ROLES:
            reply = replies[at]
            at += 1
            try:
                if isinstance(reply, CallRefused):
                    raise reply
                count = int(reply[0])
            except (CallRefused, IndexError, ValueError, TypeError):
                out[i] = {}
                continue
            if count <= 0:
                out[i] = {"value": ""}
            else:
                texts.append((i, svc, path, count, min(count, _MAX_TEXT)))
        else:
            cur, low, high = replies[at:at + 3]
            at += 3
            try:
                if any(isinstance(r, CallRefused) for r in (cur, low, high)):
                    raise CallRefused("a Value property")
                current, lo, hi = float(cur[0]), float(low[0]), float(high[0])
            except (CallRefused, IndexError, ValueError, TypeError):
                out[i] = {}
                continue
            value: dict[str, Any] = {"value": current}
            if hi > lo:  # raw units are toolkit-specific; a percent is readable
                value.update(_percent(current, lo, hi))
            out[i] = value
    got = bus.call_many([(svc, path, _TEXT, "GetText", "ii", "0", str(end))
                         for _i, svc, path, _c, end in texts])
    for (i, _svc, _path, count, end), reply in zip(texts, got, strict=True):
        try:
            if isinstance(reply, CallRefused):
                raise reply
            text = str(reply[0])
        except (CallRefused, IndexError, ValueError, TypeError):
            out[i] = {}
            continue
        out[i] = {"value": text + ("..." if count > end else "")}
    return out


def _related(bus: Bus, svc: str, path: str, rel_type: int) -> list[tuple[str, str]]:
    """The targets of one AT-SPI relation type (LABEL_FOR, LABELLED_BY)."""
    try:
        rels = bus.call(svc, path, _ACCESSIBLE, "GetRelationSet")[0]
    except (CallRefused, IndexError):
        return []
    out: list[tuple[str, str]] = []
    for kind, targets in rels:
        if int(kind) == rel_type:
            out.extend((ts, tp) for ts, tp in targets)
    return out


def _element(
    bus: Bus, svc: str, path: str, role_num: int, nm: str, actionable: bool
) -> dict[str, Any] | None:
    """One find_elements record, or None when it is not actionable (when
    asked) or has no usable geometry."""
    states = _states(bus, svc, path)
    editable = role_num in _TEXT_ROLES and _STATE_EDITABLE in states
    if actionable and role_num not in ACTIONABLE_ROLE_NUMS and not editable:
        return None
    ext = _window_extents(bus, svc, path)
    if ext is None:
        return None
    return {
        "role": _role_name(bus, svc, path, role_num),
        "name": nm,
        "extent": ext,
        "clickable": _clickable_now(states),
        **element_value(bus, svc, path, role_num, states),
        "svc": svc,
        "path": path,
        # a text field, told by role number: role NAMES differ per toolkit
        # (GTK4 says "text box"), and pressing a field's action activates it
        "field": (role_num in _TEXT_ROLES or role_num == PASSWORD_ROLE)
        and _STATE_EDITABLE in states,
    }


def focused_role(
    bus: Bus, app_svc: str, app_path: str, max_nodes: int = WALK_BUDGET
) -> int | None:
    """The AtspiRole number of the element that currently holds keyboard
    focus in this window, or None if none is found within the node budget.
    Bounded because it runs before typing: a fast, best-effort check for the
    auth guard, not an exhaustive walk. Prunes into non-showing subtrees so
    it does not spend the budget on hidden pages."""
    walk = _Walk(bus, (app_svc, app_path), max_nodes, fields=("role", "states"))
    for svc, path in walk:
        states = walk.states((svc, path))
        if _STATE_FOCUSED in states:
            return walk.role((svc, path))
        if _STATE_SHOWING not in states and (svc, path) != (app_svc, app_path):
            walk.skip()
    return None


_EDITABLE = "org.a11y.atspi.EditableText"


def current_value(bus: Bus, svc: str, path: str) -> dict[str, Any]:
    """The element's reading as `ui` reports it (checked, value, percent),
    for reading a background action back; {} when it carries none."""
    try:
        return element_value(bus, svc, path, _role_num(bus, svc, path), _states(bus, svc, path))
    except CallRefused:
        return {}


def _has_actions(bus: Bus, svc: str, path: str) -> bool:
    # the interface first: NActions on an object without one is an error
    return "Action" in _interfaces(bus, svc, path) and has_action(bus, svc, path)


def action_name(bus: Bus, svc: str, path: str, index: int = 0) -> str:
    """The name of an element's action, as the toolkit gives it ("click",
    "Press", "SetFocus"), or "" when it cannot be read."""
    try:
        return str(bus.call(svc, path, _ACTION, "GetName", "i", str(index))[0])
    except (CallRefused, IndexError):
        return ""


def is_focused(bus: Bus, svc: str, path: str) -> bool:
    """Whether the element reports the app's own keyboard focus."""
    return _STATE_FOCUSED in _states(bus, svc, path)


def has_action(bus: Bus, svc: str, path: str) -> bool:
    """Whether the element has an action to invoke (a button, a check box),
    which is what a background press needs. NActions is a property in
    AT-SPI, not a method."""
    try:
        return int(bus.prop(svc, path, _ACTION, "NActions")) > 0
    except (CallRefused, IndexError, ValueError):
        return False


def grab_focus(bus: Bus, svc: str, path: str) -> bool:
    """Move the app's OWN keyboard focus to this element. Not the
    compositor's: the human's focus is untouched."""
    journal.refuse_if_dry("grab_focus")
    try:
        return bool(bus.call(svc, path, _COMPONENT, "GrabFocus")[0])
    except (CallRefused, IndexError):
        return False


def text_of(bus: Bus, svc: str, path: str) -> str:
    """A text element's full contents, or "" when it cannot be read."""
    try:
        n = int(bus.prop(svc, path, _TEXT, "CharacterCount"))
        return str(bus.call(svc, path, _TEXT, "GetText", "ii", "0", str(n))[0])
    except (CallRefused, IndexError, ValueError):
        return ""


def toolkit(bus: Bus, svc: str) -> tuple[str, str]:
    """(ToolkitName, Version) of the app on `svc`, ("", "") when unknown.
    GTK 3 says ("gtk", "3.24.52"), GTK 4 ("GTK", "4.22.4"), Qt ("Qt", "6.11.1")."""
    try:
        name = bus.prop(svc, _ROOT, "org.a11y.atspi.Application", "ToolkitName")
        version = bus.prop(svc, _ROOT, "org.a11y.atspi.Application", "Version")
        return str(name), str(version)
    except CallRefused:
        return "", ""


def insert_text(bus: Bus, svc: str, path: str, text: str) -> bool:
    """Insert `text` at the caret, as typing would, with no keyboard."""
    journal.refuse_if_dry("insert_text")
    try:
        caret = int(bus.prop(svc, path, _TEXT, "CaretOffset"))  # a property, not a method
        # The length is read differently per toolkit: GTK and the other ATK
        # apps take UTF-8 bytes (a character count cuts the text short), Qt
        # resizes the text to that many UTF-16 units (a byte count pads it)
        qt = toolkit(bus, svc)[0].lower() == "qt"
        length = len(text.encode("utf-16-le")) // 2 if qt else len(text.encode())
        return bool(
            bus.call(svc, path, _EDITABLE, "InsertText", "isi",
                     str(max(caret, 0)), text, str(length))[0]
        )
    except (CallRefused, IndexError, ValueError):
        return False


def focused_text(
    bus: Bus, app_svc: str, app_path: str, max_nodes: int = WALK_BUDGET
) -> tuple[str, str, int] | None:
    """(svc, path, role) of the element holding the app's own keyboard focus
    in this window when it is a text field (password fields included, so a
    caller can refuse them), else None. Walks every child within the
    budget: a hidden window's subtree may not report SHOWING."""
    walk = _Walk(bus, (app_svc, app_path), max_nodes, fields=("role", "states"))
    for svc, path in walk:
        if _STATE_FOCUSED in walk.states((svc, path)):
            role = walk.role((svc, path))
            return (svc, path, role) if role in _TEXT_ROLES | {PASSWORD_ROLE} else None
    return None


def editable_fields(
    bus: Bus, app_svc: str, app_path: str, max_nodes: int = WALK_BUDGET
) -> tuple[list[tuple[str, str, int]], bool]:
    """((svc, path, role) of every editable text field in this window, in
    document order; whether the walk read the whole window). GTK reports no
    keyboard focus inside a window that is not active, so a background
    caller uses this when focused_text finds nothing: a window with exactly
    one field has one place for text, which a walk cut short cannot show."""
    out: list[tuple[str, str, int]] = []
    walk = _Walk(bus, (app_svc, app_path), max_nodes, fields=("role", "states"))
    for svc, path in walk:
        role = walk.role((svc, path))
        if role in _TEXT_ROLES | {PASSWORD_ROLE} and _STATE_EDITABLE in walk.states((svc, path)):
            out.append((svc, path, role))
    return out, not walk.truncated


def do_action(bus: Bus, svc: str, path: str, index: int = 0) -> bool:
    """Invoke an accessible's action (default index 0, usually the click),
    with no pointer, so it works even when the window is not visible."""
    journal.refuse_if_dry("do_action")
    try:
        return bool(bus.call(svc, path, _ACTION, "DoAction", "i", str(index))[0])
    except (CallRefused, IndexError):
        return False
