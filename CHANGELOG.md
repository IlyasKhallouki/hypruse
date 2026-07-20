# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Fixed
- `HYPRUSE_STRICT` no longer locks up permanently after a human touches
  the seat. The guard's own error says "re-read desktop()/screenshot()
  and retry", but no read ever re-baselined the seat, so once tripped it
  refused every action for the rest of the session. Every observation of
  current state (`desktop`, the `screenshot`/`zoom` capture path, `ui`,
  `marks`) now re-arms the guard, making the documented recovery real.
  The guard also fails closed when the seat cannot be read at all,
  matching the trust layers' fail-toward-less-action invariant.
- `hypr close_window` with a `then=` observation no longer snapshots
  before the destroy lands (the observation used to still list the
  window it had just closed). It subscribes to the event socket before
  dispatching and waits, up to 1s, for the compositor to report the
  close; a window that will not close (an unsaved-changes prompt) is
  reported honestly instead of claimed closed, and a socket dying
  mid-confirmation degrades to a note rather than a tool error.
- `wait_for('layer_open')` gained a level-triggered pre-check: a
  launcher that mapped before the wait subscribed (the
  use_bind-then-wait pattern) satisfies the wait immediately instead of
  timing out forever. Filtered waits only, like the workspace pre-check.
  `layer_close` deliberately gets no pre-check: "nothing matching is
  mapped" also describes a layer that has not opened yet, and a wrong
  "already closed" is worse than an honest timeout.
- Input aimed under a focus-stealing layer surface (launcher, lock
  screen, on-screen keyboard) stops being reported as success the
  target never saw. `click_ui` refuses when such a layer covers the
  click point; `pointer` appends a warning naming the layer (clicking
  INTO a launcher is the legitimate way to drive one); `keyboard`
  refuses a `window=`-targeted type while any such layer holds the
  keyboard grab (the keys provably cannot reach that window, and
  `allow_auth` does not apply, since intending credential entry
  contradicts naming another target), refuses typing into a layer
  entirely while `HYPRUSE_CONFINE` is set (a layer surface is not a
  window and a launcher runs what it is given, the same escape
  `use_bind` is refused for), refuses typing under a lock screen unless
  `allow_auth=true`, and otherwise annotates window-less typing with
  where the keys really went.
- `click_ui` with `then='ui'` observes the clicked window, not whatever
  window holds focus after the click (a spawned dialog could steal it).
- `wait_for('title_change')` waits on `windowtitlev2` only: the
  address-only `windowtitle` event could never satisfy a title match,
  and an unfiltered wait could return a payload with no title in it.
  This requires a Hyprland new enough to emit `windowtitlev2`.
- `marks` now flashes the same `HYPRUSE_MARK` capture notice as
  `screenshot`/`zoom` (it takes a real capture and was silent).
- Input is refused while the session is LOCKED. This gap was found by
  audit: the lock-screen protections above were built on the `layers`
  list, but modern lockers (hyprlock, swaylock >= 1.7) are
  `ext-session-lock-v1` clients, not layer-shell clients, so they never
  appear there and every lock branch was unreachable against the locker
  people actually run. Hyprland exposes no lock state over IPC either,
  so detection now uses the locker process, which the protocol
  guarantees exits on unlock. `pointer`, `keyboard`, and `click_ui`
  refuse while it is up; `allow_auth=true` downgrades that to a note
  for a human who wants the agent to unlock. The docs claiming lock
  screens appear under `layers` were corrected in the same pass.
- `click_ui` with `then='ui'` no longer answers "window not found" when
  the click dismissed its own window (Close, OK, Discard). It falls
  back to the focused window, which is normally the parent.
- `keyboard` validates `action`/`text`/`keys` before focusing the
  target window, so a malformed call no longer moves the human's focus
  on its way to raising.
- A layer refusal that names the covering surface now names the TOPMOST
  one (highest level, most recently mapped) rather than the first in
  list order, and its "close it first" advice is written per kind: a
  lock screen does not yield to esc, and an on-screen keyboard is
  dismissed rather than escaped.

### Changed
- Read-only mode ships its own server instructions and observation-tool
  descriptions covering only the observation workflow, instead of the
  full-surface text that kept advertising the seven stripped acting
  tools (`click_ui`, `sequence`, `use_bind`, ...).

## [0.9.2] - 2026-07-19

### Fixed
- `sequence`: a `keyboard` or `click_ui` step with `window=X` no longer
  aborts the run when X lives on another workspace. Focusing X switched
  to its workspace, and the sequence mistook its own focus-induced switch
  for a human takeover, dropping the remaining steps (the documented
  type-then-enter pattern lost its enter). Any focus-capable step now
  excuses the workspace it actually landed on.
- `hypr fullscreen` / `toggle_floating` with no target act on the active
  window, but skipped the `HYPRUSE_STRICT` seat guard, so in strict mode
  they could fullscreen a window the human had just refocused. The
  target-less branch now checks the seat guard (address-targeted actions
  still skip it, since they name their window).

