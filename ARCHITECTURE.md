# Architecture

Ten-minute orientation for contributors.

## Module map

```
src/hypruse/
  cli.py         entry point: server by default, doctor / init / stop
                 subcommands
  server.py      MCP wiring: 15 tools (clipboard is opt-in), docstrings =
                 the agent-facing API
  hyprctl.py     all Hyprland IPC (queries + dispatchers), state trimming,
                 keybind decoding
  events.py      socket2 event stream: parser + wait primitive
  wire.py        raw Wayland client for zwlr_virtual_pointer_v1
  input.py       pointer orchestration (movecursor + wire) and wtype keyboard
  screenshot.py  grim capture: monitor / window / region + coord metadata
  a11y.py        AT-SPI accessibility-tree reader over D-Bus (busctl): named
                 controls, current values, exact coords, and focused-role
                 lookup; backs ui, marks, click_ui, then='ui', and the
                 auth-guard password-field check
  trust.py       opt-in confinement, auth interlock, seat-contention guard,
                 and ownership marking (HYPRUSE_CONFINE/AUTH_GUARD/STRICT/MARK)
  clipboard.py   wl-clipboard wrapper for the opt-in clipboard tool
  session.py     discovers HYPRLAND_INSTANCE_SIGNATURE / WAYLAND_DISPLAY
                 from runtime-dir sockets when the host stripped the env
  safety.py      activity beacon + kill-switch semantics
```

Rule of thumb: `server.py` validates and narrates; everything real happens
in the leaf modules, which stay importable and testable without MCP.

## The coordinate contract

One space rules everything: **Hyprland global logical coordinates** (what
`hyprctl cursorpos`, client `at`, and `dispatch movecursor` speak).

- `desktop` reports window geometry in it.
- `pointer` accepts it.
- `screenshot` captures *pixels* and returns `geometry` + `scale` per
  capture so callers map back: `global = origin + pixel / scale`.

If you touch anything coordinate-adjacent, preserve this contract; it is
what keeps multi-monitor and fractional scaling tractable.

## Why input works the way it does

- **Position** via `hyprctl dispatch movecursor x y`, authoritative,
  global, no per-monitor extent math, immune to
  [hyprwm/Hyprland#6749](https://github.com/hyprwm/Hyprland/issues/6749).
- **Buttons/axis** via a virtual pointer created over the raw wire
  (`wire.py` is ~250 lines: registry scan, bind, button/axis/frame, sync
  barrier, wl_display.error surfacing). No daemon, no uinput, no root.
- **Keyboard** via `wtype`: it uploads its own XKB keymap through
  `zwp_virtual_keyboard_v1`, which is why unicode and non-US layouts work.
  We shell out instead of reimplementing keymap upload; that wheel is
  round already.

A click's press and release always happen inside one tool call. A drag
holds a button across ~200 ms of cursor moves, so the SIGTERM path (what
the kill switch sends) runs a registered cleanup that releases any held
button first. Either way the process can die mid-run without stranding a
button, which is what makes both `hypruse stop` (graceful: signals the
beacon pid, releases the button, clears the beacon) and the blunter
`pkill -f hypruse` safe panic actions at any moment.

## Sequence of a typical agent step

1. `desktop` → find `firefox` at `0x…`, workspace 3, geometry.
2. `hypr focus_window 0x…` (IPC, ~ms), no vision spent.
3. `screenshot window=0x…` → crop + `geometry`/`scale` (or `ui` to read
   the accessibility tree by name, no pixels).
4. `pointer click x y`, computed from image pixel via the contract (or
   `click_ui name="Save"` to resolve and click in one call).
5. `keyboard type "…"`.
6. `desktop` again to verify the world changed as expected (or fuse it:
   most acting tools take `then='desktop'|'screenshot'|'ui'`).

## Trust layers

Four opt-in env flags (`HYPRUSE_CONFINE`, `HYPRUSE_AUTH_GUARD`,
`HYPRUSE_STRICT`, `HYPRUSE_MARK`) live in `trust.py` and are enforced as
`trust.guard_*` calls inside the acting tools in `server.py`: `pointer`,
`keyboard`, `click_ui`, `hypr`, and `use_bind` each refuse an out-of-scope
target, an authentication window, or a moved seat; `sequence` steps go
through those same tool functions, so they inherit the guards. `launch` is
the exception: it creates a new window (nothing to confine), so instead of
guarding it seeds the owned-set (`note_launched`). A guard raises
`TrustError`, which becomes the tool's error; every guard fails toward
*less* action (an unresolved target or a malformed scope refuses rather
than proceeds). `remember_seat` runs after an acting tool moves the seat so
the next `guard_seat` has a fresh baseline; the observation tools that
show current state (`desktop`, `screenshot`/`zoom` captures, `ui`,
`marks`) re-baseline too, so a tripped strict guard recovers when the
agent re-observes. Three always-on companion checks cover what the
window-based guards cannot see. Layer surfaces never appear in
`clients`, so a click aimed under a launcher or on-screen keyboard
would silently land on the layer (`click_ui` refuses, `pointer` appends
a warning naming the topmost covering surface), and a launcher holds
the keyboard grab, so `keyboard` refuses a window-targeted type and
annotates window-less typing with where the keys really went. A locked
session is invisible to both: modern lockers (hyprlock, swaylock >=
1.7) are `ext-session-lock-v1` clients rather than layer-shell ones, so
they appear in neither `clients` nor `layers`, and Hyprland exposes no
lock state over IPC. `trust.session_locked` therefore detects the
locker PROCESS, since the protocol returns the session the instant that
client exits, and the input-delivering tools (`keyboard`, `click_ui`,
and `pointer`'s click/drag/scroll; a bare `pointer` move only shifts the
cursor) refuse while it is up unless `allow_auth` says a human wants the
agent driving the prompt. The guards are the confinement
path over the same happy path above: step 4 is refused if the point is over
an out-of-scope or authentication window, step 2 if the target is out of
scope.

## Testing tiers

1. **Unit** (CI): pure functions, wire encoders/parsers, combo parsing,
   region parsing, state trimming against fixtures.
2. **Live seat-safe** (`pytest -m e2e`): real session, zero input events,
   including a virtual-pointer create/destroy handshake.
3. **Supervised** (`scripts/e2e_input.py`): the only tier that clicks and
   types; countdown, self-verifying via kitty remote control, restores
   focus. Never wire this into anything automatic.
