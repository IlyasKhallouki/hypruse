# hypruse

**Computer use for [Hyprland](https://hypr.land).** An [MCP](https://modelcontextprotocol.io) server that gives AI agents native hands on your Wayland desktop: workspaces, windows, mouse, keyboard, screenshots.

No ydotool daemon. No root. No portals. No X11.

![Claude reading the desktop over IPC, switching to btop on another workspace, and reporting back](https://raw.githubusercontent.com/IlyasKhallouki/hypruse/main/assets/demo.gif)

## Why

Computer use exists on macOS and Windows. On Linux there is effectively nothing: the Claude Desktop Linux beta explicitly ships **without** screen control, Anthropic's reference implementation is an X11 container, and the existing Wayland attempts lean on setuid uinput hacks or GNOME-only portals.

Meanwhile Hyprland already exposes everything an agent needs, better than any accessibility bridge: a complete IPC surface for state and window management, and first-class Wayland protocols for input. hypruse just wires them to MCP:

- **Semantic first.** `desktop` returns the real window/workspace tree (addresses, classes, titles, geometry) in one call. The agent switches workspaces and focuses windows the way you do (instantly, over IPC), not by squinting at pixels.
- **Vision when it matters.** Screenshots of a monitor, an exact window crop, or a zoomed region, with the geometry/scale metadata to map any pixel back to a clickable coordinate: a coarse-to-fine loop [grounded in the GUI-agents research](#research).
- **Native input.** Clicks and scrolls are spoken directly over the Wayland wire (`zwlr_virtual_pointer_v1`); typing goes through `wtype`'s virtual keyboard with a proper XKB keymap, unicode-safe on any layout.

## How it works

```
agent (Claude Code, or any MCP client)
   │ stdio
   ▼
hypruse
   ├── hyprctl -j ········▶ desktop state: monitors, workspaces, windows
   ├── hyprctl dispatch ··▶ focus / move / close / launch / movecursor
   ├── grim ··············▶ screenshots: monitor, window crop, region
   ├── wtype ·············▶ keyboard (zwp_virtual_keyboard_v1, real XKB keymap)
   └── raw Wayland wire ··▶ click & scroll (zwlr_virtual_pointer_v1)
```

Design decisions:

- **No ydotool / uinput.** That path needs a daemon, udev rules or root, and types US scancodes that break on other layouts. hypruse is just another Wayland client of your compositor, same standing as `wlrctl`.
- **No portals.** `xdg-desktop-portal-hyprland` does not implement the RemoteDesktop portal (InputCapture is capture, not injection), so anything built on libei/portals silently degrades on Hyprland. hypruse doesn't try.
- **Cursor positioning via `hyprctl dispatch movecursor`** (global logical coordinates, exact on any monitor layout), with only button/axis events on the virtual pointer, sidestepping the known multi-monitor bugs of absolute virtual-pointer motion ([hyprwm/Hyprland#6749](https://github.com/hyprwm/Hyprland/issues/6749)).

## Tools

| tool | what it does |
|---|---|
| `desktop` | One-call semantic snapshot: monitors, workspaces, windows (address/class/title/geometry), active window, cursor |
| `screenshot` | Focused monitor, exact window crop by address, or `x,y,WxH` region; returns image + coordinate-mapping metadata; `stable=true` waits for the frame to settle |
| `zoom` | Native-resolution re-capture around an estimated point (optionally clamped to a window): the precision step before clicking small controls, same metadata contract |
| `pointer` | move / click / drag / scroll (discrete wheel notches) in global coordinates |
| `keyboard` | Type literal text (unicode-safe) or press combos: `ctrl+shift+t`, `super+enter`, `F5` |
| `hypr` | Switch workspace, focus/move/close windows, fullscreen, floating (pure IPC, milliseconds) |
| `launch` | Start an app (optionally silent on another workspace), block on its actual `openwindow` event, return its address; detects single-instance apps (browsers) whose window ignores exec rules and moves it to the requested workspace |
| `binds` | The user's own keybinds, decoded (`SUPER+Q`, action, description); the agent runs one with `use_bind` |
| `use_bind` | Execute a keybind by combo (`SUPER+F`), running its bound action, so the agent drives the owner's own launchers and shortcuts |
| `wait_for` | Block on real compositor events (window open/close, workspace change, title change) with a match filter and timeout; replaces sleep-and-hope in multi-step automations |
| `clipboard` | Read or write the text clipboard via `wl-clipboard`; opt-in, exists only with `HYPRUSE_CLIPBOARD=1` in the server env |

## Install

Requirements: Hyprland, `grim`, `wtype` (most Hyprland setups already have both), and [uv](https://docs.astral.sh/uv/). Optional: `wl-clipboard` for the opt-in clipboard tool.

Arch Linux, from the [AUR](https://aur.archlinux.org/packages/hypruse):

```sh
yay -S hypruse        # or hypruse-git for main
```

Then let it set itself up and verify the environment:

```sh
hypruse init     # detects your MCP clients, registers (asks first), runs doctor
hypruse doctor   # just the diagnostics
```

Manual registration, Claude Code:

```sh
claude mcp add -s user hypruse -- uvx hypruse
```

From a source checkout:

```sh
claude mcp add -s user hypruse -- uv run --directory /path/to/hypruse hypruse
```

Any other MCP client: run `uvx hypruse` as a stdio server.

**Read-only mode:** set `HYPRUSE_READONLY=1` in the server config to expose only the observation tools (`desktop`, `screenshot`, `zoom`, `binds`, `wait_for`). The agent can see and narrate but cannot click, type, or launch. A good first week.

### Claude Desktop (Linux beta)

The Linux beta ships **without** Anthropic's first-party computer use, but stdio MCP servers work in chat, which makes hypruse the workaround. In `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hypruse": {
      "command": "uvx",
      "args": ["hypruse"],
      "env": { "HYPRUSE_SCREENSHOT_MODE": "image" }
    }
  }
}
```

Two Desktop-specific notes: use `image` mode (Desktop renders inline MCP images and has no file-read tool), and the app must run natively inside your Hyprland session so the server inherits `WAYLAND_DISPLAY`/`HYPRLAND_INSTANCE_SIGNATURE`; from a VM or container it cannot reach your compositor. If your Desktop install bypasses tool-approval prompts, treat the [Waybar indicator + panic keybind](waybar/) as mandatory, not optional.

## Security model

Read this section before installing. **hypruse hands an agent your mouse, your keyboard, your screen contents, and an app launcher.** The layers that keep that sane:

1. **Approval:** MCP clients gate tool calls. In Claude Code, allowlist the read-only tools (`desktop`, `screenshot`) and leave `pointer`/`keyboard`/`hypr`/`launch` on ask-first until you trust a workflow.
2. **Visibility:** the server maintains an activity beacon (`$XDG_RUNTIME_DIR/hypruse/state.json`); the shipped [Waybar module](waybar/) is invisible when idle and shows a robot indicator while an agent has hands on your desktop.
3. **Interruption:** click the indicator, or bind a panic key: `bind = SUPER SHIFT, BackSpace, exec, pkill -f hypruse`. Killing it mid-action is safe: button press/release pairs never span tool calls, so it cannot die holding a button.
4. **The seat is shared.** There is one cursor and one keyboard focus, and Hyprland's focus-follows-mouse means a cursor move alone can retarget keystrokes. Don't type while an agent is driving; watch the indicator.
5. **Scope:** stdio only (no network listener), nothing persisted except the beacon, and no clipboard access unless you opt in: `HYPRUSE_CLIPBOARD=1` registers a `clipboard` tool (never in read-only mode); clipboards hold passwords, so leave it off unless a workflow needs it. A screenshot sees everything visible: treat an agent session like screen sharing.

## Performance

Measured on a live session (Hyprland 0.55, 1080p, 20 windows): `desktop`
~30 ms, workspace/window dispatch ~10-20 ms, screenshots ~0.5 s. If tool
calls *feel* slow, it is almost certainly the MCP **approval prompt** in
front of each call, not the server. Allowlist the tools you trust and the
latency disappears. Claude Code (`.claude/settings.json`):

```jsonc
{
  "permissions": {
    "allow": [
      "mcp__hypruse__desktop",
      "mcp__hypruse__screenshot",
      "mcp__hypruse__hypr"
      // add pointer/keyboard/launch once you trust your workflows
    ]
  }
}
```

## Coordinates

Everything speaks Hyprland's global logical coordinates, the space `hyprctl cursorpos` and window `at` use. Screenshots are pixel-space; each capture returns `geometry` and `scale` so `global = origin + pixel / scale`. On scale 1.0 monitors (most setups) image pixels *are* global coordinates.

The `zoom` tool does the precision arithmetic for the agent: give it an estimated global point and it captures a native-resolution box around it, clamped to the screen (or to a window), with the same metadata contract. That two-step loop, estimate on the full view then re-estimate on the zoom, is the [research-backed](#research) way to hit small controls.

In image mode, captures automatically fit the host's result-size limit (Claude Desktop caps tool results at 1 MB): format degrades before resolution (native PNG, then full-res JPEG, then stepped downscale) because full-res JPEG reads UI text better than half-res PNG. The applied scale is folded into the returned metadata, so coordinate mapping stays exact; tune with `HYPRUSE_MAX_IMAGE_BYTES`, or pass `scale` for a deliberate zoom-out.

By default the screenshot tool writes a PNG under `$XDG_RUNTIME_DIR/hypruse/` and returns its path; MCP hosts with a file reader (Claude Code's `Read`) render it natively. This default exists because some hosts (including Claude Code 2.1.x) serialize inline MCP image blocks to base64 text the model cannot see. `HYPRUSE_SCREENSHOT_MODE=image` switches to inline image content blocks for hosts that render them correctly.

## Development

```sh
uv sync --group dev
uv run pytest            # unit tests, no compositor needed
uv run pytest -m e2e --override-ini addopts=   # live seat-safe checks
uv run python scripts/e2e_input.py             # supervised: takes the seat ~10s
```

The input e2e is deliberately manual: it borrows your cursor and keyboard, counts down, proves click/scroll/type delivery by reading the target terminal's screen back over kitty remote control, and restores your focus.

## Roadmap

Grounded in measured hot-path latencies and the finding that LLM calls are 76 to 96% of computer-use task latency ([OSWorld-Human](https://arxiv.org/abs/2506.16042)), so cutting round-trips beats shaving milliseconds.

**Faster**

- JPEG-default captures: default screenshots to JPEG q90 instead of PNG (measured ~13x faster full-monitor capture, ~640 ms to ~50 ms, and ~3x smaller payload), keeping lossless PNG on request. Also speeds the wait-for-stable poll, which inherits the format.
- Quality-first fit ladder: under a byte budget, step JPEG quality down before ever downscaling, since grim's convolution downscale is slower than a full-res capture.
- Leaner `desktop()`: collapse its five `hyprctl` forks into one `hyprctl --batch` (~28 ms to ~12 ms) and fold in the focused window, per-window workspace, and a recent-events tail.
- In-process wlr-screencopy over the raw wire (as input already works): drop grim's fork floor from small captures and add damage-tracked wait-for-stable that returns the instant the screen settles.

**Fewer round-trips**

- `sequence` tool: run an ordered list of actions (click, type, key, wait) in one call, aborting the moment the compositor reports an unexpected change. Abort-on-change keeps a shared seat safe.
- Act-and-observe fusion: let any action append a fresh semantic `desktop` snapshot to its result, so the agent sees the effect without a second screenshot round-trip.
- Semantic screen diff: after an action, return only what changed (window topology from the event stream, or a bounded changed-region crop) instead of a full frame.

**More reach**

- AT-SPI element tree via `busctl`: read GTK/Qt widget trees and click by accessible name at exact coordinates with no screenshot, mapping window-relative extents to global through the hyprctl geometry hypruse already has. No new Python dependencies; vision stays the fallback.
- `record` tool: a scoped GIF or mp4 of the agent driving the desktop, via wf-recorder (a wlroots-family binary like grim).
- Notifications: list recent desktop notifications and block on the next match, like `wait_for` for notifications.
- Action journal, replay, and dry-run: an auditable NDJSON record of every action, replayable, with a validate-only mode.

**Platform**

- sway / niri support: the wire client already speaks the wlr protocols; what remains is an IPC layer alongside `hyprctl.py` (contributions welcome).
- Headless end-to-end tests in CI: needs a QEMU virtio-gpu VM, since Hyprland's aquamarine backend requires a real GPU render node that hosted runners lack.
- Zoom-loop precision benchmark: measure click accuracy of the coarse-to-fine loop against known targets.

## Related projects

| project | approach | on Hyprland |
|---|---|---|
| computer-use-linux | AT-SPI + portals, ydotool fallback | GNOME-first; the RemoteDesktop portal it prefers is not implemented by xdg-desktop-portal-hyprland |
| hyprmcp | hyprctl wrapper | window management only; no screenshots or input |
| wayland-mcp | evemu input, VLM analysis | requires elevated setup for input; no Hyprland semantics |
| Anthropic computer-use-demo | X11 + xdotool in Docker | a sandboxed reference environment rather than a live desktop |

## Research

hypruse deliberately ships no OCR engine and no accessibility bridge; its precision mechanism is the coarse-to-fine zoom loop: screenshot a window, re-capture the target region at native resolution, click through the exact coordinate mapping. That choice follows what the GUI-agents field converged on. Anthropic's computer use grounds clicks from raw pixels and ships a `zoom` action as the documented fix for small text; its troubleshooting guidance for near-miss clicks prescribes zooming and region cropping, never OCR [1]. OpenAI's CUA is likewise pure pixel grounding under resolution discipline, with no OCR layer at all [2]. Zoom is also the measured lever: training-free iterative zooming roughly doubles high-resolution grounding accuracy (OS-Atlas-7B, 18.9 → 49.7 on ScreenSpot-Pro) [3], and the benchmark's official harness implements a dozen grounding-model adapters plus four zoom/crop strategies, but zero OCR baselines [4]. Vision-only agents match or beat agents that additionally consume HTML or accessibility trees [5], substrates Wayland doesn't guarantee anyway, and state-of-the-art native agents run from screenshots alone [7]. OCR was rejected because it is blind to icons, the element class every grounding model handles worst (SeeClick: 30-52% on icons vs 56-78% on text) [6]; where OCR survives in modern stacks it is a text-disambiguation sidecar, not the targeting mechanism [8].

1. Anthropic, [*Computer use tool*](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool): platform docs (`computer_20251124`, `enable_zoom`, resolution guidance)
2. OpenAI, [*Computer-Using Agent*](https://openai.com/index/computer-using-agent/) and the [computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
3. *DiMo-GUI: Advancing Test-time Scaling in GUI Grounding via Modality-Aware Visual Reasoning*, EMNLP 2025. [arXiv:2507.00008](https://arxiv.org/abs/2507.00008)
4. Li et al., *ScreenSpot-Pro: GUI Grounding for Professional High-Resolution Computer Use*. [arXiv:2504.07981](https://arxiv.org/abs/2504.07981); [official harness](https://github.com/likaixin2000/ScreenSpot-Pro-GUI-Grounding)
5. Gou et al., *Navigating the Digital World as Humans Do: Universal Visual Grounding for GUI Agents* (UGround), ICLR 2025 Oral. [arXiv:2410.05243](https://arxiv.org/abs/2410.05243)
6. Cheng et al., *SeeClick: Harnessing GUI Grounding for Advanced Visual GUI Agents*, ACL 2024. [arXiv:2401.10935](https://arxiv.org/abs/2401.10935)
7. Qin et al., *UI-TARS: Pioneering Automated GUI Interaction with Native Agents*. [arXiv:2501.12326](https://arxiv.org/abs/2501.12326)
8. Agyeya et al., *Agent S2: A Compositional Generalist-Specialist Framework for Computer Use Agents* (Tesseract as a textual-grounding sidecar). [arXiv:2504.00906](https://arxiv.org/abs/2504.00906)

## License

[MIT](LICENSE)
