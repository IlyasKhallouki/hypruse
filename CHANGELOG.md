# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added
- `sequence` tool: run an ordered list of actions
  (pointer/keyboard/hypr/wait_for) in one MCP call, so a click/type/enter
  micro-sequence costs one model round-trip instead of several (LLM calls
  dominate task latency). With `stop_on_change` (default), the run stops,
  best-effort, when it notices a structural change between steps that the
  step did not intend (a window opening, closing, or moving, or a switch
  to a different workspace than the step asked for), so later steps do not
  act on stale state; it returns what ran plus the changed desktop.
  Matching is payload-aware, so the human doing the same kind of action to
  a different target still stops the run. Bare focus changes are not
  watched (a click focuses a window), so give a keyboard step a `window=`
  address to type into a specific window reliably. Bounded to 20 steps and
  ~30s total wall-clock. `then` observes the final state.

### Changed
- `desktop` assembles its snapshot from a single `hyprctl --batch` call
  instead of five separate queries, cutting the per-command fork overhead
  (~4x fewer forks; measured snapshot ~28 ms to ~20 ms). Output unchanged.

## [0.5.0] - 2026-07-17

### Added
- `keyboard` takes an optional `window` address that focuses the target
  window before typing, so keystrokes land in the intended app rather than
  whatever currently holds focus (focus-follows-mouse can retarget a shared
  seat between calls). Composes with `then`.
- Act-and-observe fusion: `pointer`, `keyboard`, `hypr`, and `use_bind`
  take an optional `then` argument that appends a fresh view of the result
  to the same tool call, so the agent sees an action's effect without a
  second round-trip (LLM calls dominate task latency). `then='desktop'`
  appends a semantic snapshot (~25 ms, best for window/focus changes),
  `then='screenshot'` a stable capture (best for visual changes),
  `then='none'` (default) nothing.

### Changed
- Screenshots default to JPEG q90 instead of PNG. On a 1080p frame this is
  roughly 13x faster to encode (measured ~640 ms to ~50 ms full monitor)
  and about 3x smaller, while full-res q90 reads UI text well. Pass
  `lossless=true` on `screenshot`/`zoom` for exact pixels (PNG). Because
  `capture_stable` inherits the format, its poll speeds up by the same
  factor. The byte-budget fit ladder now degrades JPEG quality before
  resolution, since grim's downscale filter is slower than a full-res
  capture; resolution is a last resort.

## [0.4.1] - 2026-07-17

### Fixed
- Multi-monitor and fractional-scaling hardening: `desktop` now reports
  monitor `geometry` in global logical coordinates (the same space window
  `at`/`size` and `pointer` use) instead of physical mode pixels, so on a
  fractionally-scaled or rotated monitor the reported size matches the
  layout. Rotated monitors surface a `transform` field, and their logical
  footprint has the axes swapped. Monitor-containment logic (used by
  `zoom` and screenshot scale lookup) shares one transform-aware
  `logical_rect` source of truth in `hyprctl`, fixing wrong-monitor
  selection and wrong `scale` stamps near fractional-scale seams.

## [0.4.0] - 2026-07-17

### Added
- `clipboard` tool, opt-in via `HYPRUSE_CLIPBOARD=1` (and never in
  read-only mode): read or write the text clipboard through wl-clipboard.
  Default installs keep the documented no-clipboard-access posture.
- `stable` parameter on `screenshot` and `zoom`: capture repeatedly (up
  to 2s) until two consecutive frames are byte-identical, so a capture
  right after an action does not land mid-animation; the metadata
  reports `stable` true/false.

### Changed
- Whole-notch scrolls now go out as discrete axis events (axis_discrete
  on the virtual-pointer wire), so applications that step per wheel
  click see real notches; fractional deltas keep the continuous path.

## [0.3.0] - 2026-07-17

### Added
- `zoom` tool: native-resolution re-capture around an estimated global
  point (`x,y`, optional `size` "WxH" and window clamp), returning the
  same pixel-to-global mapping metadata as `screenshot` plus the echoed
  `point`. This promotes the coarse-to-fine precision loop from a usage
  convention in the server instructions to a first-class primitive, the
  mechanism vendor computer-use implementations and the GUI-agents
  literature converged on (see the README Research section).

### Changed
- Roadmap: OCR click-by-text is dropped in favor of the zoom loop; the
  README gains a Research section with the sources behind the decision.

