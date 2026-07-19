# Architecture

Ten-minute orientation for contributors.

## Module map

```
src/hypruse/
  cli.py         entry point: server by default, doctor / init subcommands
  server.py      MCP wiring: 15 tools (clipboard is opt-in), docstrings =
                 the agent-facing API
  hyprctl.py     all Hyprland IPC (queries + dispatchers), state trimming,
                 keybind decoding
  events.py      socket2 event stream: parser + wait primitive
  wire.py        raw Wayland client for zwlr_virtual_pointer_v1
  input.py       pointer orchestration (movecursor + wire) and wtype keyboard
  screenshot.py  grim capture: monitor / window / region + coord metadata
  a11y.py        AT-SPI accessibility-tree reader over D-Bus (busctl): the
                 `ui` tool's element names, coordinates, and values
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

A press and its release always happen inside one tool call, which is what
makes `pkill -f hypruse` a safe panic action at any moment.

## Sequence of a typical agent step

1. `desktop` → find `firefox` at `0x…`, workspace 3, geometry.
2. `hypr focus_window 0x…` (IPC, ~ms), no vision spent.
3. `screenshot window=0x…` → crop + `geometry`/`scale`.
4. `pointer click x y`, computed from image pixel via the contract.
5. `keyboard type "…"`.
6. `desktop` again to verify the world changed as expected.

## Testing tiers

1. **Unit** (CI): pure functions, wire encoders/parsers, combo parsing,
   region parsing, state trimming against fixtures.
2. **Live seat-safe** (`pytest -m e2e`): real session, zero input events,
   including a virtual-pointer create/destroy handshake.
3. **Supervised** (`scripts/e2e_input.py`): the only tier that clicks and
   types; countdown, self-verifying via kitty remote control, restores
   focus. Never wire this into anything automatic.
