# hypruse recipes

Worked flows as shell sequences, with the lines each step prints (`#` lines
are expected output). Addresses, paths and coordinates are examples: take
yours from `hypruse desktop` and from the capture metadata. Every flow starts
from `hypruse desktop` and ends with a verification that would fail if the
action had not worked. Recipes 1 to 8 work in the window the human is in, on
their screen; on any other window the same verbs run in the background and
print how they did it (recipe 9).

## Contents

1. [Fill a GTK form by control name](#1-fill-a-gtk-form-by-control-name)
2. [Click a small control with the zoom loop](#2-click-a-small-control-with-the-zoom-loop)
3. [Drive the owner's launcher with binds, use_bind and wait_for](#3-drive-the-owners-launcher-with-binds-use_bind-and-wait_for)
4. [A click, type, enter micro-plan with sequence](#4-a-click-type-enter-micro-plan-with-sequence)
5. [Read a form's state after typing with --then ui](#5-read-a-forms-state-after-typing-with---then-ui)
6. [Handle a refusal (exit 3) correctly](#6-handle-a-refusal-exit-3-correctly)
7. [Find a window on another workspace and focus it](#7-find-a-window-on-another-workspace-and-focus-it)
8. [Troubleshooting](#8-troubleshooting)
9. [Work in an app beside the human (background mode)](#9-work-in-an-app-beside-the-human-background-mode)

## 1. Fill a GTK form by control name

Goal: the human has GNOME Text Editor in front of them and asked you to
write a line in it and save it as `tomorrow.md`. It is the window they are
in, so everything happens on their screen. Everything is by name; no pixel
is estimated.

```bash
hypruse desktop
# ...
# win 0x55a4479a1200 ws 2 org.gnome.TextEditor "New Document" at 7,7 1906x1066
# active 0x55a4479a1200
W=0x55a4479a1200

hypruse keyboard type "notes for tomorrow" --window $W
# typed 18 characters into 0x55a4479a1200

hypruse keyboard key ctrl+s --window $W
# pressed ctrl+s into 0x55a4479a1200

hypruse wait_for window_open --match "Save As" --timeout 5
# openwindow address=0x55a44a0b3c00 workspace=2 class=org.gnome.TextEditor title="Save As"
D=0x55a44a0b3c00

hypruse ui --window $D
# [0] text "Name" @700,140 value="Untitled Document.txt"
# [1] push button "Cancel" @620,88
# [2] push button "Save" @1204,88

hypruse click_ui Name --window $D
# clicked text 'Name' at (700, 140) in org.gnome.TextEditor

hypruse keyboard key ctrl+a --window $D
# pressed ctrl+a into 0x55a44a0b3c00

hypruse keyboard type "tomorrow.md" --window $D --then ui
# typed 11 characters into 0x55a44a0b3c00
# [0] text "Name" @700,140 value="tomorrow.md"
# [1] push button "Cancel" @620,88
# [2] push button "Save" @1204,88
```

The `value="tomorrow.md"` line is the verification. Saving writes a file, so
stop here and confirm with the user unless their instruction already named
this exact file. Then:

```bash
hypruse click_ui Save --window $D --then desktop
# clicked push button 'Save' at (1204, 88) in org.gnome.TextEditor
# monitor eDP-1 at 0,0 1920x1080 scale 1.0 ws 2 focused
# ...
# win 0x55a4479a1200 ws 2 org.gnome.TextEditor "tomorrow.md" at 7,7 1906x1066
```

The dialog's address is gone from the window list and the editor's title
changed: the save happened.

## 2. Click a small control with the zoom loop

Goal: press a small gear icon in an app that exposes no accessibility tree.

```bash
hypruse desktop
# ...
# win 0x55a44b1d2e00 ws 7 electron-notes "Notes" at 7,7 950x1066
# ...
W=0x55a44b1d2e00

hypruse ui --window $W
# electron-notes exposes no accessibility tree; use screenshot + zoom instead
# (exit 4)

hypruse screenshot --window $W
# /run/user/1000/hypruse/shot-1757770000000.jpg
# {"geometry":[7,7,950,1066],"scale":1.0,"image":[950,1066],"format":"jpeg","path":"/run/user/1000/hypruse/shot-1757770000000.jpg"}
```

Read the image file. Estimate by proportion: the gear sits about 92% across
and 4% down a 950x1066 image, so image pixel (874, 43), and
`global = geometry[:2] + pixel / scale = (7 + 874/1.0, 7 + 43/1.0) = (881, 50)`.
That is a full-window estimate, good to tens of pixels: zoom, do not click.

```bash
hypruse zoom 881 50 --window $W
# /run/user/1000/hypruse/shot-1757770000210.jpg
# {"geometry":[477,7,480,360],"scale":1.0,"image":[480,360],"format":"jpeg","path":"/run/user/1000/hypruse/shot-1757770000210.jpg","target":"zoom","point":[881,50]}
```

The box was clamped to the window, so the point is not at the image center:
always compute from `geometry`. On the zoomed image the gear's center is at
pixel (408, 44), so `global = (477 + 408, 7 + 44) = (885, 51)`.

```bash
hypruse pointer click 885 51 --then screenshot
# click ok; cursor now at (885, 51)
# /run/user/1000/hypruse/shot-1757770000640.jpg
# {"geometry":[0,0,1920,1080],"scale":1.0,"image":[1920,1080],"format":"jpeg","path":"/run/user/1000/hypruse/shot-1757770000640.jpg","stable":true}
```

Read the new capture and confirm the settings panel is open. On a scale 1.5
monitor the first screenshot would say `"scale":1.5,"image":[1425,1599]` and
the same gear at image pixel (1311, 64) still maps to `7 + 1311/1.5 = 881`;
the formula absorbs the scale, so never adjust by hand. If the click missed,
zoom once more at the corrected point; after two misses, stop and ask.

## 3. Drive the owner's launcher with binds, use_bind and wait_for

Goal: start `btop` the way the owner does, through their launcher.

```bash
hypruse binds
# SUPER+RETURN  exec kitty  "terminal"
# SUPER+SPACE  exec wofi --show drun  "launcher"
# SUPER+F  lua  "scratchpad"

hypruse use_bind SUPER+SPACE
# ran SUPER+SPACE: exec wofi --show drun

hypruse wait_for layer_open --match wofi --timeout 5
# already: openlayer namespace=wofi
```

`already:` means the launcher mapped before the wait subscribed; either line
means it is up. A launcher is a layer, not a window, and it holds the
keyboard grab, so this is the one place `keyboard` runs without `--window`:

```bash
hypruse keyboard type "btop"
# typed 4 characters; NOTE: the launcher layer 'wofi' holds the keyboard grab, so the keys went to it, not to the focused window

hypruse keyboard key enter
# pressed enter; NOTE: the launcher layer 'wofi' holds the keyboard grab, so the keys went to it, not to the focused window

hypruse wait_for window_open --match btop --timeout 10
# openwindow address=0x55a44c0a9100 workspace=7 class=btop title="btop"
```

The `openwindow` line is the verification. `SUPER+F` above is a `lua` bind:
`use_bind SUPER+F` exits 1 with an explanation, because a Lua closure cannot
be called from outside; read its description and do the same thing with
`hypr` or `launch`. If the launcher is not what you need after all, leave it
with `hypruse keyboard key esc` (no `--window`) and confirm with
`hypruse wait_for layer_close --match wofi --timeout 5`.

## 4. A click, type, enter micro-plan with sequence

Goal: search a Files window for "invoice" in one round-trip. Enter here only
runs a search, so it is safe inside the plan; an enter that submits or sends
belongs in its own step after the user confirms.

```bash
W=0x55a4479a1200   # org.gnome.Nautilus, from hypruse desktop
hypruse sequence '[
  {"op":"click_ui","name":"Search","window":"'$W'"},
  {"op":"keyboard","action":"type","text":"invoice","window":"'$W'"},
  {"op":"keyboard","action":"key","keys":"enter","window":"'$W'"},
  {"op":"wait_for","event":"title_change","match":"invoice","timeout_s":5}
]' --then ui
# sequence: all 4/4 steps ran
# [0] click_ui : clicked toggle button 'Search' at (1180, 88) in org.gnome.Nautilus
# [1] keyboard type: typed 7 characters into 0x55a4479a1200
# [2] keyboard key: pressed enter into 0x55a4479a1200
# [3] wait_for title_change: {'event': 'windowtitlev2', 'address': '0x55a4479a1200', 'title': 'invoice'}
# [0] text "Search" @1180,88 value="invoice"
# [1] push button "Back" @40,88
# ...
```

Every keyboard step carries `window` because a sequence does not catch a
bare focus change. When the desktop changes in a way a step did not intend,
the run stops:

```bash
# sequence: stopped after 1/4 steps, desktop changed (openwindow) before step 1
# [0] click_ui : clicked toggle button 'Search' at (1180, 88) in org.gnome.Nautilus
# (exit 4)
```

Step 0 already happened. Run `hypruse desktop`, find what opened, deal with
it, and resume from step 1 with a new sequence; never re-run the whole plan.
For a plan longer than one line, write it to a file and pass `@plan.json`,
or pipe it in with `hypruse sequence -`.

## 5. Read a form's state after typing with --then ui

Goal: rename a new folder in a GTK dialog and prove the field holds exactly
the new name before pressing the button.

```bash
D=0x55a44d2f0000   # the "New Folder" dialog, from hypruse desktop
hypruse ui --window $D
# [0] text "Folder name" @700,140 value="Untitled Folder"
# [1] check box "Create as hidden" @640,190 checked=false
# [2] push button "Cancel" @620,240
# [3] push button "Create" @1204,240

hypruse click_ui "Folder name" --window $D
# clicked text 'Folder name' at (700, 140) in org.gnome.Nautilus

hypruse keyboard key ctrl+a --window $D
# pressed ctrl+a into 0x55a44d2f0000

hypruse keyboard type "receipts-2026" --window $D --then ui
# typed 13 characters into 0x55a44d2f0000
# [0] text "Folder name" @700,140 value="receipts-2026"
# [1] check box "Create as hidden" @640,190 checked=false
# [2] push button "Cancel" @620,240
# [3] push button "Create" @1204,240
```

`value="receipts-2026"` is the proof. If it read
`value="Untitled Folderreceipts-2026"`, the select-all did not take: fix the
field before touching `Create`. A toggle is verified the same way:

```bash
hypruse click_ui "Create as hidden" --window $D --then ui
# clicked check box 'Create as hidden' at (640, 190) in org.gnome.Nautilus
# ...
# [1] check box "Create as hidden" @640,190 checked=true
```

`Create` changes the filesystem: press it only if the user's instruction
covered exactly this, otherwise confirm first. Password fields never report a
value and many dropdowns report none, so for those a screenshot is the
verification, not `ui`.

## 6. Handle a refusal (exit 3) correctly

The owner set `HYPRUSE_CONFINE=class:firefox,kitty`. The task drifts toward
a Signal window:

```bash
hypruse keyboard type "on my way" --window 0x55a44e000000
# hypruse: refused: 0x55a44e000000 (Signal) is outside the agent's confinement scope (class:firefox,kitty)
# (exit 3)
```

Correct handling, in order:

1. Stop. Nothing was delivered, and nothing should be.
2. Do not route around it: not `pointer click` into the window followed by
   `keyboard type` without `--window`, not `use_bind`, not `--allow-auth`.
   Every one of those is a different guard saying the same thing, or a
   guard the owner deliberately set.
3. Report the reason verbatim to the user and what you did not do: "hypruse
   refused to type into the Signal window because it is outside the
   confinement scope (class:firefox,kitty). I sent nothing. Widen
   HYPRUSE_CONFINE if you want me working there."

The same rule holds for every refusal text: an authentication dialog
(`hyprpolkitagent is a system authentication dialog; refusing to drive it`, or
`the authentication prompt 'omarchy-polkit' holds the keyboard` for a prompt
the shell draws as a layer),
a password field, a locked session, a launcher holding the keyboard grab, and
read-only mode. Only the strict-mode seat refusal carries its own retry (see
Troubleshooting). When branching in a script, branch on the exit code, not on
the text:

```bash
hypruse click_ui Send --window $W --then ui
case $? in
  0) ;;                                   # read the ui lines and verify
  3) exit 3 ;;                            # report; the reason is on stderr
  4) hypruse marks --window $W ;;         # ambiguous or no tree: look, then choose
esac
```

Exit 3 is also what every acting verb returns under HYPRUSE_READONLY: the
owner chose observation only, so narrate what you see and what you would do.

A background refusal is the exception that names its own way forward. Its
text ends `so it cannot be done in the background:` and then what works:
`click_ui` instead of `pointer` for a control in another window, or
`--foreground`, which takes over the human's screen and is right only when
the user asked for that. Under HYPRUSE_BACKGROUND=1 the way forward is to
ask the user.

## 7. Find a window on another workspace and focus it

Goal: bring up the Firefox window that is not on the current workspace.
This switches the human's view, so do it only when they asked to see it;
under HYPRUSE_BACKGROUND=1 `focus_window` is refused. To work in that window
without showing it, skip the focus and use recipe 9.

```bash
hypruse desktop
# monitor eDP-1 at 0,0 1920x1080 scale 1.0 ws 7 focused
# ws 3 "3" on eDP-1 windows 1
# ws 7 "7" on eDP-1 windows 2 visible
# win 0x55a447938f40 ws 7 kitty "~" at 964,7 949x1066
# win 0x55a4479a1200 ws 3 firefox "Inbox" at 7,7 1906x1066
# win 0x55a44c0a9100 ws 7 btop "btop" at 7,7 950x1066
# active 0x55a447938f40
# cursor 642,516
```

The `win` line says Firefox lives on workspace 3. No screenshot was needed.

```bash
hypruse hypr focus_window 0x55a4479a1200 --then desktop
# focused 0x55a4479a1200
# monitor eDP-1 at 0,0 1920x1080 scale 1.0 ws 3 focused
# ...
# active 0x55a4479a1200
```

`ws 3 focused` and `active 0x55a4479a1200` are the verification. Focusing a
window on another workspace switches the whole desktop there, and the human
sees the switch. To bring the window here instead, silently:

```bash
hypruse hypr move_window 0x55a4479a1200 7 --then desktop
# moved 0x55a4479a1200 to workspace 7
# ...
# win 0x55a4479a1200 ws 7 firefox "Inbox" at 7,7 950x1066
```

With many windows, `hypruse desktop | grep -i firefox` is fine: the output
is plain text on purpose. Do not act on a stale address; if a `hypr` call
says `not found, call desktop() for current addresses` (exit 1), re-read.

## 8. Troubleshooting

**"exposes no accessibility tree" (exit 4).** Terminals, canvas apps, games
and Electron/Chrome started without `--force-renderer-accessibility` expose
little or nothing. Fall back to `screenshot --window`, `zoom`, `pointer`
(recipe 2). A tree that exists but prints `no actionable elements` may still
hold what you need under `ui --all` or a `--name` filter. A huge tree says
it stopped after reading 6000 elements, and `--name` reads the same ones,
so it does not get further. `busctl` missing or no AT-SPI bus reads as
`accessibility read failed: ...`; tell the owner.

**Ambiguous names (exit 4).** `click_ui` lists the candidates in `ui` line
form. Pick one with `--index I` (0-based into that list), use a more
specific name (an exact accessible name wins over a substring), or run
`marks` and click `--mark N` after looking. Never guess between two
candidates named "Delete".

**A launcher holds the keyboard (exit 3).** `keyboard ... --window ADDR`
aimed at the human's window (a window in the background still gets its keys)
refuses with `the launcher layer 'wofi' holds the keyboard grab, so keys
cannot reach the requested window. Drive the launcher itself (call keyboard
without window=), or close it first (usually esc).` Do one of those two
things: type into the launcher deliberately with no `--window` (recipe 3),
or `hypruse keyboard key esc`, `hypruse wait_for layer_close --match wofi`,
then retry with `--window`. `click_ui` refuses the same way when a layer
covers the target point; `pointer click` on the layer is the deliberate way
to click a launcher.

**Locked session (exit 3).** `the session is locked (hyprlock)...` means a
lock surface takes every event and no window can receive input. The lock is
invisible to `desktop`'s window and layer lists, so the refusal is the only
signal. Report it and stop; `--allow-auth` exists so a human can ask the
agent to drive the unlock prompt, and nothing else justifies it.

**Strict-mode seat refusal (exit 3).** `the seat moved since hypruse last
acted (cursor or focus changed without the agent): re-read
desktop()/screenshot() and retry.` The human, or a popup, took the seat.
This is the one refusal with a built-in retry: run `hypruse desktop` (or
`screenshot`), which re-arms the guard, then retry the action once. If it
refuses again, the human is using the desktop: stop and ask.

**Timeouts (exit 4).** `wait_for` prints `timeout: no matching ... event
within 10s`; `launch` prints `launched, but no new window appeared within
8s...`; `sequence` prints `time budget (30s) reached before step N`. First
check `desktop`: the thing may already have happened before the wait
subscribed (`already:` covers only filtered `window_close`, `workspace` and
`layer_open`), or the app may have opened on its own workspace. Then raise
`--timeout` (max 60) or `--wait` (max 30). Every verb returns inside 60 s,
under the shell's 120 s limit, so never wrap hypruse in a sleep loop, and
never re-run an acting verb "because it timed out" without checking whether
it acted.

**Stale addresses (exit 1).** `window '0x...' not found, call desktop() for
current addresses`: window addresses are heap pointers and die with the
window. Re-read `desktop`; never reuse an address from an earlier task.

**Dry run.** Results that open with `DRY RUN, nothing was delivered: would
...` mean the owner set HYPRUSE_DRYRUN (or you passed `--dry-run`). The
screen will not change; report the plan instead of retrying.


## 9. Work in an app beside the human (background mode)

Goal: fill in a sign-up form while the human keeps working. Nothing below
moves their cursor, takes their keyboard focus or switches their workspace.

```bash
hypruse launch -- yad --form --title=Signup --field=Agree:CHK --field=Name --button=OK:0
# 0x564859226b80 yad "Signup" workspace 99
# note opened on workspace 99, one of the agent's (HYPRUSE_AGENT_WORKSPACES), out of the human's sight; pass background=false to open it on the human's workspace
W=0x564859226b80

hypruse ui --window $W
# [0] text "Name" @981,538 value=""
# [1] check box "Agree" @960,501 checked=false
# [2] button "OK" @1019,578
# note 0x564859226b80 is not on screen (no monitor is showing its workspace 99): these points are inside it, not on what the human sees, so press its controls with click_ui; a pointer click at these points would land on whatever is on screen there

hypruse click_ui Agree --window $W
# pressed check box 'Agree' through its accessibility action (no pointer, the human's focus untouched) in yad; now: checked=true

hypruse keyboard type "Ada Lovelace" --window $W --then ui
# typed 12 characters into the text field in yad through the accessibility interface (the human's focus untouched); the field now holds the typed text (12 characters)
# [0] text "Name" @981,538 value="Ada Lovelace"
# [1] check box "Agree" @960,501 checked=true
# [2] button "OK" @1019,578
# note 0x564859226b80 is not on screen (no monitor is showing its workspace 99): these points are inside it, not on what the human sees, so press its controls with click_ui; a pointer click at these points would land on whatever is on screen there
```

The `now:` part, `the field now holds the typed text` and the `--then ui`
values are the verification: they are read back from the accessibility
tree. `(the human's focus untouched)` is checked too: if a line ends with
`WARNING: the human's focused window changed ...`, an app brought a window
forward or the human switched, so stop and tell the user. Do not screenshot a hidden window to
check; many apps stop drawing while hidden, so the capture can show an old
frame.

Never use `pointer` on a hidden window. The `@x,y` points are where its
controls sit inside it, and the screen at those points shows the human's
own windows, so a click there would land on whatever they are looking at;
the `note` line says so. A control without an accessibility action and a
right or double click are refused in the background, since neither can
happen without the human's cursor.

In a form with several text fields, focus the one you want first with
`click_ui NAME`; the keys then go to it through sendshortcut
(`sent 5 keys to 0x... (yad) with sendshortcut ...`). Submitting is still
the user's call: confirm before pressing OK.
