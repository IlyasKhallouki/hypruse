# hypruse for Hermes

A computer-use provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that drives Hyprland through hypruse. Hermes keeps its own `computer_use`
tool, its schema and its approval prompts; this provider replaces the driver
underneath. Select it with `computer_use.backend: hypruse`.

Hermes' default driver, cua-driver, reaches Wayland through an XWayland
bridge, and on Hyprland its captures often come back empty or match no
window (hermes-agent issues
[#74969](https://github.com/NousResearch/hermes-agent/issues/74969) and
[#130824](https://github.com/NousResearch/hermes-agent/issues/130824), with
fixes still open in
[#74987](https://github.com/NousResearch/hermes-agent/pull/74987) and
[#89342](https://github.com/NousResearch/hermes-agent/pull/89342)).
hypruse talks to Hyprland directly: windows come from its IPC, captures from
grim, controls from the accessibility tree. With background mode, the agent
works in apps on its own workspaces (99 down to 90) while you keep using
yours.

## Requirements

- Hyprland, and hypruse 0.13 or newer on `PATH` (`hypruse --version`). Check
  the setup with `hypruse doctor`.
- Hermes 0.23 or newer.
- Nothing installed into Hermes' environment. The provider runs the
  `hypruse` command as a separate program and uses only the standard library.

## Install

Pick one, then select the provider.

From GitHub, with Hermes' own installer:

```sh
hermes plugins install IlyasKhallouki/hypruse#integrations/hermes/hermes_hypruse
```

From a checkout of this repository, as a link:

```sh
ln -s "$PWD/integrations/hermes/hermes_hypruse" ~/.hermes/plugins/hypruse
```

As a Python package. Hermes has no entry point for computer-use providers.
It loads them from `$HERMES_HOME/plugins/<name>/`, so the package ships a
command that links itself there (`--copy` copies instead):

```sh
uv tool install ./integrations/hermes     # or: pipx install ./integrations/hermes
hermes-hypruse install
```

Then select it and check it:

```sh
hermes config set computer_use.backend hypruse
hermes computer-use doctor
```

`hermes tools`, under Computer Use, lists it as well. Selecting it needs no
`plugins.enabled` entry.

## Configuration

Everything is optional:

```yaml
computer_use:
  backend: hypruse
  max_image_dimension: 1456   # shared with cua-driver; vision captures are scaled to fit
  hypruse:
    command: hypruse          # a path, a string, or a list; HERMES_HYPRUSE_CMD wins over it
    delivery: background      # background (default) or auto
    timeout: 60               # seconds per hypruse call
```

`delivery: background` sends `--background` with every action that omits
`delivery_mode`. hypruse then does the action without your cursor, focus or
workspace, or refuses it and says what would work instead. That matches
what a background approval in Hermes promises. With `auto`, hypruse decides
for itself: an action on the window you are in runs on your screen, and one
on any other window runs in the background.

hypruse's own settings stay in the environment Hermes runs in:
`HYPRUSE_BACKGROUND=1`, `HYPRUSE_CONFINE`, `HYPRUSE_READONLY`,
`HYPRUSE_AUTH_GUARD` and the rest. The provider hands `hypruse` the desktop
session variables and every `HYPRUSE_*` variable, and nothing else, so
Hermes' API keys never reach it. For an unattended or `--yolo` run, set
`HYPRUSE_BACKGROUND=1` so that nothing can touch your screen.

## How the actions map

| Hermes action | hypruse call |
|---|---|
| `capture` (`som`, the default) | `marks --window ADDR`: the image with numbered controls, and the list |
| `capture` (`ax`) | the same call; only the list goes to the model |
| `capture` (`vision`) | `screenshot --window ADDR --stable`, scaled to `max_image_dimension` |
| `capture` (`app='screen'`) | `screenshot`, the focused monitor |
| `click` / `double_click` / `right_click` / `middle_click` by `element` | `click_ui --mark N` |
| `click` by `coordinate` | `pointer click X Y`, foreground only |
| `drag`, `scroll` | `pointer drag`, `pointer scroll --at`, foreground only |
| `type` | `keyboard type - --window ADDR`, with the text on stdin |
| `key` | `keyboard key COMBO --window ADDR` |
| `focus_app` | picks the window; with `raise_window`, `hypr focus_window` |
| `list_apps`, `list_windows` | `desktop` |
| `wait` | Hermes' default sleep |
| `set_value` | not supported, see below |

Each Hermes action is one `hypruse ... --json` process, plus a `desktop` read
where a window has to be found. The CLI is where hypruse applies its
read-only gate, its lock between processes, its journal, and the state that
keeps a marks numbering from one call to the next. Calling hypruse's Python
functions from inside Hermes would skip all of that and pull hypruse's
dependencies into Hermes' pinned environment.

`app` matches a window address, then a class, then a title (exact first,
then a substring). `app='active'` is the window you are in. A `capture` with
no `app` stays on the window the last capture or `focus_app` chose, so it
only captures your window when the model asks for it. `window_id` in
`list_windows` is the Hyprland address as an integer, and
`capture(window_id=...)` accepts it.

## Coordinates

The model works in the pixels of the last capture, origin top-left, as
Hermes' schema says. hypruse works in Hyprland global logical pixels. The
capture's metadata gives the map, `global = geometry[:2] + pixel / scale`,
and the provider applies it. The map is anchored to the captured window: if
the window moved since the capture, the click follows it, and if it was
resized, the click is refused until the model captures again. Element
`bounds` are 1x1 boxes at each control's click point, in the same pixels;
hypruse reports a point for each control, not its full box.

## Refusals

hypruse exits 3 when one of its trust layers refuses an action: read-only
mode, a confinement scope, a password field or authentication dialog, a
locked session, or something background mode cannot do. The provider
returns that as `ok: false` with hypruse's own message and never retries,
re-routes or downgrades the call. A trust-layer refusal carries
`code: "refused"` and tells the model not to try another way. A background
refusal carries `code: "background_unavailable"` and keeps hypruse's advice.
Going to the foreground then takes an explicit `delivery_mode='foreground'`,
which Hermes approves separately.

The pointer is your cursor, so it never works in the background. A
coordinate click, drag or scroll needs `delivery_mode='foreground'`, and the
captured window has to be the one you are in. Otherwise the provider refuses
before any input, because the point would land on whatever your screen shows
there. `bring_to_front=true` focuses the window first, which Hermes also
approves separately. hypruse's own rule still applies on top: input that
would land on a window other than yours is refused.

## What does not work, and why

- `set_value`: hypruse has no verb that sets a value. Click the field by
  element and type, click a check box, or click a dropdown and then its
  option.
- Modifier keys held during a click, drag or scroll, and triple clicks:
  hypruse's pointer has neither.
- Right, middle and double clicks by element in the background: an
  accessibility action is the control's primary action, a plain left
  click. They work in the foreground.
- Pointer actions in the background, and on a window that is not on your
  screen, for the reason above.
- `som` images at full size: hypruse's `marks` has no downscale, so
  `max_image_dimension` applies to `vision` captures only.
- Restoring focus after a foreground action: hypruse leaves the window
  focused.
- Opening apps: Hermes' `computer_use` has no launch action. Run
  `hypruse launch -- APP` with Hermes' terminal tool; it opens the app on one
  of the agent's workspaces, out of your sight. Then `capture(app='APP')`.
- `plugins.isolation: host`: Hermes loads computer-use providers in its own
  process only, and refuses user providers under that setting.
- Apps without an accessibility tree (terminals, and Chromium or Electron
  without `--force-renderer-accessibility`) have no elements. `type` and
  `key` still reach them in the background, except XWayland windows, which
  hypruse refuses there.

## What it does on your machine

It runs the `hypruse` command once per action, with the environment
described above. Screenshots of the targeted window, or of the focused
monitor for `app='screen'`, go to your model like any computer-use
backend's. The provider deletes each capture file once it has read it, and
Hermes keeps its own copy. It reads hypruse's state file
`$XDG_RUNTIME_DIR/hypruse/cli-state.json`, never writes it, to notice when a
`marks` call from somewhere else has replaced the numbering before a click
by element. It makes no network calls, stores nothing, and holds no
credentials.

## Tests

The unit tests use a fake `hypruse` on a fake desktop and need only pytest:

```sh
cd integrations/hermes
uv run --isolated --no-project --with pytest python -m pytest tests
```

With a hermes-agent checkout, the same tests run against Hermes' real
provider contract, and `test_hermes_contract.py` drives the provider through
Hermes' own `computer_use` tool: discovery, approvals, dispatch, response
shaping and `capture_after`:

```sh
HERMES_AGENT_SRC=/path/to/hermes-agent uv run --isolated --frozen \
  --directory /path/to/hermes-agent --with pytest \
  python -m pytest /path/to/hypruse/integrations/hermes/tests
hermes plugins validate /path/to/hypruse/integrations/hermes/hermes_hypruse
```

`hermes plugins validate` runs the checks Hermes' plugin catalog runs at
admission, and this provider passes them.
