# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added
- Screenshot auto-fit: in image mode, captures fit a transport byte budget
  (`HYPRUSE_MAX_IMAGE_BYTES`, default 700 kB raw ≈ 933 kB base64, sized to
  Claude Desktop's 1 MB result cap) by degrading format before resolution
  (native PNG → full-res JPEG → stepped downscale); the applied scale is
  folded into the metadata so pixel→global mapping stays exact. Optional
  `scale` tool parameter for deliberate zoom-outs. File mode stays native.

### Fixed
- Screenshot image mode returned the fastmcp `Image` helper inside a mixed
  content list, which some SDK versions refuse to serialize ("Unable to
  serialize unknown type") — seen live from a desktop client. Both modes
  now return wire-level `ImageContent`/`TextContent` directly, and a new
  e2e tier round-trips every mode through a real MCP stdio client.
- Session discovery: when the host app launches hypruse with a stripped
  environment (dbus/systemd-activated desktop apps), the server now finds
  `HYPRLAND_INSTANCE_SIGNATURE` and `WAYLAND_DISPLAY` from their sockets
  under `XDG_RUNTIME_DIR` instead of failing with "is hyprland running?".
  Existing env is never overridden; newest live instance wins.
- `launch`: single-instance apps (e.g. browsers) open their window from an
  existing process and ignore `[workspace N silent]` exec rules — the
  window is now detected wherever it lands and moved to the requested
  workspace. Detection window is `wait_s` (default 8 s, was a fixed 3 s).
  Found in the first real-world test drive.
- Stored screenshots are pruned to the 20 newest (XDG_RUNTIME_DIR is
  tmpfs/RAM).

### Docs
- Performance notes: measured server latencies and the permission-prompt
  effect, with a Claude Code allowlist example.

## [0.1.0] — 2026-07-16

Initial release.

### Added
- MCP stdio server with six tools: `desktop`, `screenshot`, `pointer`,
  `keyboard`, `hypr`, `launch`.
- Semantic desktop snapshot over hyprctl IPC (token-lean, fixture-tested).
- Screenshots via grim: focused monitor, exact window crop, region zoom,
  with pixel→global coordinate metadata. Default returns a saved PNG path
  (verified working with hosts that mangle inline image blocks);
  `HYPRUSE_SCREENSHOT_MODE=image` opts into inline MCP image content.
- Pointer input over a raw `zwlr_virtual_pointer_v1` wire client — no
  ydotool, no uinput, no daemon; positioning via `hyprctl movecursor`.
- Keyboard input via wtype (XKB keymap upload; unicode/layout-correct),
  with combo parsing (`ctrl+shift+t`, `super+enter`, bare-mod taps).
- Workspace/window dispatchers and app launch with new-window detection.
- Activity beacon + Waybar kill-switch indicator module.
- Unit suite, seat-safe live e2e, supervised input verification script, CI.
