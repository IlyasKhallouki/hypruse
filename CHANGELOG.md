# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

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
