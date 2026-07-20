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
all input reaches a credential prompt). All are positive-detection and
best-effort: an unreadable system state never blocks anything.
"""

from __future__ import annotations

import contextlib
import os
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


def guard_use_bind() -> None:
    """Refuse use_bind while confinement is active: it dispatches the bind's
    action verbatim (exec, focuswindow, workspace switches, killactive, ...),
    an arbitrary compositor action that cannot be scoped to a window, so it
    is an escape hatch out of any confinement. Denied wholesale rather than
    guessed at."""
    scope = _confine_scope()
    if scope is not None:
        raise TrustError(
            f"use_bind runs an arbitrary compositor action and cannot be confined "
            f"({_describe(scope)}); it is refused while HYPRUSE_CONFINE is set"
        )


def _visible_workspaces(monitors: list[dict[str, Any]]) -> set[Any]:
    """Workspace ids currently shown on any monitor, INCLUDING a pulled-up
    special/scratchpad workspace (reported separately from activeWorkspace
    and drawn on top): a scratchpad password manager must not slip the
    coverage check."""
    visible: set[Any] = set()
    for m in monitors:
        visible.add((m.get("activeWorkspace") or {}).get("id"))
        special = (m.get("specialWorkspace") or {}).get("id")
        if special:  # 0 = no special workspace up
            visible.add(special)
    return visible


def _windows_under(x: float, y: float) -> list[dict[str, Any]]:
    """Every mapped, on-screen window whose rect covers (x, y). One batched
    hyprctl call (monitors + clients) so a pointer guard costs one fork."""
    monitors, clients = hyprctl.batch_query(["monitors", "clients"])
    visible = _visible_workspaces(monitors)
    return [
        c
        for c in clients
        if c.get("mapped", True)
        and (c.get("workspace") or {}).get("id") in visible
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
    under = _windows_under(px, py)
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
                f"({px:.0f}, {py:.0f}) is over {hit.get('class')}, a system authentication "
                "dialog; refusing the click. Pass allow_auth=true only if a human intends "
                "this credential action."
            )


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
    try:
        surfaces = hyprctl.parse_layers(hyprctl.query("layers"))
    except Exception:
        return None
    best: dict[str, Any] | None = None
    best_rank = -1
    for s in surfaces:
        if s.get("kind") not in hyprctl.FOCUS_STEALING_KINDS:
            continue
        g = s.get("geometry") or []
        if not (
            len(g) == 4
            and None not in g
            and g[0] <= x < g[0] + g[2]
            and g[1] <= y < g[1] + g[3]
        ):
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
# check can see them; Hyprland exposes no lock state over hyprctl either.
# The reliable signal is the PROCESS: the protocol hands the session back
# the instant the locking client exits, so a live locker means a locked
# (or actively locking) session. Older swaylock/gtklock releases drew
# their lock with layer-shell instead, which `layer_kind` still catches.
_LOCKER_COMMS = frozenset(
    {
        "hyprlock",
        "swaylock",
        "swaylock-effects",
        "gtklock",
        "waylock",
        "gtygra",  # hyprlock's binary name on some older packagings
    }
)


_PROC = "/proc"  # overridden in tests so the scan never reads live state


def session_locked() -> str | None:
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
            with open(f"{_PROC}/{entry.name}/comm") as fh:
                comm = fh.read().strip()
        except OSError:
            continue  # the process exited between listing and reading
        if comm in _LOCKER_COMMS:
            return comm
    return None


def guard_session_lock(allow_auth: bool) -> str:
    """Refuse input while the session is locked. Every event, pointer or
    keyboard, reaches the lock surface, which is a credential prompt: the
    agent cannot act on any window, and typing there feeds a password
    field. allow_auth downgrades this to the returned note, for the case
    where a human genuinely wants the agent to unlock."""
    locker = session_locked()
    if locker is None:
        return ""
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
    does not count. Positive detection and best-effort like
    covering_layer: unreadable layers yield ''."""
    try:
        surfaces = hyprctl.parse_layers(hyprctl.query("layers"))
    except Exception:
        return ""
    grabbers = [s for s in surfaces if s.get("kind") in hyprctl.KEYBOARD_GRABBING_KINDS]
    if not grabbers:
        return ""
    # a lock screen outranks a launcher when both are somehow mapped
    grabber = next((s for s in grabbers if s["kind"] == "lock"), grabbers[0])
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
    if kind == "osk":
        return "Dismiss the on-screen keyboard first."
    return (
        "Drive the launcher itself (call keyboard without window=), or "
        "close it first (usually esc)."
    )


def guard_covering_layer(x: float, y: float) -> None:
    """Refuse a window-targeted click while a focus-stealing layer covers
    the point: the layer would receive the click, the window's control
    never would, and reporting 'clicked' would be a lie. For click_ui,
    whose target is by definition a window control; a bare pointer click
    may legitimately aim at the layer itself, so it gets a warning in its
    result instead of a refusal."""
    s = covering_layer(x, y)
    if s is not None:
        raise TrustError(
            f"({x:.0f}, {y:.0f}) is covered by the {s['kind']} layer surface "
            f"{s['namespace']!r}: the click would land on that layer, not on "
            f"the window's control. {_dismiss_advice(s['kind'])} To click the "
            "layer deliberately, use `pointer`."
        )


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
# set, so windows tagged after they open still get the border. The matcher
# spelling changed across Hyprland versions (0.42+ dropped the colon:
# `tag NAME`, older is `tag:NAME`) and the field was renamed from the
# deprecated `windowrulev2 bordercolor` to `windowrule border_color` with a
# single 6-char color, so we try the current form first and fall back.
_BORDER_RULES = (
    f"border_color rgb(ff5555), tag {_OWNED_TAG}",   # Hyprland 0.42+
    f"border_color rgb(ff5555), tag:{_OWNED_TAG}",   # older
)


def init_marking() -> None:
    """Install the agent-owned border rule once at startup (best-effort;
    no-op unless HYPRUSE_MARK is set). Left in place on exit: it only colors
    windows carrying hypruse's own tag, and a config reload clears it."""
    if not marking_on():
        return
    for rule in _BORDER_RULES:
        try:
            hyprctl.keyword("windowrule", rule)
            return  # first accepted form wins
        except hyprctl.HyprctlError:
            continue


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
