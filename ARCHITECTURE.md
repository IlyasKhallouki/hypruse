# Architecture

Ten-minute orientation for contributors.

## Module map

```
src/hypruse/
  cli.py         entry point: server by default, doctor / init / stop /
                 journal / replay / skill subcommands, --help
  verbs.py       the 15 tools as shell verbs: argparse over the same tool
                 functions, the output and exit-code contract, renderers
  cli_state.py   what a one-shot verb remembers between processes (marks
                 numbering, launched-confinement set, strict seat baseline)
  skill.py       the packaged Agent Skill (skills/hypruse) and its install
                 into each agent's skills directory
  server.py      the tools (clipboard is opt-in), docstrings = the
                 agent-facing API; the FastMCP app is built on first use
  hyprctl.py     all Hyprland IPC (queries + dispatchers), the config-manager
                 probe and the Lua dialect, state trimming, keybind decoding
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
  journal.py     NDJSON record of every tool call (HYPRUSE_JOURNAL) and the
                 dry-run mode (HYPRUSE_DRYRUN) with its effect-boundary
                 barrier; what `hypruse journal` and `hypruse replay` read
  clipboard.py   wl-clipboard wrapper for the opt-in clipboard tool
  session.py     discovers HYPRLAND_INSTANCE_SIGNATURE / WAYLAND_DISPLAY
                 from runtime-dir sockets when the host stripped the env
  safety.py      activity beacon + kill-switch semantics
```

Rule of thumb: `server.py` validates and narrates; everything real happens
in the leaf modules, which stay importable and testable without MCP.

## Two surfaces, one set of tools

The MCP server and the shell verbs call the same module-level functions in
`server.py`, so a guard, a journal entry or a beacon touch is written once.
What differs is transport. A tool returns either a string or a list of
content blocks, and it asks for those blocks through `_text()`/`_image()`
rather than naming `mcp.types`: the MCP path gets the pydantic objects
FastMCP expects, the CLI path (`use_plain_blocks()`) gets a plain `Block`
with the same fields. That, plus building the FastMCP app on first access
(`app()`, reachable as `server.mcp`) instead of at import, is what keeps a
verb's startup at a few hundred milliseconds: importing the MCP stack costs about two seconds
of pydantic model building, which a process that only prints text and file
paths must not pay.

The CLI adds three things the server does not need. `cli_state.py` carries
across processes what a long-lived server keeps in memory: the `marks`
numbering, the `launched` confinement set, and the strict-mode seat
baseline (without which `guard_seat` is a no-op in every fresh process,
the one case that fails open). It is keyed by compositor instance, since
window addresses are heap pointers, and every consumer degrades to
"nothing remembered". A verb that acts takes a cross-process lock and
always arms the SIGTERM cleanup (`safety.arm()`), even when a live server
already holds the beacon, because `pkill -f hypruse` matches the verb too
and a verb killed mid-drag must still release its button. And the journal
stamps a verb's records with `source: "cli"`, never `by`, so they remain
the agent's own actions to `replay`; the session header is written once
per run of identical flags rather than once per process.

## The coordinate contract

One space rules everything: **Hyprland global logical coordinates** (what
`hyprctl cursorpos`, client `at`, and cursor positioning speak).

- `desktop` reports window geometry in it.
- `pointer` accepts it.
- `screenshot` captures *pixels* and returns `geometry` + `scale` per
  capture so callers map back: `global = origin + pixel / scale`.

If you touch anything coordinate-adjacent, preserve this contract; it is
what keeps multi-monitor and fractional scaling tractable.

## The two config managers

Hyprland 0.56 added a Lua config manager beside the original hyprlang one
and picks between them by the config file's extension, so a session runs
one or the other and no version check can tell you which. The choice
reaches the IPC, not just the config file: under the Lua manager `hyprctl
dispatch X` evaluates the Lua expression `hl.dispatch(X)`, so every legacy
dispatcher string is a syntax error, and `hyprctl keyword` is refused
outright.

`hyprctl.provider()` probes it once with `-j status` and caches the answer;
`dispatch()` translates the legacy call into the `hl.dsp.*` expression that
lands on the same compositor action, and re-probes if a call fails, since
`hyprctl reload full-reset` can change the manager under a running server.
Callers upstack never see the difference, which is the point: a window op
is described once.

