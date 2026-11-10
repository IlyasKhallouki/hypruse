<!-- mcp-name: io.github.IlyasKhallouki/hypruse -->

# hypruse

**Your AI agent works in apps on its own Hyprland workspaces while you keep working on yours.** hypruse is an [MCP](https://modelcontextprotocol.io) server, a set of shell verbs and an agent skill that give an agent the desktop you already have: the window tree, screenshots, clicks on controls by name, a mouse and a keyboard. It runs on stock [Hyprland](https://hypr.land) and on Omarchy, with no compositor plugin, no root, no portals, no ydotool daemon and no X11.

![An animation: you keep typing notes on workspace 1 and ask your agent to turn on Do Not Disturb and set the volume to 30%; the agent opens Settings on its own workspace 99, out of your sight, flips the toggle and moves the slider through the accessibility tree while your cursor and focus stay untouched, then reports back](https://raw.githubusercontent.com/IlyasKhallouki/hypruse/main/assets/demo.gif)

## Quick start

```sh
yay -S hypruse    # or: uv tool install hypruse
hypruse init      # registers it with Claude Code, Codex or Claude Desktop (asks first), then runs doctor
```

Then ask your agent: *"Use hypruse to list the windows on each of my workspaces."* It answers from Hyprland's own window tree, without a screenshot. When you want it to work out of your sight, ask it to launch an app on its own workspace and do the job there: that is [Background mode](#background-mode).

On Omarchy, everything hypruse needs is already installed; [On Omarchy](#on-omarchy) covers the one browser flag to set and a safe default for Omarchy's no-prompt agent launcher. If `doctor` reports a failure, [Troubleshooting](#troubleshooting) has the fix for each line. Before you hand an agent your seat, read the [Security model](#security-model).

[Den](https://github.com/GlacierHubAB/den) builds its desktop control on hypruse, and [hyprvibe](https://github.com/ChrisLAS/hyprvibe) and the [NUR](https://github.com/nix-community/nur-combined) package it for NixOS.

## Why

Computer use exists on macOS and Windows, and the vendors' Linux apps still ship **without** it: Claude Desktop's Linux docs say computer use is not available there, and ChatGPT's computer use covers macOS and Windows only. Anthropic's reference implementation is an X11 container. Other Wayland tools lean on ydotool and uinput, on GNOME-only portals, or on a compositor plugin built for one exact Hyprland version.

Meanwhile Hyprland already exposes everything an agent needs, better than any accessibility bridge: a complete IPC surface for state and window management, and first-class Wayland protocols for input. hypruse just wires them to MCP:

- **Semantic first.** `desktop` returns the real window/workspace tree (addresses, classes, titles, geometry) in one call. The agent switches workspaces and focuses windows the way you do (instantly, over IPC), not by squinting at pixels.
- **Vision when it matters.** Screenshots of a monitor, one window (with grim 1.5+ on Hyprland 0.54+, captured as itself even on a hidden workspace or under another window, though a hidden app may show how it last looked), or a zoomed region, with the geometry/scale metadata to map any pixel back to a clickable coordinate: a coarse-to-fine loop [grounded in the GUI-agents research](#research).
- **Native input.** Clicks and scrolls are spoken directly over the Wayland wire (`zwlr_virtual_pointer_v1`); typing goes through `wtype`'s virtual keyboard with a proper XKB keymap, unicode-safe on any layout.
- **Works beside you.** The agent can work in apps on its own workspaces while you keep working on yours. It presses controls through the accessibility tree and types through it or with Hyprland's `sendshortcut`, so your cursor, keyboard focus and workspace stay where you left them. Stock Hyprland, no compositor plugin: see [Background mode](#background-mode).

## Install

Requirements: Hyprland (both config managers: `hyprland.conf` and the Lua `hyprland.lua` that 0.56 introduced), `grim` (grim 1.5+ on Hyprland 0.54+ lets a window screenshot capture the window itself, and `hypruse doctor` says whether yours can), `wtype` (most Hyprland setups already have both), and, for the `uvx` route, [uv](https://docs.astral.sh/uv/). The accessibility tools (`ui`/`marks`/`click_ui`) use `busctl`, which ships with systemd, to find the accessibility bus, then talk to it over one connection of their own. Optional: `wl-clipboard` for the opt-in clipboard tool, `imagemagick` for numbered `marks` captures.

Arch Linux, from the [AUR](https://aur.archlinux.org/packages/hypruse):

```sh
yay -S hypruse        # or hypruse-git for main
```

Then let it set itself up and verify the environment:

```sh
hypruse init     # detects your MCP clients, registers (asks first), offers the agent skill, runs doctor
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

Any other MCP client: run `uvx hypruse` as a stdio server. hypruse is also in
the official [MCP registry](https://registry.modelcontextprotocol.io) as
`io.github.IlyasKhallouki/hypruse`, so clients that browse the registry can
install it from there.

**Read-only mode:** set `HYPRUSE_READONLY=1` in the server config to expose only the observation tools (`desktop`, `screenshot`, `zoom`, `ui`, `marks`, `binds`, `wait_for`). The agent can see and narrate but cannot click, type, or launch. A good first week.

### Claude Code plugin

The plugin holds the MCP server and the agent skill. Add this repository as a plugin marketplace, then install the plugin from it:

```sh
claude plugin marketplace add IlyasKhallouki/hypruse
claude plugin install hypruse@hypruse
```

Inside a session, `/plugin marketplace add IlyasKhallouki/hypruse` and `/plugin install hypruse@hypruse` do the same. The plugin starts its server from its own copy of the source with `uv run --locked`, so it runs exactly the code the plugin ships, with the dependency versions in the committed `uv.lock`. It needs [uv](https://docs.astral.sh/uv/) even when hypruse is installed another way, and the first start takes a few seconds while uv sets up a virtual environment inside the plugin's folder. If you registered hypruse earlier with `hypruse init` or `claude mcp add`, run `claude mcp remove -s user hypruse` first, or Claude Code lists every tool twice. For the same reason, remove the `~/.claude/skills/hypruse` link if `hypruse skill install` made one. After a release, `claude plugin update hypruse@hypruse` fetches the new version.

The plugin works in Claude Code on a Linux machine running Hyprland. Cowork starts the server only when the session runs on your own computer, and hypruse has not been tested there. Chat on claude.ai (on the web, in the desktop app and on mobile) loads the skill but never starts the server, so the plugin has nothing to drive there. macOS and Windows are not supported.

What the plugin runs and sends: it has no hooks, and its one program is the MCP server above. On first start, uv installs the dependency versions locked in `uv.lock` from PyPI into a virtual environment in the plugin's folder; hypruse itself runs from the plugin's own source. After that the server talks only to local sockets (Hyprland's IPC, the Wayland display and the accessibility bus), makes no network connections and has no telemetry. It runs the desktop tools listed under Requirements (`hyprctl`, `grim`, `wtype` and `busctl`, plus ImageMagick for `marks` and `wl-clipboard` for the opt-in clipboard tool), and `launch` runs the command the agent gives it through Hyprland's `exec`. Screenshots, window titles and control names go only to your own Claude session, as tool results; [Security model](#security-model) lists what stays on disk, and [PRIVACY.md](PRIVACY.md) says the same in short. The skill's `allowed-tools` let Claude run the read-only shell verbs (`hypruse doctor`, `desktop`, `screenshot`, `zoom`, `ui`, `marks`, `binds`, `wait_for` and `--help`) and its Read tool, which opens the screenshot files, without a prompt. The shell verbs need hypruse on `PATH`; the plugin's MCP tools and every verb that clicks, types or launches still ask unless you allow them.

Things to ask once it is installed:

- *"Use hypruse to list the windows on each of my workspaces."*
- *"Take a screenshot of my screen and tell me which apps are open."*
- *"Switch to the workspace where btop is running and tell me which process is using the most CPU."*

### Nix

The repository is a flake for x86_64-linux and aarch64-linux. To try it without installing anything:

```sh
nix run github:IlyasKhallouki/hypruse -- doctor
```

Install it before you run `hypruse init`. `init` registers the `hypruse` it finds on `PATH`, and a `nix run` copy is not on it, so `init` would fall back to `uvx`. For a quick install, run `nix profile add github:IlyasKhallouki/hypruse` (older Nix calls it `nix profile install`). In a flake:

```nix
{
  inputs.hypruse = {
    url = "github:IlyasKhallouki/hypruse";
    inputs.nixpkgs.follows = "nixpkgs";
  };
}
```

Then add `inputs.hypruse.packages.${pkgs.stdenv.hostPlatform.system}.default` to `environment.systemPackages` or `home.packages`, or add `inputs.hypruse.overlays.default` to `nixpkgs.overlays` and use `pkgs.hypruse`.

With Home Manager, import the module instead:

```nix
{
  imports = [ inputs.hypruse.homeModules.default ];
  programs.hypruse.enable = true;
  programs.hypruse.env.HYPRUSE_BACKGROUND = "1"; # optional, the server's environment
  programs.mcp.enable = true;
  programs.claude-code.enableMcpIntegration = true; # if Home Manager manages Claude Code
}
```

The module installs hypruse and adds it to `programs.mcp.servers`, the shared list of MCP servers that Home Manager's Claude Code module and other client modules read. On a Home Manager release without `programs.mcp`, the module only installs the package. If Home Manager does not manage your client, register the installed binary by hand: `claude mcp add -s user hypruse -- "$(command -v hypruse)"`.

The package wrapper appends grim, wtype, wl-clipboard, ImageMagick and systemd's `busctl` to the end of `PATH`, so the copies your session already has take precedence. It leaves out `hyprctl`, which has to match the running compositor, so hypruse uses the one your Hyprland session provides. If a client starts hypruse without that `PATH`, `hypruse.override { withHyprctl = true; }` appends nixpkgs' `hyprctl` as a last resort.

### On Omarchy

- grim, wtype, ImageMagick and wl-clipboard ship with Omarchy, and mise installs `uv` the first time you run it, so `yay -S hypruse` and `hypruse init` are all it takes. `init` registers the installed `hypruse` rather than `uvx`, because an MCP client started outside your shell may not find mise's `uvx` shim.
- Chromium, Omarchy's web apps and Obsidian build an accessibility tree only when asked. Without one, `click_ui`, `ui` and background mode fall back to screenshots in them. Add `--force-renderer-accessibility` on its own line to `~/.config/chromium-flags.conf` (Chromium and its web apps) and to `~/.config/obsidian/user-flags.conf` (Obsidian), then restart the app. `hypruse doctor` names any open window that still has no tree.
- Omarchy's agent launcher (`Super + Shift + Ctrl + A`, or `omarchy agent prompt`) starts agents in their no-approval modes, which removes the approval prompt the [Security model](#security-model) leans on. Start with `HYPRUSE_BACKGROUND=1`, which keeps hypruse's input on the agent's own workspaces, or `HYPRUSE_READONLY=1`, which lets it only look:

  ```sh
  claude mcp add -s user -e HYPRUSE_BACKGROUND=1 hypruse -- "$(command -v hypruse)"
  ```

- Omarchy 4 has no Waybar, so the [Waybar indicator](waybar/) does not apply. Bind the emergency stop to a free key in your Hyprland config instead, for example `bind = SUPER SHIFT, BackSpace, exec, hypruse stop`.
- Omarchy keeps a separate Claude Code setup per agent account, and a server added with `claude mcp add` reaches only the account it was added in. Run `hypruse init` again in each account you use.

### Troubleshooting

`hypruse doctor` checks each part on its own. What to do about each line it can print:

| doctor says | what to do |
|---|---|
| `[FAIL] dependencies missing: ...` | Install what it names, for example `sudo pacman -S grim wtype`. |
| `[FAIL] session no Hyprland instance found` | Run hypruse inside your Hyprland session. A client in a VM, a container or another user's session cannot reach your compositor. |
| `[FAIL] events` or `[FAIL] pointer` | The process found Hyprland but cannot reach its sockets or its Wayland display, which again means it is not running inside your session. |
| `[FAIL] screenshot` | Run `grim /tmp/hypruse-test.png` by hand. What it prints is the cause. |
| `[FAIL] accessibility bus unreachable` | Install `at-spi2-core` and log in again. Without it `ui`, `marks`, `click_ui` and background mode cannot work. |
| `[ok] accessibility ... none from <app>` | That app has no accessibility tree yet. Add `--force-renderer-accessibility` to the flags file the line names and restart the app (see [On Omarchy](#on-omarchy)). |
| `[ok] window shots grim has no -T` | Window screenshots crop the screen until you have grim 1.5+ on Hyprland 0.54+. Everything else works. |

Problems `doctor` cannot see:

- If the MCP server exits as soon as it starts on Arch, CachyOS or Omarchy, you are on hypruse 0.13.0 or older, which does not start with `python-mcp` 2. Arch has shipped mcp 2 since 2026-10-27. Upgrade to 0.13.1 or later.
- If the client says `uvx: command not found`, it does not see your shell's `PATH`. Register the installed binary instead: `claude mcp add -s user hypruse -- "$(command -v hypruse)"`, or run `hypruse init` again.
- If Claude Desktop shows no screenshots, set `HYPRUSE_SCREENSHOT_MODE=image` in its config, as below.

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

## Background mode

An agent can work in apps on workspaces you are not looking at while you keep working on your own. On stock Hyprland, with no compositor plugin, hypruse does not move your cursor, take your keyboard focus or switch your workspace to do it. An app can still bring one of its own windows forward in response to something the agent did; when that changes your focused window, the result says so.

It is on by default, in automatic mode. An action on the window you are in runs on your screen, as it always has. An action on any other window runs in the background:

- `click_ui` presses the control through its accessibility action. A text field gets the app's own focus instead (except in GTK 4, which does not allow it). When no button, box or field matches the name, `click_ui` also takes a named element of another role that has an action, such as a row in a web app's list. Some of those actions only focus the element (Qt WebEngine's rows offer nothing else). The result then says it focused the element and whether the app reports it focused. That does not open the row: in testing, Enter sent to a focused Qt WebEngine row did not open it either.
- `keyboard` with `window=` types into the window's focused text field (or its only one) through the accessibility interface. Otherwise it sends the keys to that window alone with Hyprland's `sendshortcut`, one key at a time on your keyboard layout. Newlines, tabs and backspaces are always sent as keys. Keys go to the control the app itself has focused, which is not necessarily the field text went into, so the agent focuses a field with `click_ui` before sending keys to it.
- `launch` with no workspace opens the app on one of the agent's own workspaces, out of your sight.
- The pointer is your real cursor, so it cannot work in the background. It is refused where its input would land on a window other than yours, and the refusal points the agent at `click_ui`.

What the background cannot do is refused with the reason and what to do instead. `background=false` on a call (`--foreground` on the CLI) does it on your screen.

### Settings

The guarantee overrides everything. Without it, a call's own `background` argument wins over the automatic rule.

| setting | what it does |
|---|---|
| `HYPRUSE_BACKGROUND=1` | The guarantee. The agent works only on windows on its own workspaces. The pointer, workspace switches, `focus_window`, `fullscreen`, `toggle_floating` with no target and `use_bind` are refused, `move_window` and `launch` take only the agent's workspaces (and none that is on screen), and `background=false` is refused too. |
| `background=true` or `false` | Per call, on `click_ui`, `keyboard`, `pointer`, `launch` and `sequence` (CLI: `--background`, `--foreground`). `true` forces the background and refuses what cannot be done there; `false` asks for your screen. On `sequence` it reaches the pointer, keyboard and `click_ui` steps, and `true` also refuses a `hypr` step that would switch your workspace or focus. |
| unset | Automatic, as above. |
| `HYPRUSE_BACKGROUND=0` | Off: actions run on your screen as in 0.12, unless a call passes `background=true`. (`ui` still lists editable text fields and adds its notes; see the changelog.) |

The guarantee bounds hypruse's own input. It cannot bound what a program does once `launch` starts it: the command runs as given, through a shell, so keep `launch` behind your client's approval prompt even with `HYPRUSE_BACKGROUND=1`.

The agent's workspaces are `HYPRUSE_AGENT_WORKSPACES`, 99 down to 90 by default, at the far end of the numbers so they stay clear of the ones you use. `A-B` counts in either direction, and a comma list (`99,98,50`) or named workspaces (`name:research`) work too. `HYPRUSE_AGENT_FILL` (default 1) is how many of the agent's apps share one workspace before the next one is used. With 1, every app gets a workspace to itself, which keeps hidden windows at their full size. A workspace in the range that holds one of your windows is skipped, so yours and the agent's never mix, and so is one you are looking at. When the whole range is full the next app goes where the agent has the fewest. A window an earlier session opened counts as the agent's when `HYPRUSE_MARK` tagged it. With `HYPRUSE_CONFINE=workspace:...`, apps open only on agent workspaces inside that scope, and on yours as before when there is none.

`launch` waits for its own window: one that opens on the workspace it asked for, or one of the launched program's own class. A window of another app that you open meanwhile is left alone; one of the same app can be taken for the launched one. An app that is already running (a browser, often) opens its new window from the running process, which ignores the workspace rule, so it appears on your screen and takes your focus until hypruse moves it; the result says so, and under the guarantee it carries a warning. Start a separate instance for the agent (for a browser, a separate profile) to avoid it.

### What you get back

Every background result says how it was done, whether your focused window and workspace were the same before and after, and, for a control that carries a value, what the accessibility tree read back:

```
pressed check box 'Agree' through its accessibility action (no pointer, the human's focus untouched) in yad; now: checked=true
typed 12 characters into the text field in yad through the accessibility interface (the human's focus untouched); the field now holds the typed text (12 characters)
sent ctrl+a to 0x5648592451c0 (yad) with sendshortcut, to the control the app has focused (the human's focus untouched)
```

A press on a check box or slider that the app ignored reads `no change seen`. A button press, a focused field or keys sent with `sendshortcut` carry no value to read back, so check their effect with `ui`. If your focused window changed during the action (because the app brought a window forward, or because you switched), the result ends with a `WARNING` naming the window that has focus now. Typed results never repeat the text, since the journal keeps every result line and typed text is only ever recorded as a length plus a digest. Screenshots are not used to confirm anything, because many apps stop drawing while hidden and a hidden window's pixels can be stale; `ui` shows its current state.

### Limits

- Only controls the accessibility tree exposes can be pressed in the background, and only with their primary action: a right or double click is refused there. Clicking an arbitrary point in a hidden window would need a compositor plugin or an input seat of its own, which this design avoids. Apps without a tree (terminals, canvas apps, Electron and Chrome without `--force-renderer-accessibility`) can still take keys.
- The accessibility tree tells an app's windows apart by title or size. When neither tells the target from the app's other windows (a second window of a single-instance app with the same title and size, say), or the app's accessibility connection belongs to another process than its window, hypruse cannot be sure which window a control belongs to: `click_ui` refuses, `keyboard` sends keys, which Hyprland aims at the window itself, and `ui` says its controls may be another window's.
- A hidden window's `ui` points are where its controls sit inside it, not on your screen, and the screen there shows your own windows. `ui` ends with a note saying so, because a `pointer` click at those points would land on whatever you are looking at.
- A GTK 3 app brings its window forward when a field takes focus over the accessibility interface. With Hyprland's `misc:focus_on_activate` on, that would switch you to it, so focusing a GTK 3 field is refused then. A per-window `focus_on_activate` rule cannot be seen; the after-check reports it.
- A character your keyboard layout has no key for (an emoji or CJK on most layouts) can only go in through the accessibility interface. A hidden GTK window does not report which of several text fields has its focus, so in a multi-field form such text is refused before any key is sent, and so is typing into a form with a password field among several, unless `allow_auth=true`.
- For each key, `sendshortcut` moves keyboard focus to the target and back inside one compositor step, so your own typing cannot mix in. Your app does see its focus leave and come back, which most apps ignore; a few close an open menu.
- `sendshortcut` is refused for XWayland windows, because Hyprland delivers the key to the focused window instead when both are XWayland.
- With several keyboard layouts, `sendshortcut` works only while you are on the first one. Hyprland finds each key on the layout in use but the app reads it on the first, so on another layout different characters would arrive, and hypruse refuses instead. Text through the accessibility interface does not depend on the layout.
- Web pages in the background need the browser bridge, which comes in a later release.
- While the session is locked, background actions are refused like all other input.

Every trust layer applies to the background paths: confinement checks the target window, the auth guard refuses a background press or key in an authentication dialog, and typing into a password field the accessibility tree shows needs `allow_auth=true` (an app with no tree cannot be checked, as in the foreground). You cannot see background work, so for anything unattended set `HYPRUSE_BACKGROUND=1`: input stays on the agent's workspaces, hypruse's own input never takes your screen (an app the agent launches can, and the result warns), the journal keeps every result with its method, and `HYPRUSE_MARK` tells you when the agent opens a window.

## How it works

```
agent (Claude Code, or any MCP client)
   │ stdio
   ▼
hypruse
   ├── hyprctl -j ········▶ desktop state: monitors, workspaces, windows, layers
   ├── hyprctl dispatch ··▶ focus / move / close / launch / movecursor,
   │                        and sendshortcut for keys to a hidden window
   ├── grim ··············▶ screenshots: monitor, window, region
   ├── D-Bus (AT-SPI) ····▶ accessibility tree: named controls, current
   │                        values, exact coords (ui / marks / click_ui),
   │                        and presses and text in the background
   ├── wtype ·············▶ keyboard (zwp_virtual_keyboard_v1, real XKB keymap)
   └── raw Wayland wire ··▶ click & scroll (zwlr_virtual_pointer_v1)
```

Optional binaries gate two more tools: `imagemagick` draws the numbered
overlay for `marks`, and `wl-clipboard` backs the opt-in `clipboard` tool.

Design decisions:

- **No ydotool / uinput.** That path needs a daemon, udev rules or root, and types US scancodes that break on other layouts. hypruse is just another Wayland client of your compositor, same standing as `wlrctl`.
- **No portals.** `xdg-desktop-portal-hyprland` does not implement the RemoteDesktop portal (InputCapture is capture, not injection), so anything built on libei/portals silently degrades on Hyprland. hypruse doesn't try.
- **Cursor positioning through the compositor's own cursor dispatcher** (global logical coordinates, exact on any monitor layout), with only button/axis events on the virtual pointer, sidestepping the known multi-monitor bugs of absolute virtual-pointer motion ([hyprwm/Hyprland#6749](https://github.com/hyprwm/Hyprland/issues/6749)).

## Tools

| tool | what it does |
|---|---|
| `desktop` | One-call semantic snapshot: monitors, workspaces, windows (address/class/title/geometry), active window, cursor, and layer surfaces (launchers, bars, notification popups) with a best-effort kind and geometry. A listed layer is one the compositor tracks, not one you can see: transparent and dormant surfaces are reported too, so screenshot when visibility matters |
| `screenshot` | Focused monitor, one window by address (with grim 1.5+ on Hyprland 0.54+, even hidden or covered; metadata `visible` says whether the compositor draws it, `source` whether the pixels are the window's own or a screen crop, and a hidden capture may be stale), or `x,y,WxH` region; returns image + coordinate-mapping metadata; fast JPEG by default (`lossless=true` for PNG); `stable=true` waits for the frame to settle |
| `zoom` | Native-resolution re-capture around an estimated point (optionally clamped to a window): the precision step before clicking small controls, same metadata contract |
| `ui` | Read a window's accessibility tree (AT-SPI, GTK/Qt apps that expose one) and return clickable elements by name with exact global coordinates, no screenshot; reports current values too (typed text, slider position, checkbox state); falls back to vision when an app exposes nothing |
| `marks` | Set-of-Marks capture: the window screenshot with every accessible control drawn as a numbered mark, plus a JSON legend (role, name, current value, exact click point per number); needs ImageMagick for the drawing, degrades to the legend alone without it |
| `click_ui` | Click a control by accessible NAME or by a `marks` number in one call: the coordinate comes from the tree. In the window you are in, the click goes through the real pointer (visible, same safety guarantees); in any other window, the control's accessibility action presses it in the background. An ambiguous name returns the candidates instead of guessing |
| `pointer` | move / click / drag / scroll (discrete wheel notches) in global coordinates. Your real cursor, so by default it is refused where the input would land on a window other than yours |
| `keyboard` | Type literal text (unicode-safe) or press app-level combos (`ctrl+shift+t`, `esc`, `F5`); an optional `window` address says where the keys go: the window you are in is focused and typed into, any other window gets them in the background. Compositor binds (`super+...`) go through `use_bind`, not here |
| `hypr` | Switch workspace, focus/move/close windows, fullscreen, floating (pure IPC, milliseconds; `close_window` also waits up to 1s for the destroy event, so its result and `then=` observation reflect the close instead of racing it) |
| `launch` | Start an app, silently on the next of the agent's own workspaces unless you name one (or pass `background=false` for yours), block on its actual `openwindow` event, return its address; detects single-instance apps (browsers) whose window ignores exec rules and moves it to the requested workspace |
| `binds` | The user's own keybinds, decoded (`SUPER+Q`, action, description); the agent runs one with `use_bind` |
| `use_bind` | Execute a keybind by combo (`SUPER+F`), running its bound action, so the agent drives the owner's own launchers and shortcuts. Not available for binds in a Lua `hyprland.lua`: those are anonymous closures Hyprland exposes no way to call, so `binds` shows them as action `lua` and `use_bind` says so instead of failing cryptically |
| `sequence` | Run an ordered list of actions (pointer/keyboard/click_ui/hypr/wait_for) in one call; stops the moment the desktop changes in a way the current step did not expect, so a click/type/enter micro-sequence costs one round-trip instead of several |
| `wait_for` | Block on real compositor events (window open/close, workspace change, title change, layer surfaces appearing/closing, urgency, screen sharing on/off) with a match filter and timeout; a filtered wait whose condition already holds (window already gone, workspace already active, layer already mapped) answers instantly with `already: true` instead of missing an event that fired before it subscribed. Replaces sleep-and-hope in multi-step automations |
| `clipboard` | Read or write the text clipboard via `wl-clipboard`; opt-in, exists only with `HYPRUSE_CLIPBOARD=1` in the server env |

The acting tools (`pointer`, `keyboard`, `click_ui`, `hypr`, `use_bind`, `sequence`) take an optional `then` argument that appends the result to the same call, so the agent sees the effect without a second round-trip: `then='desktop'` adds a fresh semantic snapshot (~20 ms, cheap, best for window/focus changes), `then='screenshot'` a stable capture (best for visual changes), `then='ui'` the acted-on window's controls with their current values (a few hundred exact tokens, best after typing or toggling; `click_ui` reads the window it clicked even if the click handed focus to a dialog, the others read the focused window), `then='none'` nothing (the default everywhere except `sequence`, which defaults to `'desktop'`).

Every tool is also a shell verb (`hypruse desktop`, `hypruse click_ui Save --window 0x...`) for agents that run commands instead of MCP, with a skill that teaches them: see [Shell verbs and the agent skill](#shell-verbs-and-the-agent-skill).

## Features

The tools group into five capabilities, ordered most-reliable-and-cheapest first. An agent that reaches for them in this order is both faster and more accurate, and many tasks never need a screenshot at all.

### 1. Semantic desktop control (start here)

`desktop` returns the entire window and workspace tree in one call: every window's address, class, title, and geometry, the active window, the cursor, and any layer surfaces the compositor is tracking (launchers, bars, notification popups). `hypr` and `launch` then act on it over IPC in milliseconds: switch workspace, focus/move/close/fullscreen/float a window by address, or start an app.

**Use it well:** never take a screenshot to find or arrange windows. Read `desktop`, act on the address you want. `launch` blocks on the real `openwindow` event and hands back the new window's address, so there is nothing to poll or guess; it also relocates single-instance apps (browsers) that ignore workspace rules.

### 2. Click controls by name, no pixels

When an app exposes an accessibility tree (most GTK and Qt apps), you can target controls by name instead of by pixel. `ui` lists every control with its exact global coordinate, and reports the current value of the controls that carry one: the text in a field, a slider's percentage, a checkbox's state. `click_ui` resolves a name and clicks it in one call, through the real cursor (so the beacon and every safety guarantee still apply), or, in a window you are not in, through the control's own accessibility action without touching your cursor. `marks` draws numbered marks over a screenshot with a legend, for when you want to see the options first and then `click_ui(mark=N)`.

**Use it well:** reach for `click_ui name="Save"` before estimating any pixel, since it is exact and spends no image. Read a form's state with `ui` (did the box actually tick?) instead of screenshotting it. An ambiguous name returns the candidates rather than guessing. When an app exposes no tree (terminals, canvas apps, Electron/Chrome without `--force-renderer-accessibility`) the tool says so, and you fall back to vision.

### 3. Vision when it matters: the zoom loop

For everything the accessibility tree cannot name, `screenshot` (monitor, window, or region) and `zoom` (a native-resolution re-capture around a point) carry a strict coordinate contract, `global = geometry + pixel / scale`, that stays exact on every monitor and fractional scale.

**Use it well:** don't guess a small control from a full-screen image. Work coarse-to-fine: screenshot the window, estimate the target, `zoom` there, re-estimate on the sharp crop, then click. This two-step loop is the [research-backed](#research) way to hit small targets.

### 4. Fewer round-trips: the latency lever

For an agent the model calls dominate task latency, not the desktop, so the real speedups are structural. `sequence` runs an ordered micro-plan (click, type, press enter, wait) in a single call, stopping the moment the desktop changes *structurally* in a way a step did not intend (a window opening, closing, or moving, an unexpected workspace switch, or a seat-taking launcher or on-screen keyboard; it deliberately ignores bare focus changes and notification popups). `then='desktop' | 'screenshot' | 'ui'` fuses a fresh view of the result into the acting call itself. `wait_for` blocks on real compositor events (a window or launcher opening, a title changing, a workspace switch, an urgency hint, screen-sharing starting) instead of sleeping and hoping.

**Use it well:** collapse a known click/type/enter flow into one `sequence`. After typing into a form, add `then='ui'` to read the effect back in a few hundred exact tokens instead of a screenshot. After a launch or a shortcut that opens something, `wait_for` the event rather than sleeping.

### 5. Safe delegation: trust layers

hypruse hands an agent your real seat, so it ships the controls to bound what that agent can do. Beyond the always-on approval prompts and the Waybar activity beacon, opt-in env flags narrow what an agent can touch: `HYPRUSE_READONLY` exposes only the observation tools; `HYPRUSE_CONFINE` restricts input to the windows the agent launched, or a class/workspace allowlist; `HYPRUSE_AUTH_GUARD` (on by default) refuses to drive authentication dialogs; `HYPRUSE_STRICT` refuses to act if you took the seat back; `HYPRUSE_MARK` tags agent-owned windows and announces when the agent opens a window or captures the screen. Two more record rather than restrict: `HYPRUSE_JOURNAL` writes an auditable NDJSON line per tool call, refusals included, and `HYPRUSE_DRYRUN` runs every check and delivers nothing, so you can watch an agent plan the work before it touches your desktop.

**Use it well:** run read-only for the first week. When you trust a workflow, allowlist its tools and, if you want to walk away, confine the agent to a scope so it cannot click or type into your password manager on another workspace. Keep a panic bind handy (`hypruse stop`, or `pkill -f hypruse`). The [Security model](#security-model) has the full story.

## Shell verbs and the agent skill

Not every agent speaks MCP, and the ones that do increasingly prefer a command line: a tool list costs a few thousand tokens per session before the first call, a shell verb costs nothing until it runs. So the same fifteen tools are verbs, going through the same functions the MCP server registers, with the same trust guards, journal and activity beacon:

```sh
hypruse desktop                                  # one line per monitor, workspace, window, layer
hypruse launch --workspace 2 -- firefox --new-window https://example.org
hypruse ui --window 0x55a4479a1200 --name Save   # [0] push button "Save" @1204,88
hypruse click_ui Save --window 0x55a4479a1200 --then ui
hypruse screenshot --window 0x55a4479a1200       # a file path, then {"geometry":[x,y,w,h],"scale":1.0,...}
hypruse zoom 1180 840 --window 0x55a4479a1200
hypruse keyboard type "hello" --window 0x55a4479a1200
hypruse wait_for title_change --match Inbox --timeout 10
hypruse sequence '[{"op":"click_ui","name":"Search","window":"0x55a4479a1200"},{"op":"keyboard","action":"type","text":"btop","window":"0x55a4479a1200"},{"op":"keyboard","action":"key","keys":"enter","window":"0x55a4479a1200"}]'
```

The contract is built for a program reading the output: the verbs are the tool names, a tool's `action` is a positional sub-verb (`pointer click 800 60`, `hypr workspace 3`, `clipboard read`), output is compact plain text (one line per fact, `--json` for the raw result as one line), a capture prints its file path and coordinate metadata rather than bytes, and errors are one line on stderr with a meaningful exit code: 0 delivered, 1 error, 2 usage, 3 refused by a trust layer (or by read-only mode), 4 ran but found nothing to act on (a `wait_for` timeout, no accessibility tree, an ambiguous name). `hypruse --help` lists every verb and `hypruse <verb> --help` its flags; `--dry-run` rehearses an acting verb. A verb starts in a few hundred milliseconds: the MCP stack is never imported on that path.

Between one-shot processes hypruse keeps the little state a verb would otherwise lose in `$XDG_RUNTIME_DIR/hypruse/cli-state.json`, keyed by the compositor instance: the `marks` numbering (so `click_ui --mark N` works), the `launched` confinement set, and the `HYPRUSE_STRICT` seat baseline, which is the one that would otherwise fail open.

The skill that teaches an agent the verbs, the desktop-first workflow and the safety rules ships inside the package. Install it into the skill directories of the agents on your machine (Claude Code, Codex, Pi, Hermes, OpenClaw, OpenCode, Gemini, Antigravity, Cursor, Copilot), or via the [skills](https://skills.sh) CLI:

```sh
hypruse skill install                # ~/.agents/skills/hypruse, linked into each agent that is installed
hypruse skill install --agent codex  # one agent, created if absent
npx skills add IlyasKhallouki/hypruse -g
```

`hypruse init` offers the same install. The skill pre-approves only the observation verbs, `doctor`, `--help` and reading the capture files; acting verbs stay behind your agent's own approval prompt, which is the boundary the [Security model](#security-model) leans on.

## Security model

Read this section before installing. **hypruse hands an agent your mouse, your keyboard, your screen contents, and an app launcher.** The layers that keep that sane:

1. **Approval:** MCP clients gate tool calls. In Claude Code, allowlist the read-only tools (`desktop`, `screenshot`) and leave `pointer`/`keyboard`/`hypr`/`launch` on ask-first until you trust a workflow.
2. **Visibility:** the server maintains an activity beacon (`$XDG_RUNTIME_DIR/hypruse/state.json`); the shipped [Waybar module](waybar/) is invisible when idle and shows a robot indicator while an agent has hands on your desktop.
3. **Interruption:** click the indicator, or bind a panic key. The portable form works for every install: `bind = SUPER SHIFT, BackSpace, exec, pkill -f hypruse`. If `hypruse` is on your PATH (the AUR or a pipx install), `bind = SUPER SHIFT, BackSpace, exec, hypruse stop` is nicer: it signals the server to shut down gracefully, releasing any held pointer button and clearing the beacon. For a `uvx` install use `exec, uvx hypruse stop`; for a source checkout, `exec, uv run --directory /path/to/hypruse hypruse stop`. Killing it mid-action is safe either way: button press/release pairs never span tool calls, and even a long drag's held button is released on the way out.
4. **The seat is shared.** There is one cursor and one keyboard focus, and Hyprland's focus-follows-mouse means a cursor move alone can retarget keystrokes. Don't type while an agent is driving your window; watch the indicator. [Background mode](#background-mode) keeps the agent off your seat for every window you are not in.
5. **Scope:** stdio only (no network listener), nothing persisted except the beacon and the capped screenshot cache in `$XDG_RUNTIME_DIR` (tmpfs, newest 20) and, if you turn it on, the [action journal](#the-record-journal-dry-run-replay) on disk under `$XDG_STATE_HOME` (rotated, one generation, and text-redacted by default). No clipboard access unless you opt in: `HYPRUSE_CLIPBOARD=1` registers a `clipboard` tool (never in read-only mode); clipboards hold passwords, so leave it off unless a workflow needs it. A screenshot sees everything visible, and a window screenshot also reaches windows on hidden workspaces, in scratchpads and under other windows: treat an agent session like sharing your whole desktop. Read-only mode and `HYPRUSE_CONFINE` limit what the agent can do, not what it can see. Window screenshots are refused while the session is locked.
6. **What the agent reads is untrusted.** Window titles, accessibility names and values, and clipboard text flow verbatim into the agent's context, and any web page, filename, or document can put instructions there (prompt injection). hypruse cannot sanitize meaning, so the approval layer is the backstop: keep consequential tools (`launch`, `keyboard`, `clipboard`) on ask-first when the agent will look at untrusted windows, and treat "the screen told me to" as attacker input when reviewing an approval prompt.
7. **Input never lands where it silently would not work.** Three always-on checks (no env flag) refuse or annotate rather than report a phantom success: a click aimed under a launcher or on-screen keyboard layer surface (which sits above windows and would swallow it), typing while a launcher holds the keyboard grab, and any input while the session is **locked**. A lock screen is an `ext-session-lock` client, invisible to the window and layer lists, so hypruse asks Hyprland itself (`hyprctl locked`), which covers hyprlock, swaylock, Omarchy's built-in lock, and a locker that crashed and left the session locked. While locked, `pointer` and a window-less `keyboard` refuse unless `allow_auth=true` says a human wants the agent driving the unlock prompt (never under `HYPRUSE_CONFINE`); `keyboard` with `window=` and `click_ui` always refuse, since input aimed at a window cannot reach it behind the lock. These are truthfulness aids, not a sandbox: the lock check fails open on an unreadable system state, so they harden the common case without being a boundary you can lean on. (While `HYPRUSE_AUTH_GUARD` is on, an unreadable layer list makes `pointer`, `click_ui` and `keyboard` refuse, since an authentication prompt cannot be ruled out.)

### Optional confinement

Seven opt-in env flags. The first five narrow what an agent can touch, and each fails toward *less* action; the last two record and rehearse rather than restrict. All compose with the layers above:

- **`HYPRUSE_CONFINE`** restricts input to a scope of windows: `launched` (only windows hypruse itself opened this session), `class:firefox,kitty`, or `workspace:3,special:notes`. Keyboard, `click_ui`, and `hypr` window ops are refused outside the scope; a `pointer` click is refused when any window under the point is out of scope (Hyprland's window list is not z-ordered, so hypruse fails closed rather than guess which window is on top). This is what lets you leave an agent working while your password manager sits on another workspace, out of reach of its clicks and keys (not of its screenshots). `use_bind` is refused outright while confinement is set, because a keybind runs an arbitrary compositor action that cannot be scoped to a window.
- **`HYPRUSE_BACKGROUND=1`** is the [background guarantee](#background-mode): input is confined to the windows on the agent's own workspaces (and to `HYPRUSE_CONFINE`'s scope too, when both are set), and any hypruse action that would take your cursor, focus or workspace is refused. It cannot bound what a command started with `launch` does.
- **`HYPRUSE_AUTH_GUARD`** (default **on**) refuses to click or type into a system authentication dialog (polkit agents, whether a window like hyprpolkitagent or a prompt the shell draws as a layer, as Omarchy 4 and end-4's dots do; the GNOME keyring prompt; Hyprland's own dialogs, which are its permission prompt and also its app-not-responding and safe-mode notices, since all three share one window class), so a manipulated agent cannot approve a privilege escalation. `click_ui` also refuses when such a dialog floats over the control it was asked to click. When the layer list cannot be read, it refuses rather than guess. A prompt a shell opens as an ordinary window of its own class, as DankMaterialShell does, is not recognised. Set `HYPRUSE_AUTH_GUARD=strict` to also refuse typing into a password field inside an ordinary window (a browser login), detected via the accessibility tree. A per-call `allow_auth=true` on `pointer`/`keyboard`/`click_ui` overrides it, and because it changes the tool's arguments the override surfaces distinctly in the approval prompt. `HYPRUSE_AUTH_GUARD=0` disables it.
- **`HYPRUSE_STRICT`** refuses to act when the cursor or focused window moved since hypruse's last action (the human, or a popup, took the seat): the agent must re-read `desktop`/`screenshot` and retry, so it never types into a window you just switched to.
- **`HYPRUSE_MARK`** makes the agent's presence legible on the desktop: it tags every window the agent opens `hypruse-owned` and flashes an on-screen notice when the agent opens a window or captures the screen. It also installs a `border_color` window rule on that tag so owned windows get a colored outline, but whether a *runtime* rule renders depends on your Hyprland version and config precedence (on some setups it does not take effect); when it cannot be installed at all, hypruse says so on stderr rather than leaving you with a marking layer that is quietly not running. For a guaranteed outline, add the rule to your own config, which hypruse's tagging then matches: `windowrule = border_color rgb(ff5555), tag hypruse-owned` in `hyprland.conf` (older Hyprland: `tag:hypruse-owned`), or `hl.window_rule({ match = { tag = "hypruse-owned" }, border_color = "rgb(ff5555)" })` in `hyprland.lua`.
- **`HYPRUSE_JOURNAL`** records what the agent did: one NDJSON line per tool call in `$XDG_STATE_HOME/hypruse/journal.ndjson` (`HYPRUSE_JOURNAL=1`), or a path of your own. Read it with `hypruse journal`, re-run it with `hypruse replay`. See [The record](#the-record-journal-dry-run-replay) below.
- **`HYPRUSE_DRYRUN`** is a rehearsal: every argument check and every guard above runs, then the call reports what it *would* have done and delivers nothing.

### The record: journal, dry run, replay

The flags above decide what an agent may do in the moment and then forget it happened. `HYPRUSE_JOURNAL` is the memory. One JSON object per line, so `tail -f`, `grep`, and `jq` all work on a live file:

```json
{"v":1,"seq":7,"ts":"2026-08-02T09:12:13.456Z","kind":"act","tool":"pointer",
 "args":{"action":"click","x":800,"y":60},"outcome":"ok","ms":37,"result":"click ok"}
```

`kind` splits the two questions people actually ask: `act` is input delivered to your desktop, `observe` is the agent looking, which is what a privacy audit wants (when the screen was captured, when the clipboard was read). Observation *results* are never recorded, only that they happened, so the journal never becomes a second copy of everything the agent saw. **Refusals are recorded too**, with the guard's own message: it is the only place the history of your trust layers doing their job exists.

Typed and copied text is recorded as a length plus a short digest, not as text, because keystrokes are passwords. `HYPRUSE_JOURNAL_TEXT=1` keeps it verbatim, which you need only to replay typing. Note what a digest is and is not: it proves two entries typed the same thing and it will not hand a reader your password, but it is an unsalted SHA-256 prefix next to an exact character count, so a four-digit PIN is trivially recovered from it. Treat the journal as sensitive either way. It is written 0600 in a 0700 directory, and rotated at `HYPRUSE_JOURNAL_MAX_BYTES` (8 MiB, one previous generation kept as `.1`; set 0 to never rotate).

The journal is a recorder, not a guard: if it cannot be written the action still happens and hypruse warns once on stderr, because failing your desktop over a log line is the wrong trade. What it does *not* record is a `then=` observation as its own entry: the acting call that carried it is recorded, `then=screenshot` and all, but the capture does not get a second line of its own.

Read it back with `hypruse journal` (`--acts` for actions only, `--refused` for what the guards stopped, `-n N` to tail, `-v` to include each result):

```
    1  09:12:10  session start pid 4211 0.10.0 confine=launched auth_guard=1 strict=True
   14  09:12:13  act  pointer    action=click x=800 y=60
   16  09:12:15  act  keyboard   action=type text=<11 chars>
   19  09:12:19  act  hypr       action=close_window target=0x5f2a10
                  REFUSED  TrustError: 0x5f2a10 (Signal) is outside the agent's confinement scope

312 actions (0 dry), 604 observations, 1 refused by a trust layer, 0 errors
```

`HYPRUSE_DRYRUN=1` turns the same session into a rehearsal. Every acting tool validates its arguments and runs every trust guard, then reports the plan instead of executing it, so a dry run refuses exactly what a real run would:

```
DRY RUN, nothing was delivered: would click push button 'Send' at (1204, 880) in signal
```

Nothing reaches the desktop: not the click, not the keystroke, not even the window focus that normally precedes typing. Enforced twice, once at each tool and once at the input path itself, so a code path nobody thought of fails loudly rather than quietly acting during a simulation. The agent is told dry run is on, so it reports a plan instead of retrying an action that "did not work". The scope is the agent's actions, not the server's own startup: `HYPRUSE_MARK`, if you set it, still installs its window rule when the server starts.

`hypruse replay <journal>` re-issues a journal's actions through the same tool functions, so the same guards apply to the replay. It prints the plan and stops there unless you pass `--execute`. Before it takes the seat it refuses outright, rather than failing halfway and leaving your desktop part-way through someone else's plan, when: an action was recorded by a newer hypruse, a recorded window no longer exists (`--skip-missing` runs the rest), typed text was recorded as a digest, the entry is a `click_ui(mark=N)` whose numbering died with the session that drew it, the entry is a clipboard write and `HYPRUSE_CLIPBOARD` is not set, or `HYPRUSE_READONLY` or `HYPRUSE_DRYRUN` is set. It paces itself from the recorded timing, capped by `--max-gap` and scaled by `--speed`, and its own actions are recorded and marked, so replaying the same file again runs the original plan rather than the plan plus the replay of it.

The honest limit is window addresses: they are heap pointers, so yesterday's journal mostly names windows that are gone, and an address can even be reused by a different window later, which no pre-flight can catch. Replay is for re-running a flow on a desktop that still looks like the one recorded.

Dry run and replay compose in the obvious direction: let the agent work with `HYPRUSE_DRYRUN=1`, read the journal, and replay it with `--execute` once the plan is one you like.

## Performance

Measured on a live session (Hyprland 0.55, 1080p, 20 windows): `desktop`
~20 ms (one batched `hyprctl` call), workspace/window dispatch ~10-20 ms, full-monitor screenshot ~65 ms
(fast JPEG default; ~800 ms if you ask for lossless PNG, grim's zlib
path dominates), region/zoom captures well under that. If tool
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

Captures default to JPEG q90: on a 1080p frame that is roughly 12x faster to encode than PNG (grim's zlib path dominates capture time, measured ~65 ms vs ~800 ms) and 3-4x smaller, while full-res q90 reads UI text well. Pass `lossless=true` for exact pixels (PNG). In image mode, captures also fit the host's result-size limit (Claude Desktop caps tool results at 1 MB) by degrading quality before resolution, since grim's downscale filter is slower than a full-res capture, and cap the long edge at `HYPRUSE_MAX_IMAGE_EDGE` pixels (default 1568) so the host never downscales the image under the model. The applied scale is folded into the returned metadata, so coordinate mapping stays exact; tune with `HYPRUSE_MAX_IMAGE_BYTES`, or pass `scale` for a deliberate zoom-out.

By default the screenshot tool writes the image under `$XDG_RUNTIME_DIR/hypruse/` and returns its path; MCP hosts with a file reader (Claude Code's `Read`) render it natively. This default exists because some hosts (including Claude Code 2.1.x) serialize inline MCP image blocks to base64 text the model cannot see. `HYPRUSE_SCREENSHOT_MODE=image` switches to inline image content blocks for hosts that render them correctly.

## Development

```sh
uv sync --group dev
uv run pytest            # unit tests, no compositor needed
uv run pytest -m e2e --override-ini addopts=   # live seat-safe checks
uv run pytest -m background --override-ini addopts=   # live: drives yad forms on the agent's workspaces
uv run python scripts/e2e_input.py             # supervised: takes the seat ~10s
```

The background suite opens yad forms on the agent's workspaces, drives them in the background, and checks after every step that your active window, workspace and cursor are exactly as they were, so leave the mouse and keyboard alone while it runs. It also checks Hyprland's own events, which hold even while you work: no window it opened ever takes focus, and no agent workspace is switched to.

The input e2e is deliberately manual: it borrows your cursor and keyboard, counts down, proves click/scroll/type delivery by reading the target terminal's screen back over kitty remote control, and restores your focus.

`scripts/vm/` builds an Omarchy test VM, and its scripts run only when you start them yourself: `fetch` downloads the Omarchy ISO from iso.omarchy.org and its signing key from GitHub, `shell --bootstrap` serves a setup script to the VM on 127.0.0.1 until ssh works, and `check` installs hypruse from the AUR inside the VM. [scripts/vm/README.md](scripts/vm/README.md) has the details. The plugin never runs anything under `scripts/`.

## Roadmap

Grounded in measured hot-path latencies and the finding that LLM calls are 76 to 96% of computer-use task latency ([OSWorld-Human](https://arxiv.org/abs/2506.16042)), so cutting round-trips beats shaving milliseconds. The round-trip work that framing motivated has largely shipped: `sequence`, act-and-observe `then=` (including `then='ui'`), and the accessibility-tree tools (`ui`, `marks`, `click_ui`) that target controls by name with no screenshot. What remains:

**Faster**

- In-process wlr-screencopy over the raw wire (as input already works): drop grim's fork floor from small captures and add damage-tracked wait-for-stable that returns the instant the screen settles.

**Fewer round-trips**

- Semantic screen diff: after an action, return only what changed (window topology from the event stream, or a bounded changed-region crop) instead of a full frame. The socket2 event expansion behind `wait_for` already tracks most of the topology; the missing piece is folding it into a post-action delta.

**Deeper reading**

- Wider accessibility coverage: close the gaps the `ui` and `marks` tools hit today, chiefly GTK's newer combo boxes that publish neither their text nor selection (so a rendered dropdown value still needs a screenshot), plus AT-SPI value-change events so `then='ui'` can report a control settling without a poll.
- Notifications: read recent desktop-notification content and history, not just wait for the popup to appear (`wait_for` already matches `layer_open` on the notification namespace).

**Background**

- Web pages in the background, through the browser bridge (an extension and a native messaging host).

**Trust**

- `record` tool: a scoped GIF or mp4 of the agent driving the desktop, via wf-recorder (a wlroots-family binary like grim), a visual companion to the [journal](#the-record-journal-dry-run-replay).

**Platform**

- sway / niri support: the wire client already speaks the wlr protocols; what remains is an IPC layer alongside `hyprctl.py` (contributions welcome).
- Headless end-to-end tests in CI: needs a QEMU virtio-gpu VM, since Hyprland's aquamarine backend requires a real GPU render node that hosted runners lack.

**Measurement**

- Zoom-loop precision benchmark: measure click accuracy of the coarse-to-fine loop against known targets.
- End-to-end task-success benchmark on Hyprland (OSWorld-style): the deferred bigger sibling of the zoom-loop microbenchmark, scoring full multi-step tasks through the real MCP surface so the a11y-versus-vision and round-trip work is judged on task completion, not latency alone.

## Related projects

| project | approach | on Hyprland |
|---|---|---|
| computer-use-linux | AT-SPI + portals, ydotool fallback | GNOME-first, and the RemoteDesktop portal it prefers is not implemented by xdg-desktop-portal-hyprland; since 2026-10-31 it has an opt-in Hyprland background mode (`COMPUTER_USE_LINUX_HYPRLAND_BACKGROUND=1`) whose typing refuses the whole call on a main keymap other than US ([#350](https://github.com/agent-sh/computer-use-linux/pull/350)) |
| agent-desktop (Vercel) | computer use CLI for agents; AT-SPI on Linux | as of 2026-11-08, keys, display layout and window control refuse on Hyprland because the Linux adapter measures only Mutter's interfaces ([#386](https://github.com/vercel-labs/agent-desktop/issues/386)) |
| hyprmcp | hyprctl wrapper | window management only; no screenshots or input |
| wayland-mcp | evemu input, VLM analysis | requires elevated setup for input; no Hyprland semantics |
| Anthropic computer-use-demo | X11 + xdotool in Docker | a sandboxed reference environment rather than a live desktop |
| Cua (cua-driver) | cross-platform driver; an optional Hyprland compositor plugin gives the agent its own input seats | the plugin must be built for the exact installed Hyprland version, its README calls it experimental and unreleased, and as of 2026-11-08 it does not yet support Hyprland 0.57's input route ([#6232](https://github.com/trycua/cua/issues/6232)) |

## Research

hypruse ships no OCR engine; its universal precision mechanism is the coarse-to-fine zoom loop: screenshot a window, re-capture the target region at native resolution, click through the exact coordinate mapping. That choice follows what the GUI-agents field converged on. Anthropic's computer use grounds clicks from raw pixels and ships a `zoom` action as the documented fix for small text; its troubleshooting guidance for near-miss clicks prescribes zooming and region cropping, never OCR [1]. OpenAI's CUA is likewise pure pixel grounding under resolution discipline, with no OCR layer at all [2]. Zoom is also the measured lever: training-free iterative zooming roughly doubles high-resolution grounding accuracy (OS-Atlas-7B, 18.9 → 49.7 on ScreenSpot-Pro) [3], and the benchmark's official harness implements a dozen grounding-model adapters plus four zoom/crop strategies, but zero OCR baselines [4]. Vision-only agents match or beat agents that additionally consume HTML or accessibility trees [5], substrates Wayland doesn't guarantee anyway, and state-of-the-art native agents run from screenshots alone [7]. OCR was rejected because it is blind to icons, the element class every grounding model handles worst (SeeClick: 30-52% on icons vs 56-78% on text) [6]; where OCR survives in modern stacks it is a text-disambiguation sidecar, not the targeting mechanism [8].

Where an app exposes an accessibility tree, hypruse also reads it (the `ui` tool, AT-SPI over D-Bus) to target controls by name with exact coordinates and no screenshot. This follows the strongest Linux precedent: OSWorld, the standard computer-use benchmark, exposes the desktop accessibility tree (obtained on Ubuntu through AT-SPI) as a first-class observation alongside screenshots, and reports the accessibility-tree-plus-screenshot combination as its best configuration [9]. The tree gives exact element identity and coordinates that current models cannot reliably infer from pixels: Agent-S tags each element with an id because MLLMs "lack an internal coordinate system," lifting OSWorld success from 11.2 to 20.6 percent [10]; Microsoft's UFO drives Windows through the UI Automation tree and fuses it with vision [11]; browser agents read the accessibility tree, which Playwright serializes to compact YAML, rather than pixels for the same reason [12]. hypruse's Wayland-specific trick is coordinate mapping: AT-SPI screen coordinates are unreliable on Wayland because an app does not know its global position, so hypruse uses window-relative extents plus the window position it already has from hyprctl. Coverage is uneven by nature: canvas, games, terminals, and Electron/Chrome without a flag expose little or nothing [13]. Testing this implementation against real GTK and Qt apps sharpened that in both directions: a typed entry reads back its exact contents, and sliders and toggles report their position and state, but GTK's newer combo boxes publish neither their text nor a selection, so reading a rendered dropdown value still needs a screenshot. Toolkits also describe widgets they never laid out (zero-height scroll arrows, unrendered tab pages reporting origins in the millions), which hypruse rejects against the window rect hyprctl knows authoritatively. A strong visual grounder can substitute for the tree entirely [5], so the accessibility tree complements the zoom loop rather than replacing it, and vision stays the guaranteed fallback.

1. Anthropic, [*Computer use tool*](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool): platform docs (`computer_20251124`, `enable_zoom`, resolution guidance)
2. OpenAI, [*Computer-Using Agent*](https://openai.com/index/computer-using-agent/) and the [computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
3. *DiMo-GUI: Advancing Test-time Scaling in GUI Grounding via Modality-Aware Visual Reasoning*, EMNLP 2025. [arXiv:2507.00008](https://arxiv.org/abs/2507.00008)
4. Li et al., *ScreenSpot-Pro: GUI Grounding for Professional High-Resolution Computer Use*. [arXiv:2504.07981](https://arxiv.org/abs/2504.07981); [official harness](https://github.com/likaixin2000/ScreenSpot-Pro-GUI-Grounding)
5. Gou et al., *Navigating the Digital World as Humans Do: Universal Visual Grounding for GUI Agents* (UGround), ICLR 2025 Oral. [arXiv:2410.05243](https://arxiv.org/abs/2410.05243)
6. Cheng et al., *SeeClick: Harnessing GUI Grounding for Advanced Visual GUI Agents*, ACL 2024. [arXiv:2401.10935](https://arxiv.org/abs/2401.10935)
7. Qin et al., *UI-TARS: Pioneering Automated GUI Interaction with Native Agents*. [arXiv:2501.12326](https://arxiv.org/abs/2501.12326)
8. Agyeya et al., *Agent S2: A Compositional Generalist-Specialist Framework for Computer Use Agents* (Tesseract as a textual-grounding sidecar). [arXiv:2504.00906](https://arxiv.org/abs/2504.00906)
9. Xie et al., *OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments*. [arXiv:2404.07972](https://arxiv.org/abs/2404.07972); the a11y tree is obtained on Ubuntu through AT-SPI ([code](https://github.com/xlang-ai/OSWorld))
10. Agashe et al., *Agent S: An Open Agentic Framework that Uses Computers Like a Human*. [arXiv:2410.08164](https://arxiv.org/abs/2410.08164)
11. Zhang et al., *UFO: A UI-Focused Agent for Windows OS Interaction* (UI Automation + vision). [arXiv:2402.07939](https://arxiv.org/abs/2402.07939)
12. Playwright, [*ARIA snapshots*](https://playwright.dev/docs/aria-snapshots): the accessibility tree as compact structured text
13. Wang et al., *GUI Agents: A Survey* (accessibility-API coverage gaps; a11y complements vision). [arXiv:2412.13501](https://arxiv.org/abs/2412.13501)

## License

[MIT](LICENSE)
