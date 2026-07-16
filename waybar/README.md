# Waybar indicator + kill switch

A tiny status module: **invisible when idle, 󰚩 while an agent has hands on
your desktop, click to kill it instantly.**

## Install

Copy the script and make it executable:

```sh
cp hypruse-status.sh ~/.config/waybar/scripts/
chmod +x ~/.config/waybar/scripts/hypruse-status.sh
```

Add the module to your Waybar config:

```jsonc
"custom/hypruse": {
    "exec": "~/.config/waybar/scripts/hypruse-status.sh",
    "return-type": "json",
    "interval": 1,
    "on-click": "pkill -f hypruse"
}
```

Style it with your theme's alert color (no hardcoded palette, reuse your
own variables):

```css
#custom-hypruse.active {
    color: @critical; /* or your theme's red */
}
```

## Panic keybind

For a keyboard-only kill switch, add to `hyprland.conf` (or
`userprefs.conf` on HyDE):

```ini
bind = SUPER SHIFT, BackSpace, exec, pkill -f hypruse
```

`pkill -f hypruse` is safe mid-action: button press/release pairs are sent
within a single tool call, so the agent can never die holding a mouse
button down.