Two things the Lua manager takes away rather than renames. `use_bind`
cannot run a bind, because a Lua config binds an anonymous closure that
Hyprland exposes no IPC route to call; `binds` still reports the combo and
description, and `use_bind` refuses with that explanation instead of a
parser error. And a runtime window rule becomes `hl.window_rule` rather
than a keyword, which is what `border_rule()` exists to hide.

Everything the Lua path sends is built by `lua_str()` and the `_lua_*`
builders. That is a security boundary, not formatting: `hyprctl dispatch`
hands its argument to the compositor's interpreter as an expression with
the standard library open, so an argument that is not a literal is code
running inside the compositor.

## Why input works the way it does

- **Position** via the compositor's own cursor dispatcher (`movecursor` on
  hyprlang, `hl.dsp.cursor.move` on Lua), authoritative, global, no
  per-monitor extent math, immune to
  [hyprwm/Hyprland#6749](https://github.com/hyprwm/Hyprland/issues/6749).
  On a named seat it goes over the wire instead, because that dispatcher
  moves the one cursor the human owns.
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
3. `screenshot window=0x…` → the window's own pixels + `geometry`/`scale` (or `ui` to read
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
they appear in neither `clients` nor `layers`. Hyprland reports the lock
itself (`hyprctl -j locked`, 0.41+), so `trust.session_locked` asks the
compositor. That also catches a lock built into the shell (Omarchy 4
locks from its Quickshell shell, with no locker process to find) and a
crashed locker, which the protocol leaves locked rather than handing the
session back. A scan of /proc for a known locker process names it in the
refusal, and decides alone only when the compositor cannot be asked.
The input-delivering tools (`keyboard`, `click_ui`,
and `pointer`'s click/drag/scroll; a bare `pointer` move only shifts the
cursor) refuse while it is up unless `allow_auth` says a human wants the
agent driving the prompt. The guards are the confinement
path over the same happy path above: step 4 is refused if the point is over
an out-of-scope or authentication window, step 2 if the target is out of
scope.

## The record: journal, dry run, replay

`journal.py` is the layer beneath the guards: they decide, it remembers.
`HYPRUSE_JOURNAL` appends one NDJSON line per tool call, written by a
`@journal.journaled` decorator applied at each tool's DEFINITION site
rather than at MCP registration, because `sequence` dispatches its steps
through the module-level tool functions and those steps are the entries
replay re-issues. Refusals are recorded with the guard's own message,
which is the only place that history exists. Typed and copied text is
recorded as a length plus digest unless `HYPRUSE_JOURNAL_TEXT` says
otherwise, since the alternative is a file of passwords. Unlike a guard,
the recorder fails toward the ACTION: an unwritable journal warns once on
stderr and gets out of the way.

`HYPRUSE_DRYRUN` is enforced twice. Each acting tool runs its argument
checks and every `trust.guard_*` call, then returns the plan it was about
to execute; a rehearsal whose refusals differ from the real run would be
worth nothing, so the guards stay exactly where they are. Underneath,
`journal.refuse_if_dry` raises at the effect boundary itself (`input`'s
six delivery functions, `hyprctl.dispatch`, `clipboard.write`), so a path
nobody thought of fails loudly with nothing delivered instead of quietly
acting during what the caller was told was a simulation.

`hypruse replay` re-issues a journal's actions through the same tool
functions, so the same guards apply. It prints the plan and stops unless
`--execute`, and every refusal happens in a pre-flight, before the seat
is taken, because a refusal that lands halfway leaves the desktop
part-way through someone else's plan. Window addresses are the honest
limit: they are heap pointers, so a journal outlives them, and an address
can even be reused by a different window later, which no pre-flight can
see.

Both the plan and `hypruse journal` render values that the audited party
wrote, so `cli._safe` strips control characters before re-embedding them
in output hypruse builds, the same treatment and for the same reason as
`safety._ACTION_JUNK` on the beacon. Without it a recorded argument could
carry ESC sequences that erase the lines above it, and the plan a human
approves would not be the plan that runs.

## Testing tiers

1. **Unit** (CI): pure functions, wire encoders/parsers, combo parsing,
   region parsing, state trimming against fixtures.
2. **Live seat-safe** (`pytest -m e2e`): real session, zero input events,
   including a virtual-pointer create/destroy handshake.
3. **Supervised** (`scripts/e2e_input.py`): the only tier that clicks and
   types; countdown, self-verifying via kitty remote control, restores
   focus. Never wire this into anything automatic.
