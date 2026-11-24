"""Trust layers for a shared human/agent seat.

Opt-in constraints, each gated by an env var, that limit what an agent can
touch and make its presence legible. They compose with the always-on
approval and beacon layers; none persists anything, and every one fails
toward LESS action, never more.

  HYPRUSE_CONFINE     restrict input to a scope of windows:
                      `launched` (only windows hypruse itself opened),
                      `class:foo,bar`, or `workspace:3,special:notes`
  HYPRUSE_AUTH_GUARD  refuse to drive polkit/authentication dialogs
                      (default on; per-call allow_auth overrides). Set it
                      to `strict` to ALSO refuse typing into a password
                      field inside an ordinary window, which costs an
                      a11y tree walk and is therefore opt-in
  HYPRUSE_STRICT      refuse to act when the seat moved without hypruse
                      since its last action (human or app took over)
  HYPRUSE_MARK        tag every agent-owned window `hypruse-owned` and flash
                      an on-screen notice when the agent opens a window or
                      captures the screen; also best-effort installs a
                      border_color windowrule on that tag (add it to your
                      config for a guaranteed outline, since a runtime rule
                      does not render on every Hyprland version/config)

The server calls the guard_* functions inside each acting tool; a raised
TrustError becomes the tool's error, which the MCP client surfaces.

Three checks are ALWAYS on, no env var, because they are truthfulness
aids rather than opt-in policy: covering_layer/guard_covering_layer (a
click aimed under a launcher or on-screen-keyboard layer surface would
land on the layer, not the window), guard_keyboard_layer (a launcher
holds the keyboard grab, so synthetic keys go to it no matter which
window was focused), and guard_session_lock (the session is locked, so
all input reaches a credential prompt). All are positive-detection, and
the lock check is best-effort. While HYPRUSE_AUTH_GUARD is on (the
default), though, pointer, click_ui and keyboard refuse when the layer
list cannot be read, since an authentication prompt drawn as a layer
cannot be ruled out.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any

from hypruse import a11y, hyprctl


class TrustError(RuntimeError):
    """An action was refused by a trust layer (confinement, auth, seat)."""


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


# --- confinement ------------------------------------------------------------

_owned: set[str] = set()  # window addresses hypruse launched this session

_OWNED_TAG = "hypruse-owned"


def note_launched(address: str, label: str = "") -> None:
    """Record a window hypruse opened (for `launched` confinement), and when
    HYPRUSE_MARK is on, make it legible: tag it `hypruse-owned` (which the
    border rule from init_marking colors) and flash an on-screen notice."""
    if not address:
        return
    _owned.add(address)
    if _flag("HYPRUSE_MARK"):
        with contextlib.suppress(hyprctl.HyprctlError):
            hyprctl.dispatch("tagwindow", f"+{_OWNED_TAG}", f"address:{address}")
        hyprctl.notify(f"hypruse opened {label}".rstrip(), ms=2500, color="rgb(ff5555)")


def owned() -> set[str]:
    return set(_owned)


# --- cross-process state ------------------------------------------------------
#
# The owned-set, the strict seat baseline and the capture-notice limiter live
# in this process. The MCP server is one long process, so that is all it
# needs; a CLI verb is a fresh process per call, and without these two
# functions `launched` confinement would refuse everything after the launch
# and HYPRUSE_STRICT would silently never fire (guard_seat is a no-op until
# something remembered a seat). cli_state carries them across calls.


def export_state() -> dict[str, Any]:
    return {
        "owned": sorted(_owned),
        "seat": {"cursor": _seat["cursor"], "active": _seat["active"]},
        "notify_ts": _last_notify["ts"],
    }


def restore_state(state: Any) -> None:
    """Replace the remembered state with what export_state wrote, or with
    nothing when the record is missing or malformed: a hand-edited or
    truncated file degrades to 'nothing remembered', never to a crash and
    never to a baseline left over from some other compositor. The cursor
    comes back as a tuple, because guard_seat compares it to
    hyprctl.cursor_pos() with != and a list would never be equal."""
    if not isinstance(state, dict):
        state = {}
    owned = state.get("owned")
    _owned.clear()
    if isinstance(owned, list):
        _owned.update(a for a in owned if isinstance(a, str))
    seat = state.get("seat")
    seat = seat if isinstance(seat, dict) else {}
    cursor = seat.get("cursor")
    is_pair = isinstance(cursor, list | tuple) and len(cursor) == 2
    _seat["cursor"] = tuple(cursor) if is_pair else None
    active = seat.get("active")
    _seat["active"] = active if isinstance(active, str) else None
    ts = state.get("notify_ts")
    _last_notify["ts"] = float(ts) if isinstance(ts, int | float) else 0.0


def _confine_scope() -> tuple[str, Any] | None:
    """The confinement scope, or None when confinement is off. Under the
    background guarantee (HYPRUSE_BACKGROUND=1) it is ("agent", explicit):
    a window must be on the agent's workspaces AND inside HYPRUSE_CONFINE
    when that is set too, so an explicit scope narrows the guarantee and
    never widens it."""
    from hypruse import background  # background imports this module

    explicit = _explicit_scope()
    if background.mode() == background.GUARANTEE:
        return ("agent", explicit)
    return explicit


def _explicit_scope() -> tuple[str, tuple[str, ...]] | None:
    """Parse HYPRUSE_CONFINE, or None when it is unset. Raises TrustError
    (which denies the action) on a malformed value, so a typo fails closed
    instead of silently disabling the sandbox."""
    raw = os.environ.get("HYPRUSE_CONFINE", "").strip()
    if not raw:
        return None
    if raw == "launched":
        return ("launched", ())
    kind, sep, rest = raw.partition(":")
    if sep and kind in ("class", "workspace"):
        vals = tuple(v.strip() for v in rest.split(",") if v.strip())
        if vals:
            return (kind, vals)
    raise TrustError(
        f"HYPRUSE_CONFINE={raw!r} is malformed (use launched | class:a,b | "
        "workspace:1,2); refusing every action until it is fixed"
    )


def _client_in_scope(client: dict[str, Any], scope: tuple[str, Any]) -> bool:
    kind, vals = scope
    if kind == "agent":
        from hypruse import background

        return background.on_agent_workspace(client) and (
            vals is None or _client_in_scope(client, vals)
        )
    if kind == "launched":
        return client.get("address") in _owned
    if kind == "class":
        return client.get("class") in vals
    ws = client.get("workspace", {}) or {}
    return str(ws.get("id")) in vals or str(ws.get("name", "")) in vals


def _describe(scope: tuple[str, Any]) -> str:
    kind, vals = scope
    if kind == "agent":
        return "the agent's workspaces" + (f" and {_describe(vals)}" if vals else "")
    return "windows hypruse launched" if kind == "launched" else f"{kind} in {list(vals)}"


def guards_window_input() -> bool:
    """Whether any active guard needs the target window resolved before a
    keyboard action (so the tool only pays for the hyprctl query when a
    guard is on). True by default because the auth guard defaults on."""
    return _confine_scope() is not None or _auth_guard_on()


def guard_client(client: dict[str, Any]) -> None:
    """Refuse if `client` is outside the confinement scope. Takes an already
    resolved client so a caller that holds one does not re-query hyprctl."""
    scope = _confine_scope()
    if scope is None:
        return
    if not _client_in_scope(client, scope):
        raise TrustError(
            f"{client.get('address', '?')} ({client.get('class', '?')}) is outside "
            f"the agent's confinement scope ({_describe(scope)})"
        )


def guard_use_bind() -> None:
    """Refuse use_bind while confinement is active: it dispatches the bind's
    action verbatim (exec, focuswindow, workspace switches, killactive, ...),
    an arbitrary compositor action that cannot be scoped to a window, so it
    is an escape hatch out of any confinement. Denied wholesale rather than
    guessed at."""
    scope = _confine_scope()
    if scope is not None and scope[0] == "agent":
        from hypruse import background

        background.refuse(
            "use_bind runs whatever the bind does, which can switch the human's "
            "workspace or take their focus"
        )
    if scope is not None:
        raise TrustError(
            f"use_bind runs an arbitrary compositor action and cannot be confined "
            f"({_describe(scope)}); it is refused while HYPRUSE_CONFINE is set"
        )


def _windows_under(x: float, y: float) -> list[dict[str, Any]]:
    """Every mapped, on-screen window whose rect covers (x, y). One batched
    hyprctl call (monitors + clients) so a pointer guard costs one fork."""
    return _seat_at(x, y)[1]


def _seat_at(x: float, y: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The monitors, and the mapped on-screen windows whose rect covers
    (x, y), from one batched hyprctl call."""
    monitors, clients = hyprctl.batch_query(["monitors", "clients"])
    visible = hyprctl.visible_workspaces(monitors)
    return monitors, [
        c
        for c in clients
        if c.get("mapped", True)
        and hyprctl.workspace_ref(c.get("workspace")) in visible
        and _covers(c, x, y)
    ]


