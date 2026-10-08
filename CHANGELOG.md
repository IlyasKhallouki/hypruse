# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.12.0] - 2026-10-08

### Fixed
- **Hyprland 0.57 workspaces.** Hyprland 0.57 drops `id` from special and
  named workspaces (hyprwm/Hyprland#16269). `desktop` sorted workspaces by
  id and would have crashed for anyone with a scratchpad up, and the
  visibility checks would have stopped seeing a pulled-up scratchpad, so
  the confinement guard would have failed open over it. A workspace is now
  named by its number when it has one and by its name otherwise
  (`"special:vault"`), in `desktop` and in every check. On 0.56 too, a
  pulled-up special workspace now reads `visible: true` in `desktop`.
- **The lock guard asks the compositor.** It used to look for a hyprlock
  or swaylock process. It now asks Hyprland (`hyprctl -j locked`, 0.41+),
  which also catches Omarchy 4's lock, built into its Quickshell shell,
  and a locker that crashed, which leaves the session locked. The process
  scan still names the locker in the refusal, and decides alone only when
  Hyprland cannot be asked.
- **Omarchy 4's polkit prompt.** Omarchy 4 draws its polkit prompt as a
  layer surface, not a window, so the auth interlock never saw it. A new
  `auth` layer kind refuses typing and clicks into it unless `allow_auth`,
  and stops `click_ui` and a running `sequence`. Omarchy's menu, pickers,
  bar popups, bar and notifications are classified too, instead of
  `unknown`.
- **Screenshots of hidden or covered windows** (#4). `screenshot(window=)`
  cropped the window's rectangle out of the screen, so a window on a hidden
  workspace or under another window came back as some other window's
  pixels, labelled as this one. It now captures the window itself with
  `grim -T` (grim 1.5+), scaled by the window's own monitor, and the
  metadata gains `visible`. Many apps stop drawing while hidden, so a
  hidden window's capture carries a note that it may show how the window
  last looked, and `stable=true` reports `stable: null` for it rather than
  vouching for a frozen frame. Window captures are refused while the
  session is locked, since Hyprland would hand over the window behind the
  lock. Where `grim -T` is not available, a window on screen falls back to
  the crop and a hidden one is refused, with how to bring it forward.
  `zoom(window=)` refuses a hidden window.
- **Qt apps with GTK theme integration** (#6, thanks @Pillumz). A Qt app
  run with `QT_QPA_PLATFORMTHEME=gtk3` registers two accessibility roots
  under one PID, the empty GTK bridge first, so `ui` found no controls. The
  first of them that holds a window now wins.
- A stable-capture test could pass by accident on a fast machine (#5,
  thanks @Pillumz).
- `hypruse pointer scroll DY` without DX exited with a usage error on
  Python 3.11 and on 3.12 before 3.12.7, an argparse bug that also kept CI
  red since 0.11.0.

### Added
- Every MCP tool declares a title and annotations: the seven observation
  tools are read-only, the acting ones destructive.

### Changed
- `launch`'s description opens with "Open an app".
- Removed the `Environment :: X11 Applications` classifier.

## [0.11.0] - 2026-09-13

### Added
- **Shell verbs**: every tool is now a command, for agents that run shell
  commands instead of MCP (Pi, Codex, Hermes, OpenClaw, or a Claude Code
  session that prefers a skill to a tool list). The verbs are the tool
  names, a tool's `action` is a positional sub-verb (`hypruse pointer click
  800 60`, `hypruse keyboard type hello --window 0x..`, `hypruse hypr
  workspace 3`, `hypruse clipboard read`), and the calls go through the
  same functions the MCP server registers, so every trust guard, the
  journal and the activity beacon apply unchanged. The contract is built
  for a program reading the output: compact plain text, one line per fact
  (`--json` for the raw result as one line); a capture prints its file path
  and coordinate metadata, never bytes (`--out PATH` moves it); errors are
  one line on stderr; exit 0 delivered, 1 error, 2 usage, 3 refused by a
  trust layer or by read-only mode, 4 ran but found nothing to act on (a
  `wait_for` timeout, no accessibility tree, an ambiguous name, a sequence
  that stopped). `--then` and `--dry-run` work as on the tools, `--allow-auth`
  where the tool has it, `-` reads text from stdin, and `launch` takes the
  app's own flags after `--`. `hypruse --help` lists every verb.
- **Agent skill** (`skills/hypruse`, shipped inside the package): a
  SKILL.md following the Agent Skills specification that teaches an agent
  the desktop-first workflow, the coordinate contract, the exit codes and
  the safety rules, with `references/verbs.md` and `references/recipes.md`
  one level down. `hypruse skill install` copies it to
  `~/.agents/skills/hypruse` and links it into the skills directory of each
  agent present on the machine (`--agent NAME` for one, created if absent;
  `--copy` instead of symlinks); `hypruse skill uninstall` removes exactly
  what it made and nothing that is not hypruse's. `hypruse init` offers the
  install and `doctor` reports it. The repo layout also works with
  `npx skills add IlyasKhallouki/hypruse`.
- **Cross-process state** for the verbs, in
  `$XDG_RUNTIME_DIR/hypruse/cli-state.json`, keyed by compositor instance:
  the `marks` numbering (so `click_ui --mark N` resolves), the `launched`
  confinement set (which would otherwise be empty again after the launch),
  and the `HYPRUSE_STRICT` seat baseline, without which the seat guard is a
  no-op in every fresh process and strict mode would silently fail open.
  Under `launched` confinement the set is pruned against the live window
  list before it is trusted, since an address can be reused.
- `hypruse serve` runs the MCP server explicitly (bare `hypruse` still does).
- Journal records written through the verbs carry `source: "cli"`, and
  `hypruse journal` shows it; they are still the agent's own actions, so
  `replay` re-issues them. The session header is written once per run of
  identical flags rather than once per process.

### Changed
- `hypruse.server` no longer imports the MCP stack at import time: the
  tools ask for content blocks through two small factories, and the FastMCP
  app is built on first use (`server.app()`, still reachable as
  `server.mcp`). A shell verb therefore never loads `mcp`, and starts in
  a few hundred milliseconds instead of several seconds.
- `safety.arm()` installs the SIGTERM and atexit cleanup without raising the
  beacon; `init()` calls it. A verb running beside a live server leaves the
  server's beacon alone but is still safe under `pkill -f hypruse` mid-drag.
- The file-mode capture metadata now carries `path`, so a reader takes the
  location from the record rather than from the sentence around it.
- The CLI's default usage is a complete verb list, and the interactive
  notice the server prints on a terminal points at it.

### Fixed
- `hypruse --help` and `-h` started the MCP server (silently, when stdin
  was a pipe, which it always is under an agent's shell): the entry point
  routed every leading dash to the server. Help and `--version` now print
  and exit without importing it.

## [0.10.0] - 2026-08-31

### Added
- **Action journal** (`HYPRUSE_JOURNAL`): an append-only NDJSON record of
  every tool call, one JSON object per line, in
  `$XDG_STATE_HOME/hypruse/journal.ndjson` or a path you name. Each entry
  carries the tool, the arguments as called, how long it took, and the
  outcome, and a session opens with the trust flags it ran under, so a
  refusal three hours in can be read next to the configuration that
  produced it. Refusals are recorded with the guard's own message: the
  confinement, auth, and seat layers previously decided and forgot, and
  this is the only place that history exists. `kind` separates input
  delivered to the desktop from the agent merely looking, which is the
  split a privacy audit needs; observation payloads are never recorded,
  only that they happened, so the journal cannot become a second copy of
  everything the agent saw. Typed and copied text is stored as a length
  plus a short digest unless `HYPRUSE_JOURNAL_TEXT=1` says otherwise,
  because keystrokes are passwords; a digest is not anonymity, though, so
  the file is written 0600 in a 0700 directory and rotates at
  `HYPRUSE_JOURNAL_MAX_BYTES` (8 MiB, one generation kept, 0 to never
  rotate). Unlike a guard, the recorder fails toward the ACTION: an
  unwritable journal, an unusable path, or a path that is not a regular
  file warns once on stderr and gets out of the way rather than failing a
  desktop over a log line.
- **Dry run** (`HYPRUSE_DRYRUN`): every acting tool validates its
  arguments and runs every trust guard, then reports the plan it was
  about to execute and delivers nothing. A rehearsal whose refusals
  differed from the real run would be worth nothing, so the guards stay
  exactly where they are; what changes is that no click, keystroke,
  window op, or clipboard write leaves the process, not even the window
  focus that normally precedes typing. Enforced twice: at each tool, and
  at the effect boundary itself (`input`'s six delivery functions,
  `hyprctl dispatch`, `clipboard write`), so a path nobody thought of
  fails loudly with nothing delivered instead of quietly acting during
  what the caller was told was a simulation. The server instructions tell
  the agent dry run is on, so it reports a plan rather than retrying an
  action that "did not work".
- `hypruse journal` reads the record back: one line per call, a second
  line for anything a trust layer refused, and a summary of actions,
  observations, refusals, and errors. `--acts`, `--refused`, `-n N`,
  `-v`.
- `hypruse replay` re-issues a journal's actions through the same tool
  functions, so the same guards apply to the replay. It prints the plan
  and stops there unless `--execute`. Everything it refuses, it refuses
  before taking the seat, because a refusal that lands halfway leaves the
  desktop part-way through someone else's plan: an action recorded by a
  newer hypruse (skipping it and running the rest would report success
  for a different sequence of events than the one recorded), a recorded
  window that no longer exists (`--skip-missing` runs the rest), text
  recorded as a digest, a `click_ui(mark=N)` whose numbering died with
  the session that drew it, a clipboard write without the same
  `HYPRUSE_CLIPBOARD` opt-in the server needs, and `HYPRUSE_READONLY` or
  `HYPRUSE_DRYRUN`. It paces itself from the recorded think time, capped
  by `--max-gap` and scaled by `--speed`. Its own actions are journaled
  and marked, so replaying a file twice runs the recorded plan rather
  than the plan plus the replay of it, and it raises the activity beacon
  only when no live hypruse already holds it, so a running server keeps
  its own `hypruse stop` and Waybar indicator.

### Fixed
- **Hyprland's Lua config manager** (0.56+). Every window operation failed
  on a desktop configured with a `hyprland.lua`: `launch`, all six `hypr`
  actions, `click_ui`, `keyboard` with a `window=` target, `use_bind`, and,
  without a named seat, every `pointer` and `sequence` move. 0.56 did not
  remove the string dispatchers, as the reports read; it added a second
  config manager and gave it a different IPC. Under that one `hyprctl
  dispatch X` evaluates the Lua expression `hl.dispatch(X)`, so
  `movecursor 100 100` is a syntax error, and `hyprctl keyword` is refused
  outright. Which manager runs is decided by the config file's extension,
  and `hyprland.lua` is looked for first, so a fresh 0.56 install is a Lua
  desktop while a 0.56 install with the old config is untouched: this is
  not something a version check can answer. hypruse now probes the manager
  once (`hyprctl -j status`, a JSON line, not the two-second `systeminfo`),
  speaks whichever dialect came back, and re-probes if a call fails, since
  `hyprctl reload full-reset` can swap managers under a running server.
  Callers are unchanged: a window op is still described once, in the
  legacy shape, and `hyprctl.py` translates. Thanks to @Sokoshy for the
  report, the `hl.dsp` enumeration, and the verified migration examples
  ([#1](https://github.com/IlyasKhallouki/hypruse/issues/1)).
- Ownership marking (`HYPRUSE_MARK`) was silently dead on a Lua config:
  the border rule could not install and no window was ever tagged, so even
  the documented workaround of putting the rule in your own config matched
  nothing, and the only surviving signal was the toast. The rule now goes
  out as `hl.window_rule` there, and a failure to install it says so on
  stderr instead of leaving a visibility layer quietly not running.
- `use_bind` on a Lua config now explains itself. Hyprland stores a Lua
  bind as an anonymous closure and exposes no IPC route to call it, so the
  tool refused with a Lua parser error about `__lua` plus an upstream note
  suggesting the caller had sent bad syntax, and a dry run cheerfully
  planned an action that could never run. `binds` reports those as action
  `lua` and drops the registry index, which is meaningless outside the
  compositor; the combo and description still say what the workflow is,
  and `use_bind` names the cause and points at `hypr` and `launch`.
- `binds` no longer lists pointer binds as if they were pressable. The
  `mouse` flag it filtered on is the hyprlang manager's, and a Lua config
  never sets it, so `SUPER+mouse:272` came through as a keyboard combo.
- `hypr` and `launch` now reject a `workspace` argument containing the
  punctuation a workspace identifier never has. On a Lua desktop the
  dispatcher argument is evaluated inside the compositor's own interpreter
  with the standard library open, and `workspace` was the one agent-supplied
  dispatcher argument with no structural validation, on deliberately the
  one `hypr` action that skips the confinement guard. Everything hypruse
  sends on the Lua path is now built as an escaped literal rather than
  interpolated.
- `hypruse doctor` reports which config manager the session runs.

### Changed
- `hypr` now validates its action, its `workspace` argument, and its
  target address before any guard runs and before anything is
  dispatched, matching what `keyboard` already did. A malformed address
  under confinement now says so, instead of reporting the window as
  missing.
- `keyboard(action="key")` parses the combo before it focuses the target
  window. An unknown modifier used to surface from `wtype` after the
  focus had already moved, which is exactly the seat change a refused
  call must not leave behind.
- `pointer` checks the button name, the x/y pairing, and a non-zero
  scroll itself rather than leaving all three to the input layer, so the
  same call is rejected whether or not it is going to be delivered.

## [0.9.4] - 2026-07-26

### Added
- hypruse is listed in the official [MCP registry](https://registry.modelcontextprotocol.io)
  as `io.github.IlyasKhallouki/hypruse`, so MCP clients that browse the
  registry can discover and install it. The entry ships as `server.json`
  and carries the env flags (`HYPRUSE_SCREENSHOT_MODE`, `HYPRUSE_READONLY`,
  `HYPRUSE_CONFINE`, and the rest of the trust layer) so clients can
  surface them at install time. The `release` workflow publishes the entry
  on a version tag, right after PyPI.

No code changes: the registry verifies package ownership by finding a
`mcp-name:` token in the README PyPI serves as the package description, and
a published version's description cannot be edited, so the token needed a
release of its own.

## [0.9.3] - 2026-07-22

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
- `click_ui` now surfaces the session-lock note on the `allow_auth` path
  (it was discarded, so a click into a locked session reported a control
  it never reached), and refuses under a lock like `pointer`/`keyboard`
  do; both were untested at that call site.
- Two `_LOCKER_COMMS` entries could never match a real process:
  `swaylock-effects` is 16 characters and the kernel caps
  `/proc/PID/comm` at 15 (now stored truncated), and `gtygra` was a
  fabricated name with no basis in any packaging (removed). An
  import-time assertion now rejects any over-length entry.
- Under a lock, the keyboard-grab note no longer also fires and
  contradicts the lock note about where the keys went.
- The session-lock guard now refuses (not just annotates) a
  `window=`-targeted or confined input even with `allow_auth`, closing a
  regression where `keyboard(type, window=X, allow_auth=true)` under a
  lock would type X's secret into the credential prompt: naming a target
  window, or a confinement scope, contradicts intending to drive the
  prompt, so it is refused the same way a keyboard-grabbing layer is.
  `click_ui` refuses under a lock outright (it always targets a window
  control that the input cannot reach). `pointer` emits a single note,
  not two contradictory ones, when a lock and a covering layer coincide.
- A windowless `keyboard` type now fails closed under `HYPRUSE_CONFINE`
  when the active window cannot be resolved because hyprctl is
  unreadable, instead of skipping the confinement check (the virtual
  keyboard delivers keys even when hyprctl is down). "Nothing focused"
  and "the compositor is unreadable" are now distinguished.
- The strict-mode password-field refusal runs before the `focuswindow`
  dispatch, so a refused type no longer moves the human's focus first.
- `swaylock-effects` is packaged as a drop-in that ships
  `/usr/bin/swaylock` (comm `swaylock`, already covered), so the
  fabricated `swaylock-effect` locker entry was removed rather than
  modeling a `/proc` name no real packaging produces.

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
