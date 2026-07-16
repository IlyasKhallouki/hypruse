#!/bin/bash
# Waybar custom module for hypruse: invisible when idle, indicator while an
# agent has hands on the desktop. Click = kill switch.
STATE="${XDG_RUNTIME_DIR:-/tmp}/hypruse/state.json"

if [ -f "$STATE" ]; then
    pid=$(jq -r '.pid // empty' "$STATE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        last=$(jq -r '.last_action // ""' "$STATE" 2>/dev/null)
        printf '{"text":"󰚩","class":"active","tooltip":"agent has hands on this desktop, click to kill%s"}\n' \
            "${last:+\nlast: $last}"
        exit 0
    fi
fi
printf '{"text":"","class":"idle","tooltip":""}\n'