def guard_pointer(x: float | None, y: float | None, allow_auth: bool = False) -> None:
    """Guard a pointer action at (x, y): confinement AND the auth interlock,
    resolved from the windows under the point in one query. When x/y are
    omitted the action lands at the CURRENT cursor, so the current position
    is resolved and checked (a click-in-place must not skip the guards).

    Fails closed: if the point (or the windows under it) cannot be resolved
    while a guard is active, the action is refused, never allowed through
    unchecked. Confinement in particular cannot prove the top window is safe
    from a non-z-ordered client list, so any covering out-of-scope window
    refuses. Auth refuses a click over a known authentication dialog unless
    allow_auth."""
    scope = _confine_scope()
    auth = _auth_guard_on() and not allow_auth
    if scope is None and not auth:
        return  # nothing active: no query
    px, py = x, y
    if px is None or py is None:
        # a coordinate-less click/scroll lands at the current cursor; if we
        # cannot read where that is, refuse rather than fire unchecked (the
        # virtual-pointer wire delivers the event even when hyprctl is down)
        try:
            px, py = hyprctl.cursor_pos()
        except Exception as exc:
            raise TrustError(
                "cannot resolve the cursor position to guard a coordinate-less "
                f"pointer action ({exc}); pass explicit x,y or retry"
            ) from exc
    monitors, under = _seat_at(px, py)
    if hyprctl.monitor_at(monitors, px, py) is None:
        # Hyprland moves the cursor to the nearest point inside the monitor
        # layout, where a window this guard never checked takes the input
        raise TrustError(
            f"({px:.0f}, {py:.0f}) is off every monitor: Hyprland would move the pointer "
            "to the nearest screen edge, onto a window the guards never checked; "
            "refusing. Aim at a point on a monitor."
        )
    if scope is not None:
        outside = [c for c in under if not _client_in_scope(c, scope)]
        if outside:
            classes = ", ".join(sorted({c.get("class", "?") for c in outside}))
            raise TrustError(
                f"({px:.0f}, {py:.0f}) is over a window outside the confinement scope "
                f"({classes}; {_describe(scope)}); clicking there is refused"
            )
    if auth:
        hit = next((c for c in under if (c.get("class") or "").lower() in _AUTH_CLASSES), None)
        if hit is not None:
            raise TrustError(
                f"({px:.0f}, {py:.0f}) is over {hit.get('class')}, {_auth_what(hit)}; "
                f"refusing the click. {_auth_consent(hit)}"
            )
        # a shell-integrated polkit agent draws its prompt as a layer, which
        # `clients` never lists (Omarchy 4). ANY auth surface over the point
        # counts, not just the topmost: an on-screen keyboard above one feeds
        # its password field
        surfaces = _read_layers()
        if surfaces is None:
            raise TrustError(
                f"cannot read the layer list to rule out an authentication prompt at "
                f"({px:.0f}, {py:.0f}); refusing the click. Retry, or pass allow_auth=true "
                "only if a human intends a credential action."
            )
        layer = next(
            (s for s in surfaces if s.get("kind") == "auth" and _surface_covers(s, px, py)),
            None,
        )
        if layer is not None:
            raise TrustError(
                f"({px:.0f}, {py:.0f}) is covered by {layer['namespace']!r}, a system "
                "authentication prompt; refusing the click. Pass allow_auth=true only "
                "if a human intends this credential action."
            )