### Changed
- Documentation reorganized for the larger 0.8.0/0.9.x feature set: a new
  README "Features" section groups the tools into five capabilities
  (semantic control, click-by-name, the zoom loop, round-trip fusion,
  trust) ordered cheapest-first, each with a "use it well" tip; the
  roadmap no longer lists shipped work (`sequence`, `then=` fusion, the
  a11y tools) as future and gains "Deeper reading", "Trust", and
  "Measurement" sections; the "How it works" diagram shows the AT-SPI
  (`busctl`) path the `ui`/`marks`/`click_ui` tools use; the security
  model documents `pointer`'s `allow_auth` and that `use_bind` is refused
  under confinement; ARCHITECTURE gains a trust-layer section and notes
  `hypruse stop`. Several small factual fixes (the auth-guard override
  covers `pointer` too, a stale desktop-snapshot timing figure, the
  `super+enter` keyboard example that belonged to `use_bind`).

## [0.9.1] - 2026-07-19

### Fixed
- `HYPRUSE_MARK` ownership marking was a no-op on modern Hyprland. The
  0.9.0 code used `hyprctl keyword windowrulev2 "bordercolor ..."`, but
  `windowrulev2` is deprecated (a no-op), the field was renamed to
  `border_color`, it takes a single 6-char color, and the tag matcher
  dropped its colon (`tag NAME`), so the rule was silently rejected and
  swallowed. Marking now: reliably tags each agent-owned window
  `hypruse-owned` and flashes an on-screen notice when the agent opens a
  window (both verified working), and best-effort installs the corrected
  `border_color` windowrule (current and legacy matcher forms). A runtime
  window rule does not render on every Hyprland version/config, so the
  README documents the one-line config rule for a guaranteed outline;
  hypruse's tagging matches it.

## [0.9.0] - 2026-07-19

### Added
- Optional confinement and trust layers (`src/hypruse/trust.py`), each an
  opt-in env flag that fails toward less action and composes with the
  approval/beacon layers:
  - `HYPRUSE_CONFINE` restricts input to a scope of windows: `launched`
    (only windows hypruse opened this session, seeded from `launch`),
    `class:a,b`, or `workspace:1,2`. Keyboard, `click_ui`, and `hypr`
    window ops are refused outside scope; a `pointer` click is refused
    when any window under the point is out of scope (Hyprland's client
    list is not z-ordered, so it fails closed rather than guess the top
    window). A malformed value refuses every action.
  - `HYPRUSE_AUTH_GUARD` (default on) refuses to click or type into a
    known authentication dialog (polkit agents, keyring prompt);
    `=strict` also refuses typing into a password field detected in the
    accessibility tree. A per-call `allow_auth=true` on `keyboard` /
    `click_ui` overrides it and, changing the argument shape, surfaces in
    the approval prompt.
  - `HYPRUSE_STRICT` refuses to act when the cursor or focused window
    moved since hypruse's last action (the seat was taken); the agent
    must re-observe and retry.
  - `HYPRUSE_MARK` borders agent-owned windows (a runtime `windowrulev2`
    on a tag, torn down on exit) and flashes a rate-limited on-screen
    notice on capture.
- `hypruse stop` subcommand: an emergency stop that signals the running
  server to shut down gracefully (releasing any held pointer button and
  clearing the beacon), cleaner than `pkill` and safe to bind:
  `bind = SUPER SHIFT, BackSpace, exec, hypruse stop`.

## [0.8.0] - 2026-07-19

### Added
- `desktop` reports layer-shell surfaces: launchers (wofi/rofi), bars,
  notification popups, and lock screens are not windows and were
  previously invisible to the semantic snapshot. They appear under
  `layers` with namespace, a best-effort `kind` (prefix heuristic that
  degrades to `unknown`), level, monitor, and global geometry, from the
  same single batched hyprctl call.
- `wait_for` gains `layer_open` / `layer_close` (match on the layer
  namespace, e.g. waiting for a launcher to appear), `urgent` (a window
  demands attention), and `screencast` (screen sharing started/stopped),
  parsed from the matching socket2 events.
- `sequence` treats a keyboard-grabbing layer surface (launcher, lock
  screen, on-screen keyboard) appearing mid-run as a structural change
  and stops; notification popups and bars are explicitly noise and do
  not abort the run.
- `then='ui'`: a fourth act-and-observe mode that appends the focused
  window's accessible elements with their CURRENT values to the acting
  call's own result. Reading the effect of typing or toggling costs a
  few hundred exact tokens instead of a screenshot, and degrades to a
  note when the app exposes no tree.
- `marks` tool: Set-of-Marks capture, the window screenshot with every
  accessible control drawn as a numbered mark plus a JSON legend (role,
  name, current value, exact global click point per number). Drawing
  shells out to ImageMagick (optional, same idiom as grim/wtype/busctl);
  without it the exact legend still returns. Grounded in the set-of-mark
  visual-prompting and OSWorld a11y+screenshot results already cited in
  the README Research section.