### Fixed
- Screenshot metadata could stamp the wrong `scale` near monitor seams on
  multi-monitor layouts with fractional scaling: monitor containment
  treated hyprctl's physical mode width/height as logical bounds. Monitor
  rects are now derived logically (size divided by scale, axes swapped
  for 90/270-degree transforms); `zoom` uses the same logical rects for
  clamping and monitor selection.

## [0.2.1] - 2026-07-16

### Fixed
- Keybinds are now run through the new `use_bind` tool (execute a bind's
  action by combo) instead of synthetic keypresses: Hyprland does not route
  virtual-keyboard input through its keybind matcher, so `keyboard` combos
  reach the focused app but never trigger compositor binds like `super+f`.
  The `binds`/`keyboard` docs and server instructions are corrected to
  match.
- `wait_for` pre-checks current state so it does not miss an event that
  fired before it could subscribe: `window_close` returns immediately when
  nothing matches, `workspace` when the target is already active.

## [0.2.0] - 2026-07-16

### Added
- `hypruse init`: registers the server in detected MCP clients (per-client
  confirmation, timestamped config backup, never overwrites an existing
  entry), then runs doctor. `hypruse doctor`: one-command diagnostics for
  dependencies, session discovery, the event socket, a virtual-pointer
  handshake, and a live capture.
- `binds` tool: the user's keybinds decoded to combos with descriptions,
  so agents drive the desktop through its owner's own shortcuts.
- `wait_for` tool: block on real compositor events (window open/close,
  workspace change, title change) with a match filter and timeout.
- Read-only mode: `HYPRUSE_READONLY=1` exposes only observation tools.

### Changed
- `launch` now subscribes to the Hyprland event socket before dispatching
  and blocks on the actual openwindow event (polling remains as fallback).
- Console entry point moved to `hypruse.cli:main`; bare `hypruse` still
  runs the stdio server, so existing client registrations keep working.

## [0.1.2] - 2026-07-16

### Changed
- Docs, docstrings, and package metadata use plain ASCII punctuation; em
  and en dashes removed throughout.

## [0.1.1] - 2026-07-16

### Fixed
- Ctrl+C no longer hangs with a "could not acquire lock for stdin at
  interpreter shutdown" fatal: the beacon's SIGINT handler was fighting the
  MCP/anyio runtime's own shutdown. SIGINT is now left to the runtime (the
  beacon is still cleaned up via `atexit`); only SIGTERM is handled.

### Added
- Running `hypruse` directly in a terminal now prints what it is and how to
  register it, then exits, instead of silently blocking as a stdio server
  waiting for a client that will never connect.

## [0.1.0] - 2026-07-16

Initial release.

### Added
- MCP stdio server with six tools: `desktop`, `screenshot`, `pointer`,
  `keyboard`, `hypr`, `launch`, plus cross-tool guidance in the server
  `instructions` and terse per-tool schemas for eager client loading.
- Semantic desktop snapshot over hyprctl IPC (token-lean, fixture-tested).
- Screenshots via grim: focused monitor, exact window crop, region zoom,
  with true output dimensions and scale for exact pixel→global mapping.
  Default returns a saved PNG path (works with hosts that mangle inline
  image blocks); `HYPRUSE_SCREENSHOT_MODE=image` returns wire-level image
  content auto-fit to a transport byte budget and long-edge cap
  (`HYPRUSE_MAX_IMAGE_BYTES`, `HYPRUSE_MAX_IMAGE_EDGE`) by degrading format
  before resolution, so the API never silently downscales under the model.
  Coarse-to-fine clicking guidance to counter pixel-estimation error.
- Pointer input over a raw `zwlr_virtual_pointer_v1` wire client, no
  ydotool, no uinput, no daemon; positioning via `hyprctl movecursor`.
- Keyboard input via wtype (XKB keymap upload; unicode/layout-correct),
  with combo parsing (`ctrl+shift+t`, `super+enter`, bare-mod taps).
- Workspace/window dispatchers and app launch with new-window detection,
  including single-instance apps (browsers) whose window is relocated to
  the requested workspace.
- Session discovery: finds `HYPRLAND_INSTANCE_SIGNATURE` / `WAYLAND_DISPLAY`
  from runtime sockets when the host launches the server with a stripped
  environment (dbus/systemd-activated desktop apps).
- Activity beacon + Waybar kill-switch indicator module.
- Unit suite, seat-safe live e2e, MCP stdio round-trip tests, supervised
  input verification script, CI.
