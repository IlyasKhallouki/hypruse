"""Trust layers for a shared human/agent seat.

Opt-in constraints, each gated by an env var, that limit what an agent can
touch and make its presence legible. They compose with the always-on
approval and beacon layers; none persists anything, and every one fails
toward LESS action, never more.

  HYPRUSE_CONFINE     restrict input to a scope of windows:
                      `launched` (only windows hypruse itself opened),
                      `class:foo,bar`, or `workspace:3,special:notes`
  HYPRUSE_AUTH_GUARD  refuse to drive polkit/authentication dialogs and to
                      type into password fields (default on; per-call
                      allow_auth overrides)
  HYPRUSE_STRICT      refuse to act when the seat moved without hypruse
                      since its last action (human or app took over)
  HYPRUSE_MARK        border every agent-owned window and flash an on-screen
                      notice on capture, so the human sees the agent's hands

The server calls the guard_* functions at the top of each acting tool; a
raised TrustError becomes the tool's error, which the MCP client surfaces.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from hypruse import a11y, hyprctl, safety


class TrustError(RuntimeError):
    """An action was refused by a trust layer (confinement, auth, seat)."""


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


# --- confinement ------------------------------------------------------------

_owned: set[str] = set()  # window addresses hypruse launched this session

_OWNED_TAG = "hypruse-owned"


def note_launched(address: str) -> None:
    """Record a window hypruse opened, so `launched` confinement and the
    ownership border know it is the agent's."""
    if address:
        _owned.add(address)
        if _flag("HYPRUSE_MARK"):
            with contextlib.suppress(hyprctl.HyprctlError):
                hyprctl.dispatch("tagwindow", f"+{_OWNED_TAG}", f"address:{address}")


def owned() -> set[str]:
    return set(_owned)


def _confine_scope() -> tuple[str, tuple[str, ...]] | None:
    """Parse HYPRUSE_CONFINE, or None when confinement is off. Raises
    TrustError (which denies the action) on a malformed value, so a typo
    fails closed instead of silently disabling the sandbox."""
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


def _client_in_scope(client: dict[str, Any], scope: tuple[str, tuple[str, ...]]) -> bool:
    kind, vals = scope
    if kind == "launched":
        return client.get("address") in _owned
    if kind == "class":
        return client.get("class") in vals
    ws = client.get("workspace", {}) or {}
    return str(ws.get("id")) in vals or str(ws.get("name", "")) in vals


def _describe(scope: tuple[str, tuple[str, ...]]) -> str:
    kind, vals = scope
    return "windows hypruse launched" if kind == "launched" else f"{kind} in {list(vals)}"


def _client(address: str) -> dict[str, Any] | None:
    return next(
        (c for c in hyprctl.query("clients") if c.get("address") == address), None
    )


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


def guard_window(address: str) -> None:
    """Refuse if `address` is outside the confinement scope, resolving it
    first. For callers (hypr) that hold only an address."""
    scope = _confine_scope()
    if scope is None:
        return
    client = _client(address)
    if client is None:
        raise TrustError(f"window {address} not found; call desktop() for current addresses")
    guard_client(client)


def guard_point(x: float | None, y: float | None) -> None:
    """Refuse a pointer action at (x, y) unless EVERY visible window under
    the point is in scope. Hyprland's clients array is not z-ordered, so if
    any window covering the point is out of scope hypruse cannot prove the
    top one is safe and fails closed. A point over no window is allowed
    (nothing confined is touched)."""
    scope = _confine_scope()
    if scope is None or x is None or y is None:
        return
    monitors = hyprctl.query("monitors")
    visible = {m.get("activeWorkspace", {}).get("id") for m in monitors}
    under = [
        c
        for c in hyprctl.query("clients")
        if c.get("mapped", True)
        and (c.get("workspace", {}) or {}).get("id") in visible
        and _covers(c, x, y)
    ]
    outside = [c for c in under if not _client_in_scope(c, scope)]
    if outside:
        classes = ", ".join(sorted({c.get("class", "?") for c in outside}))
        raise TrustError(
            f"({x:.0f}, {y:.0f}) is over a window outside the confinement scope "
            f"({classes}; {_describe(scope)}); clicking there is refused"
        )


def _covers(client: dict[str, Any], x: float, y: float) -> bool:
    at, size = client.get("at"), client.get("size")
    if not at or not size:
        return False
    return at[0] <= x < at[0] + size[0] and at[1] <= y < at[1] + size[1]


# --- authentication interlock -----------------------------------------------

# Desktop authentication agents: their windows own a separate trust domain
# (they gate sudo, PolicyKit, credential prompts). The agent must not click
# or type into them without explicit human consent.
_AUTH_CLASSES = frozenset(
    {
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
    # the password-field check walks the a11y tree (busctl subprocesses per
    # node), too costly for every keystroke, so it is opt-in
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
            f"{cls} is a system authentication dialog; refusing to drive it. "
            "Pass allow_auth=true only if a human intends this credential action."
        )


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
        app = a11y.app_for_pid(bus, client.get("pid"), client.get("title", ""))
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
    Cannot attribute the change; it only reports that the world moved."""
    if not _strict_on() or _seat["cursor"] is None:
        return
    try:
        cursor = hyprctl.cursor_pos()
        active = (hyprctl.query("activewindow") or {}).get("address")
    except Exception:
        return
    if cursor != _seat["cursor"] or active != _seat["active"]:
        raise TrustError(
            "the seat moved since hypruse last acted (cursor or focus changed "
            "without the agent): re-read desktop()/screenshot() and retry. "
            "Set HYPRUSE_STRICT=0 to disable this guard."
        )


# --- ownership marking ------------------------------------------------------


def marking_on() -> bool:
    return _flag("HYPRUSE_MARK")


def init_marking() -> None:
    """Install a runtime border rule for agent-owned windows and arrange to
    remove it at shutdown. No-op unless HYPRUSE_MARK is set."""
    if not marking_on():
        return
    with contextlib.suppress(hyprctl.HyprctlError):
        hyprctl.keyword(
            "windowrulev2", f"bordercolor rgb(ff5555) rgb(ff2222), tag:{_OWNED_TAG}"
        )
    safety.on_shutdown(_teardown_marking)


def _teardown_marking() -> None:
    with contextlib.suppress(hyprctl.HyprctlError):
        hyprctl.keyword("windowrulev2", f"unset, tag:{_OWNED_TAG}")


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