- `click_ui` tool: click a control by accessible NAME or by a `marks`
  number in ONE call. The coordinate is resolved from the accessibility
  tree, the window is focused first, and the click goes through the real
  pointer path so it stays visible and inherits every safety guarantee
  (beacon, panic kill, seat serialization); AT-SPI DoAction was
  evaluated and rejected because invisible synthetic input would bypass
  that layer. Exact name match beats substring, an ambiguous name
  returns the candidate list instead of guessing, and mark offsets are
  window-relative so a moved window stays clickable. Also available as a
  `sequence` op.

## [0.7.1] - 2026-07-19

### Fixed
- The pixel-to-global coordinate contract on scaled monitors, two root
  causes found by an adversarial audit. grim's `-s` flag is an ABSOLUTE
  logical-to-pixel factor, not a multiplier of native pixels: every
  capture that emitted `-s` (the image-mode edge cap, an explicit
  `scale`, or a byte-budget rung) came back smaller than intended on any
  monitor with scale other than 1.0 while the metadata claimed otherwise,
  so mapped clicks landed off by the monitor scale (halved on a 2x
  display). And a capture rect straddling monitors with different scales
  is rendered by grim at the GREATEST intersected scale, not the scale
  under the rect's top-left corner. Both paths now model grim's real
  semantics, with HiDPI and cross-seam regression tests.
- `sequence`: a `wait_for` step now runs against the sequence's own event
  stream. The between-step settle drain used to consume the very event
  the wait then waited for on a fresh socket, so click-then-wait (the
  documented pattern) falsely timed out for any app that mapped its
  window within the 0.2s settle.
- `sequence`: a workspace step with a relative or alias target (`+1`,
  `e+1`, `previous`) no longer aborts the rest of the sequence. The
  compositor reports the resolved workspace name, which never equaled
  the literal argument, so the sequence mistook its own switch for a
  human takeover.
- `wait_for('workspace')` with no `match` filter now actually waits for
  the next switch instead of returning the current workspace instantly
  as already-satisfied. A filtered workspace wait still pre-checks.
- The Waybar module builds its JSON with `jq` and the beacon whitelists
  `last_action` down to a plain token: a crafted tool argument could
  previously inject JSON through the hand-built module output and blank
  the robot indicator while the agent still had hands on the desktop.
- The SIGTERM kill switch arriving mid-`drag` now releases the held
  button before the process dies, via a registered cleanup; a drag holds
  a button across ~200 ms of cursor moves, the one window the
  one-call press/release guarantee did not cover.
- Concurrent tool calls can no longer interleave input on the shared
  seat: pointer and keyboard operations are serialized by a reentrant
  lock (MCP hosts may issue tool calls in parallel and sync tools run on
  worker threads). The kill-switch cleanup stays lock-free.

### Changed
- CI tests all four advertised Python versions (3.11-3.14); the `mcp`
  dependency gets an upper bound (`<2`); both PKGBUILDs declare the
  `hyprland` runtime dependency; RELEASING.md documents bumping both
  version sources and a test enforces their agreement.
- README/ARCHITECTURE drift reconciled: 13 tools, `ui` listed in
  read-only mode, `sequence` listed among `then=` takers, lossless-PNG
  timing re-measured (~800 ms, not ~440 ms), `HYPRUSE_MAX_IMAGE_EDGE`
  documented, and a new security-model layer names prompt injection via
  on-screen text as untrusted input with approval as the backstop.

## [0.7.0] - 2026-07-18

### Added
- `ui` tool: read a window's AT-SPI accessibility tree and return its
  clickable elements by accessible name with exact global coordinates and
  no screenshot, for GTK/Qt apps that expose one. Read through the `busctl`
  CLI (no new Python dependencies), correlating the a11y app to the window
  by connection PID (frame-title fallback for multi-process apps) and
  mapping window-relative extents to global through the window's hyprctl
  position (AT-SPI screen coordinates are unreliable on Wayland). It is an
  observation tool (available in read-only mode) and returns a
  fall-back-to-vision message when an app exposes nothing (terminals,
  Electron/Chrome without `--force-renderer-accessibility`). Grounded in
  the GUI-agent literature (OSWorld, UFO, Agent-S); see the README
  Research section.
- `ui` also reports a control's CURRENT VALUE, not just its label: `value`
  for text typed into an entry and for a slider or spinner number,
  `percent` for a slider's position within its range, `checked` for a box
  or toggle. Reads are gated on role, so finding a button costs no extra
  calls, and a password field's contents are never read. Many dropdowns
  expose no value at all (GTK's newer combo boxes publish neither text nor
  selection), so a screenshot is still the way to read a rendered value.

## [0.6.0] - 2026-07-17

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