def _click_scene() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Monitors and every client, from one batched hyprctl call."""
    monitors, clients = hyprctl.batch_query(["monitors", "clients"])
    return monitors, clients


def _special_fallthrough() -> int | None:
    """Hyprland's input:special_fallthrough, or None when it cannot be read.
    hyprland.conf reports it as "int", the Lua config manager as "bool"."""
    try:
        reply = hyprctl.query("getoption input:special_fallthrough")
    except Exception:
        return None
    value = reply.get("int", reply.get("bool")) if isinstance(reply, dict) else None
    return int(value) if isinstance(value, int | bool) else None


def check_click_point(
    client: dict[str, Any],
    x: float,
    y: float,
    allow_auth: bool = False,
    focused: bool = False,
) -> None:
    """click_ui's checks on the point it is about to click.

    Always: the point must be on a monitor (Hyprland moves an off-monitor
    point to the nearest edge, onto something else) and inside the target
    window (a mark's offset can fall outside a window resized since
    marks()); on another monitor than the target's, the target must float
    (a tiled window takes clicks only on its own monitor), and a scratchpad
    up there must not take the click first.

    While a guard is on: what covers the point once the target is
    focused. Hyprland's hit test on the monitor under the point takes its
    own priority dialog (the permission prompt) first, then pinned
    windows, then that monitor's scratchpad, then floating windows (from
    any monitor), then that monitor's tiled ones, and focusing a window
    does not raise it, so these count: the permission prompt over any
    target; for a pinned target, only other pinned windows besides; for
    any other target, any pinned window; a floating or fullscreen window on the
    target's workspace, whether or not the target floats; a floating
    window from another monitor (it can hang across the edge); the point
    monitor's scratchpad windows; and a scratchpad on the target's
    monitor, unless focusing will hide it
    (input:special_fallthrough 0, the default, and a target on a regular
    workspace; unreadable counts as not hidden). After focusing (`focused`)
    a scratchpad still up counts. These do not: the target and its group
    tabs, an inactive tab, a tiled window on its workspace (tiled windows
    do not overlap, and the point is inside the target), and windows on
    another regular workspace of its monitor, which focusing replaces.
    Refuses when one of them is an authentication dialog, or out of scope
    under confinement; fails closed while a guard is on and the desktop
    cannot be read."""
    scope = _confine_scope()
    auth = _auth_guard_on() and not allow_auth
    try:
        monitors, clients = _click_scene()
    except Exception as exc:
        if scope is not None or auth:
            raise TrustError(
                f"cannot read the desktop to guard the click ({exc}); refusing. Retry."
            ) from exc
        monitors, clients = None, []
    if monitors is not None and hyprctl.monitor_at(monitors, x, y) is None:
        raise TrustError(
            f"({x:.0f}, {y:.0f}) is off every monitor: Hyprland would move the click to the "
            "nearest screen edge, onto something else; refusing. Bring the window fully on "
            "screen first."
        )
    if not _covers(client, x, y):
        raise TrustError(
            f"({x:.0f}, {y:.0f}) is outside the window ({client.get('at')} "
            f"{client.get('size')}), so the click would land on something else; refusing. "
            "A mark taken before the window changed needs marks() again."
        )
    # A control hanging over onto another monitor. Hyprland's hit test on
    # the monitor under the point runs pinned windows, then that monitor's
    # scratchpad, then floating windows (no monitor test), then only that
    # monitor's own tiled windows. So a tiled target never gets the click
    # there, and with a scratchpad up there at special_fallthrough 0 only a
    # pinned target or one on an open scratchpad of its own gets past it (at
    # 1 the scratchpad's windows come first, counted below). Focusing the
    # target hides only its own monitor's scratchpad.
    m = hyprctl.monitor_at(monitors, x, y) if monitors is not None else None
    if m is not None and m.get("id") != client.get("monitor"):
        if not client.get("floating") and not client.get("pinned"):
            raise TrustError(
                f"({x:.0f}, {y:.0f}) is on {m.get('name')}, not the window's own monitor: "
                "Hyprland gives a tiled window clicks only on its own monitor, so the click "
                "would land on something else; refusing."
            )
        own_ws = client.get("workspace") or {}
        on_open_special = _is_special(own_ws) and hyprctl.workspace_ref(
            own_ws
        ) in hyprctl.visible_workspaces(monitors)
        if (
            not client.get("pinned")
            and not on_open_special
            and hyprctl.workspace_ref(m.get("specialWorkspace")) is not None
            and _special_fallthrough() != 1
        ):
            raise TrustError(
                f"({x:.0f}, {y:.0f}) is on {m.get('name')}, which has a scratchpad up: the "
                "click would go to the scratchpad, not to the window; refusing. Hide the "
                "scratchpad first."
            )
    if scope is None and not auth:
        return
    point_monitor = m.get("id") if m is not None else None
    over = [
        c
        for c in clients
        if c.get("mapped", True)
        and _covers(c, x, y)
        and _would_cover(c, client, monitors or [], focused, point_monitor)
    ]
    if auth:
        hit = next((c for c in over if (c.get("class") or "").lower() in _AUTH_CLASSES), None)
        if hit is not None:
            raise TrustError(
                f"({x:.0f}, {y:.0f}) is covered by {hit.get('class')}, {_auth_what(hit)}, "
                f"which would take the click; refusing. {_auth_consent(hit)}"
            )
    if scope is not None:
        outside = [c for c in over if not _client_in_scope(c, scope)]
        if outside:
            classes = ", ".join(sorted({c.get("class", "?") for c in outside}))
            raise TrustError(
                f"({x:.0f}, {y:.0f}) is covered by a window outside the confinement scope "
                f"({classes}; {_describe(scope)}), which would take the click; refusing"
            )


def other_window_at(x: float, y: float, active: str | None) -> dict[str, Any] | None:
    """The window other than `active` that pointer input at (x, y) lands on,
    or None when it lands on `active` or on no window (a bar, a launcher,
    the wallpaper). Background mode's automatic rule, which keeps the
    pointer on the window the human is in: a window over `active` by
    check_click_point's hit-test order counts, and z-order among floating
    windows is unknown, so an overlap counts as covering. An unreadable
    desktop gives None, because this is a courtesy, not a guard: the
    guarantee refuses the pointer before asking."""
    try:
        monitors, clients = _click_scene()
    except Exception:
        return None
    if _topmost_covering(_read_layers() or [], x, y) is not None:
        return None  # a launcher or on-screen keyboard over the point takes the input
    visible = hyprctl.visible_workspaces(monitors)
    under = [
        c
        for c in clients
        if c.get("mapped", True)
        and c.get("visible") is not False
        and hyprctl.workspace_ref(c.get("workspace")) in visible
        and _covers(c, x, y)
    ]
    me = next((c for c in under if c.get("address") == active), None) if active else None
    if me is None:
        return under[0] if under else None
    m = hyprctl.monitor_at(monitors, x, y)
    point_monitor = m.get("id") if m is not None else None
    return next(
        (c for c in under if _would_cover(c, me, monitors, True, point_monitor)), None
    )


def _would_cover(
    c: dict[str, Any],
    target: dict[str, Any],
    monitors: list[dict[str, Any]],
    focused: bool,
    point_monitor: Any = None,
) -> bool:
    """Whether `c`, whose rect holds the point, would take a click aimed
    at `target` once the target is focused (see check_click_point)."""
    if c.get("address") == target.get("address") or c.get("address") in (
        target.get("grouped") or []
    ):
        return False
    if c.get("visible") is False:  # an inactive tab: not drawn
        return False
    ws = c.get("workspace") or {}
    if (
        (c.get("class") or "").lower() == _PRIORITY_DIALOG
        and hyprctl.workspace_ref(ws) in hyprctl.visible_workspaces(monitors)
    ):
        # priority focus, keyed on the dialog's process, floating or tiled:
        # it takes the pointer before even pinned windows. The app-not-
        # responding notice shares the class and has no priority, but
        # nothing tells them apart, so it counts too (fail closed)
        return True
    if target.get("pinned"):
        # the pinned pass runs first: only another pinned window can be above
        return bool(c.get("pinned"))
    if c.get("pinned"):
        return True
    ref = hyprctl.workspace_ref(ws)
    if ref == hyprctl.workspace_ref(target.get("workspace")):
        return bool(c.get("floating") or c.get("fullscreen"))
    if ref not in hyprctl.visible_workspaces(monitors):
        return False
    if c.get("monitor") != target.get("monitor"):
        # a floating window can hang across the monitor edge; a scratchpad
        # takes clicks first only on its own monitor, when that is the
        # point's
        return bool(c.get("floating")) or (
            _is_special(ws) and c.get("monitor") == point_monitor
        )
    if not _is_special(ws):
        return False  # focusing the target replaces this workspace
    if focused:
        return True  # a scratchpad still up over the target
    hidden_by_focus = (
        not _is_special(target.get("workspace") or {})
        and not target.get("pinned")
        and _special_fallthrough() == 0
    )
    return not hidden_by_focus


def _is_special(ws: dict[str, Any]) -> bool:
    return ws.get("type") == "special" or str(ws.get("name", "")).startswith("special:")


def _covers(client: dict[str, Any], x: float, y: float) -> bool:
    at, size = client.get("at"), client.get("size")
    if not at or not size:
        return False
    return at[0] <= x < at[0] + size[0] and at[1] <= y < at[1] + size[1]


def covering_layer(x: float, y: float) -> dict[str, Any] | None:
    """The TOPMOST focus-stealing layer surface (launcher, on-screen
    keyboard, or a legacy layer-shell lock screen) whose rect covers
    (x, y), or None. Layer surfaces sit above windows, so input aimed at
    a window under one lands on the layer instead, and _windows_under
    cannot see that (`clients` never lists layer surfaces). Where several
    overlap, the one that actually receives the input is the highest
    level, and within a level the most recently mapped, which is the
    order `hyprctl layers` reports; naming any other surface in a refusal
    would misinform. Positive-detection only (known kinds), and
    best-effort by design: this is a truthfulness aid, not a confinement
    boundary, so an unreadable layer list yields None rather than
    blocking every click."""
    surfaces = _read_layers()
    return None if surfaces is None else _topmost_covering(surfaces, x, y)


def _read_layers() -> list[dict[str, Any]] | None:
    """The parsed layer list, or None when it cannot be read."""
    try:
        return hyprctl.parse_layers(hyprctl.query("layers"))
    except Exception:
        return None


def _surface_covers(s: dict[str, Any], x: float, y: float) -> bool:
    g = s.get("geometry") or []
    return (
        len(g) == 4
        and None not in g
        and g[0] <= x < g[0] + g[2]
        and g[1] <= y < g[1] + g[3]
    )


def _topmost_covering(
    surfaces: list[dict[str, Any]], x: float, y: float
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_rank = -1
    for s in surfaces:
        if s.get("kind") not in hyprctl.FOCUS_STEALING_KINDS or not _surface_covers(s, x, y):
            continue
        rank = _level_rank(s.get("level", ""))
        if rank >= best_rank:  # >= so a later surface on the same level wins
            best, best_rank = s, rank
    return best


def _level_rank(level: str) -> int:
    """Stacking order of a parsed layer level name; unknown levels sort
    lowest so a named level always outranks one we cannot place."""
    try:
        return hyprctl.LAYER_LEVELS.index(level)
    except ValueError:
        return -1


# Session lockers. These are ext-session-lock-v1 clients, NOT layer-shell
# clients, so they never appear in `hyprctl layers` and no layer-based
# check can see them. The authority is the compositor: `hyprctl locked`
# (Hyprland 0.41+) reports the lock itself, whoever holds it. That covers
# lockers no process scan can find (Omarchy 4 locks from inside its
# Quickshell shell) and a crashed locker, which ext-session-lock-v1 says
# must leave the session LOCKED, not hand it back. The process scan below
# names a running locker for the refusal message, and is the whole check
# only when the compositor cannot be asked. swaylock before 1.7 drew its
# lock with layer-shell under the "lockscreen" namespace, which `layer_kind`
# still classifies; a layer-shell locker with no recognisable namespace
# (gtklock 2.x) that the compositor does not report as a lock goes unseen.
#
# Matched against /proc/PID/comm, which the kernel caps at TASK_COMM_LEN-1
# = 15 characters, so every entry here MUST be <= 15 chars or it can never
# match; _COMM_MAX below enforces that at import time. A locker whose
# binary name is longer would need its truncated form listed instead.
# swaylock-effects is packaged as a drop-in that ships /usr/bin/swaylock
# (Provides/Conflicts swaylock), so its comm is "swaylock", already
# covered; it is deliberately NOT listed under a fabricated
# "swaylock-effect" name that no real packaging produces.
_LOCKER_COMMS = frozenset(
    {
        "hyprlock",
        "swaylock",
        "gtklock",
        "waylock",
    }
)

_COMM_MAX = 15  # Linux TASK_COMM_LEN (16) minus the trailing NUL: the kernel
# truncates /proc/PID/comm to this, so a longer entry can never match and
# must be listed in its truncated form instead. Enforced at import.
assert all(len(c) <= _COMM_MAX for c in _LOCKER_COMMS), (
    "a _LOCKER_COMMS entry longer than 15 chars can never match /proc comm"
)


_PROC = "/proc"  # overridden in tests so the scan never reads live state


def _compositor_locked() -> bool | None:
    """Hyprland's own answer to "is the session locked" (`hyprctl -j
    locked`, 0.41+), or None when the compositor cannot be asked (older
    Hyprland, IPC down) or answers in a shape this does not know."""
    try:
        state = hyprctl.query("locked")
    except Exception:
        return None
    locked = state.get("locked") if isinstance(state, dict) else None
    return locked if isinstance(locked, bool) else None


def session_locked() -> str | None:
    """What holds the session locked, or None: the running locker's name
    when one can be found, else "lock screen". The compositor decides
    whether the session is locked (see the note above _LOCKER_COMMS);
    only when it cannot be asked does a running locker process alone
    count, the best signal left."""
    state = _compositor_locked()
    if state is False:
        return None
    locker = _locker_process()
    if state is True:
        return locker or "lock screen"
    return locker


def _locker_process() -> str | None:
    """The name of a running session locker, or None. Reads /proc directly
    rather than shelling out, so it adds no binary dependency (~8 ms, the
    same order as one hyprctl call). Best-effort like the layer checks: an
    unreadable /proc yields None rather than blocking every action."""
    try:
        entries = list(os.scandir(_PROC))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            # bytes, decoded leniently: the kernel cuts comm at 15 bytes,
            # which can split a multibyte character
            with open(f"{_PROC}/{entry.name}/comm", "rb") as fh:
                comm = fh.read().decode("utf-8", "replace").strip()
        except OSError:
            continue  # the process exited between listing and reading
        if comm in _LOCKER_COMMS:
            return comm
    return None


def guard_session_lock(window_given: bool, allow_auth: bool) -> str:
    """Refuse input while the session is locked. Every event, pointer or
    keyboard, reaches the lock surface, which is a credential prompt, so
    it cannot reach any window. The refusal order mirrors
    guard_keyboard_layer, because a lock is the same shape of problem:

    1. `window=` names a recipient the input provably cannot reach, so
       the contract is broken; allow_auth does not apply, since intending
       to drive the credential prompt contradicts naming a target window.
       (This is the case that would otherwise leak a browser secret into
       the lock prompt when the screen locks mid-flow.)
    2. Under confinement the credential prompt is not a confinable
       window, so it is refused wholesale, the same escape guard_use_bind
       and guard_keyboard_layer refuse.
    3. Otherwise the input reaches the prompt: allow_auth means a human
       is deliberately driving the unlock, and earns the returned note;
       without it, feeding a credential prompt unasked is refused."""
    locker = session_locked()
    if locker is None:
        return ""
    if window_given:
        raise TrustError(
            f"the session is locked ({locker}), so input cannot reach the "
            "requested window; it would go to the lock's credential prompt. "
            "Unlock the session first."
        )
    scope = _confine_scope()
    if scope is not None:
        raise TrustError(
            f"the session is locked ({locker}); its credential prompt is not a "
            f"confinable window ({_describe(scope)}), so input is refused while "
            "HYPRUSE_CONFINE is set. Unlock the session first."
        )
    if not allow_auth:
        raise TrustError(
            f"the session is locked ({locker}): all input goes to its "
            "credential prompt, not to any window. Unlock the session "
            "first. Pass allow_auth=true only if a human intends the agent "
            "to drive that prompt."
        )
    return (
        f"; NOTE: the session is locked ({locker}), so the input went to "
        "its credential prompt, not to any window"
    )


def guard_keyboard_layer(window_given: bool, allow_auth: bool) -> str:
    """Guard typing against a mapped keyboard-grabbing layer. Synthetic
    keys go to the seat's KEYBOARD focus, and a launcher or lock screen
    holds an exclusive grab no matter which window was focused over IPC,
    so the keys reach the LAYER, never the window the other guards
    inspect. Refusals, in order:

    1. `window=` names a recipient the keys provably cannot reach, so the
       tool's own contract is broken; allow_auth does not apply, since
       'a human intends credential entry here' contradicts naming some
       other target window.
    2. Under confinement no scope can contain a layer surface: it is not
       a window, and a launcher runs whatever is typed into it. Refused
       wholesale for the same reason as guard_use_bind.
    3. A lock screen's focused control is a credential prompt, so typing
       into it needs the explicit allow_auth intent.

    Otherwise the keys legitimately drive the layer (typing into a
    launcher after use_bind is the documented flow) and the returned
    note, appended to the tool result, records where they went. The
    on-screen keyboard kind feeds keys rather than eating them, so it
    does not count. Positive detection: an unreadable layer list yields
    '', except that while the auth guard is on it refuses, since an
    authentication prompt holding the keyboard cannot be ruled out."""
    surfaces = _read_layers()
    if surfaces is None:
        if _auth_guard_on() and not allow_auth:
            raise TrustError(
                "cannot read the layer list to rule out an authentication prompt "
                "holding the keyboard; refusing to type. Retry, or pass "
                "allow_auth=true only if a human intends a credential entry."
            )
        return ""
    grabbers = [s for s in surfaces if s.get("kind") in hyprctl.KEYBOARD_GRABBING_KINDS]
    if not grabbers:
        return ""
    # a lock screen outranks an authentication prompt, which outranks a
    # launcher, when several are somehow mapped
    grabber = next(
        (s for kind in ("lock", "auth") for s in grabbers if s["kind"] == kind), grabbers[0]
    )
    kind, ns = grabber["kind"], grabber["namespace"]
    if window_given:
        raise TrustError(
            f"the {kind} layer {ns!r} holds the keyboard grab, so keys cannot "
            f"reach the requested window. {_dismiss_advice(kind)}"
        )
    scope = _confine_scope()
    if scope is not None:
        raise TrustError(
            f"the {kind} layer {ns!r} holds the keyboard grab, and a layer "
            f"surface is not a window, so it cannot be confined "
            f"({_describe(scope)}); typing is refused while HYPRUSE_CONFINE "
            f"is set. {_dismiss_advice(kind)}"
        )
    if kind == "lock" and not allow_auth:
        raise TrustError(
            f"the lock screen layer {ns!r} holds the keyboard: typing now "
            "would feed a credential prompt. Pass allow_auth=true only if a "
            "human intends that credential entry."
        )
    if kind == "auth" and not allow_auth and _auth_guard_on():
        raise TrustError(
            f"the authentication prompt {ns!r} holds the keyboard: typing now "
            f"would feed a credential prompt. {_dismiss_advice(kind)} Pass "
            "allow_auth=true only if a human intends that credential entry."
        )
    return (
        f"; NOTE: the {kind} layer {ns!r} holds the keyboard grab, so the "
        "keys went to it, not to the focused window"
    )


def _dismiss_advice(kind: str) -> str:
    """How to get a layer of this kind out of the way. A lock screen is
    the one surface designed NOT to yield to a keystroke, so telling the
    agent to press esc there would be false, and telling it to drive the
    layer instead would route it into the credential refusal below."""
    if kind == "lock":
        return "Unlock the session first."
    if kind == "auth":
        return "A human has to answer or cancel the authentication prompt first."
    if kind == "lock-preview":
        return "Click anywhere to dismiss it first."
    if kind == "osk":
        return "Dismiss the on-screen keyboard first."
    return (
        "Drive the launcher itself (call keyboard without window=), or "
        "close it first (usually esc)."
    )


def guard_covering_layer(x: float, y: float, allow_auth: bool = False) -> None:
    """Refuse a window-targeted click while a focus-stealing layer covers
    the point: the layer would receive the click, the window's control
    never would, and reporting 'clicked' would be a lie. For click_ui,
    whose target is by definition a window control; a bare pointer click
    may legitimately aim at the layer itself, so it gets a warning in its
    result instead of a refusal. An unreadable layer list refuses while
    the auth guard is on, since an authentication prompt over the control
    cannot be ruled out."""
    surfaces = _read_layers()
    if surfaces is None:
        if _auth_guard_on() and not allow_auth:
            raise TrustError(
                "cannot read the layer list to rule out an authentication prompt "
                f"over ({x:.0f}, {y:.0f}); refusing the click. Retry, or pass "
                "allow_auth=true only if a human intends a credential action."
            )
        return
    s = _topmost_covering(surfaces, x, y)
    if s is not None:
        kind = s["kind"]
        # a credential surface is not something to click deliberately
        tail = "" if kind in ("auth", "lock") else (
            " To click the layer deliberately, use `pointer`."
        )
        raise TrustError(
            f"({x:.0f}, {y:.0f}) is covered by the {kind} layer surface "
            f"{s['namespace']!r}: the click would land on that layer, not on "
            f"the window's control. {_dismiss_advice(kind)}{tail}"
        )


# --- authentication interlock -----------------------------------------------

# Desktop authentication agents: their windows own a separate trust domain
# (they gate sudo, PolicyKit, credential prompts). The agent must not click
# or type into them without explicit human consent.
# Hyprland's own permission prompt (screencopy, plugins, keyboard grabs:
# Deny / Allow once / Allow and remember) is a floating hyprland-dialog
# window with priority focus, so it takes the pointer before even pinned
# windows, and a click there can grant a permission.
_PRIORITY_DIALOG = "hyprland-dialog"

_AUTH_CLASSES = frozenset(
    {
        _PRIORITY_DIALOG,
        "hyprpolkitagent",
        "polkit-gnome-authentication-agent-1",
        "org.kde.polkit-kde-authentication-agent-1",
        "polkit-mate-authentication-agent-1",
        "lxpolkit",
        "lxqt-policykit-agent",
        "xfce-polkit",
        "gcr-prompter",
        "org.gnome.keyring.prompt",
    }
)


def _auth_guard_on() -> bool:
    # default on: the class check below is cheap (uses a client we already
    # hold) and blocks the highest-value case, polkit/credential dialogs
    return os.environ.get("HYPRUSE_AUTH_GUARD", "1").lower() not in ("", "0", "false", "no", "off")


def _auth_strict() -> bool:
    # the password-field check walks the a11y tree, a call or two per
    # element, too costly for every keystroke, so it is opt-in
    return os.environ.get("HYPRUSE_AUTH_GUARD", "1").lower() in ("strict", "2", "field", "fields")


def guard_auth_client(client: dict[str, Any], allow_auth: bool) -> None:
    """Refuse to act on a system authentication dialog. Positive-detection
    only (a known agent class), so it never blocks ordinary windows even
    when the class is unreadable. Takes an already resolved client."""
    if allow_auth or not _auth_guard_on():
        return
    cls = (client.get("class") or "").lower()
    if cls in _AUTH_CLASSES:
        raise TrustError(
            f"{cls} is {_auth_what(client)}; refusing to drive it. {_auth_consent(client)}"
        )


def _auth_what(c: dict[str, Any]) -> str:
    """What an _AUTH_CLASSES window is, for a refusal. hyprland-dialog is
    Hyprland's permission prompt, but also its app-not-responding and
    safe-mode notices: same binary, same class, an empty title, so nothing
    tells them apart, and all of them are refused."""
    if (c.get("class") or "").lower() == _PRIORITY_DIALOG:
        return (
            "Hyprland's own dialog (a permission prompt, or an app-not-responding or "
            "safe-mode notice)"
        )
    return "a system authentication dialog"


def _auth_consent(c: dict[str, Any]) -> str:
    if (c.get("class") or "").lower() == _PRIORITY_DIALOG:
        return (
            "A permission prompt grants or denies a permission; pass allow_auth=true only "
            "if a human intends this."
        )
    return "Pass allow_auth=true only if a human intends this credential action."


def guard_password_field(client: dict[str, Any], allow_auth: bool) -> None:
    """Refuse to TYPE when the focused control is a password field. The
    class check above is the strong guarantee; this adds cover for a
    password box inside an ordinary window (a browser login). Opt-in
    (HYPRUSE_AUTH_GUARD=strict) because it walks the a11y tree. Best-effort
    and fail-open: an unreadable tree does not block typing, but a positive
    password-field detection does."""
    if allow_auth or not _auth_strict():
        return
    try:
        bus = a11y.connect()
        app = a11y.app_for_pid(
            bus, client.get("pid"), client.get("title", ""), tuple(client.get("size") or ()) or None
        )
        role = a11y.focused_role(bus, app[0], app[1]) if app else None
    except Exception:
        return  # unreadable tree: the class check is the real guarantee
    if role == a11y.PASSWORD_ROLE:
        raise TrustError(
            "the focused field is a password entry; refusing to type into it. "
            "Pass allow_auth=true only if a human intends this."
        )


# --- seat-contention guard --------------------------------------------------

_seat: dict[str, Any] = {"cursor": None, "active": None}


def _strict_on() -> bool:
    return _flag("HYPRUSE_STRICT")


def remember_seat() -> None:
    """Stash the cursor and focused window hypruse just left the seat in, so
    the next action can tell whether anything moved it in between."""
    if not _strict_on():
        return
    with contextlib.suppress(Exception):
        _seat["cursor"] = hyprctl.cursor_pos()
        _seat["active"] = (hyprctl.query("activewindow") or {}).get("address")


def guard_seat() -> None:
    """In strict mode, refuse to act when the cursor or focused window moved
    since hypruse's last action: something other than the agent (the human,
    or a popup) took the seat, and acting now could land in the wrong place.
    Cannot attribute the change; it only reports that the world moved.
    Fails closed: a seat that cannot be read cannot be proven still ours."""
    if not _strict_on() or _seat["cursor"] is None:
        return
    try:
        cursor = hyprctl.cursor_pos()
        active = (hyprctl.query("activewindow") or {}).get("address")
    except Exception as exc:
        raise TrustError(
            f"cannot read the seat to check for contention ({exc}); refusing "
            "to act while HYPRUSE_STRICT is set. Retry, or set HYPRUSE_STRICT=0 "
            "to disable this guard."
        ) from exc
    if cursor != _seat["cursor"] or active != _seat["active"]:
        raise TrustError(
            "the seat moved since hypruse last acted (cursor or focus changed "
            "without the agent): re-read desktop()/screenshot() and retry. "
            "Set HYPRUSE_STRICT=0 to disable this guard."
        )


# --- ownership marking ------------------------------------------------------


def marking_on() -> bool:
    return _flag("HYPRUSE_MARK")


# Border color for agent-owned windows. The rule matches the tag hypruse
# applies in note_launched, and Hyprland re-evaluates it when the tag is
# set, so windows tagged after they open still get the border. Installing it
# is hyprctl's problem: which IPC call carries a runtime rule depends on the
# session's config manager.
_MARK_COLOR = "rgb(ff5555)"


def init_marking() -> None:
    """Install the agent-owned border rule once at startup (no-op unless
    HYPRUSE_MARK is set). Left in place on exit: it only colors windows
    carrying hypruse's own tag, and a config reload clears it.

    A failure here does not stop the server: marking makes the agent's
    presence legible, it is not a containment boundary the way confinement
    is, and taking the desktop's guards away over a missing outline would
    be the worse trade. It does say so, though. The whole point of the
    feature is that the human at the desk can see what the agent owns, and
    a marking layer that quietly is not running is worse than one that was
    never asked for."""
    if not marking_on():
        return
    try:
        hyprctl.border_rule(_OWNED_TAG, _MARK_COLOR)
    except hyprctl.HyprctlError as exc:
        print(
            f"hypruse: HYPRUSE_MARK is set but the border rule did not install ({exc}); "
            f"agent windows are still tagged {_OWNED_TAG}, so a rule in your own "
            "Hyprland config will still color them",
            file=sys.stderr,
        )


_last_notify: dict[str, float] = {"ts": 0.0}


def notify_capture() -> None:
    """Flash an on-screen notice that the agent captured the screen,
    rate-limited so a burst of screenshots does not spam. No-op unless
    marking is on."""
    if not marking_on():
        return
    import time

    now = time.monotonic()
    if now - _last_notify["ts"] < 3.0:
        return
    _last_notify["ts"] = now
    hyprctl.notify("hypruse: agent captured the screen", ms=1500, color="rgb(ff5555)")
