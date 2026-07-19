#!/bin/bash
# Waybar custom module for hypruse: invisible when idle, indicator while an
# agent has hands on the desktop. Click = kill switch.
#
# The module JSON is built by jq FROM the state file, never by shell string
# interpolation: last_action derives from tool arguments an agent chose, and
# hand-built JSON around it would let a crafted argument blank the indicator.
STATE="${XDG_RUNTIME_DIR:-/tmp}/hypruse/state.json"

if [ -f "$STATE" ]; then
    pid=$(jq -r '.pid // empty' "$STATE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        jq -c '{text: "󰚩", class: "active",
                tooltip: ((["agent has hands on this desktop, click to kill"]
                           + (if (.last_action // "") == "" then []
                              else ["last: " + (.last_action | tostring)] end))
                          | join("\n"))}' "$STATE" 2>/dev/null && exit 0
        # unreadable state with a live pid: still show the indicator
        printf '{"text":"󰚩","class":"active","tooltip":"agent has hands on this desktop, click to kill"}\n'
        exit 0
    fi
fi
printf '{"text":"","class":"idle","tooltip":""}\n'
