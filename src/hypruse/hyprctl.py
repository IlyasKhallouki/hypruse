"""Thin layer over Hyprland's hyprctl IPC.

Everything hypruse knows about the desktop comes through here, and every
workspace/window action goes back out through dispatch(). It shells out to
the hyprctl binary rather than opening the .socket directly so behaviour
always matches what the user's own shell would do.

Coordinates everywhere in hypruse are Hyprland's global *logical* layout
coordinates, the same space `hyprctl cursorpos`, client `at`, and
`dispatch movecursor` use.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class HyprctlError(RuntimeError):
    """hyprctl failed, returned an error, or is unreachable."""


def _run(*args: str) -> str:
    if shutil.which("hyprctl") is None:
        raise HyprctlError("hyprctl not found, hypruse needs a running Hyprland session")
    try:
        proc = subprocess.run(
            ["hyprctl", *args], capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired as exc:
        raise HyprctlError(f"hyprctl {' '.join(args)} timed out") from exc
    out = proc.stdout.strip()
    if proc.returncode != 0:
        raise HyprctlError(f"hyprctl {' '.join(args)}: {proc.stderr.strip() or out}")
    return out


def query(command: str) -> Any:
    """Run a JSON query: monitors, workspaces, clients, activewindow, cursorpos, ..."""
    out = _run("-j", command)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise HyprctlError(f"unparseable hyprctl -j {command} output: {out[:200]!r}") from exc


def dispatch(name: str, *args: str) -> None:
    """Run a dispatcher; Hyprland answers 'ok' on success, an error string otherwise."""
    out = _run("dispatch", name, *args)
    if out != "ok":
        raise HyprctlError(f"dispatch {name} {' '.join(args)}: {out}")


def cursor_pos() -> tuple[int, int]:
    pos = query("cursorpos")
    return int(pos["x"]), int(pos["y"])


def _window(c: dict[str, Any]) -> dict[str, Any]:
    """Trim a hyprctl client to what a model needs to reason and act."""
    win: dict[str, Any] = {
        "address": c["address"],
        "workspace": c.get("workspace", {}).get("id"),
        "class": c.get("class", ""),
        "title": c.get("title", ""),
        "at": c.get("at"),
        "size": c.get("size"),
        "floating": c.get("floating", False),
        "pid": c.get("pid"),
    }
    # int enum since Hyprland 0.42 (0 none / 1 maximized / 2 fullscreen), bool before
    if c.get("fullscreen"):
        win["fullscreen"] = True
    if c.get("hidden"):
        win["hidden"] = True
    return win


def snapshot_from(
    monitors: list[dict[str, Any]],
    workspaces: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    active_window: dict[str, Any] | None,
    cursor: tuple[int, int] | None,
) -> dict[str, Any]:
    """Pure assembly of the desktop state, separated from IPC for testability."""
    visible = {m.get("activeWorkspace", {}).get("id") for m in monitors}
    return {
        "monitors": [
            {
                "name": m["name"],
                "geometry": [m["x"], m["y"], m["width"], m["height"]],
                "scale": m.get("scale", 1.0),
                "focused": m.get("focused", False),
                "active_workspace": m.get("activeWorkspace", {}).get("id"),
            }
            for m in monitors
        ],
        "workspaces": [
            {
                "id": w["id"],
                "name": w.get("name", ""),
                "monitor": w.get("monitor", ""),
                "windows": w.get("windows", 0),
                "visible": w["id"] in visible,
            }
            for w in sorted(workspaces, key=lambda w: w["id"])
        ],
        "windows": [_window(c) for c in clients if c.get("mapped", True)],
        "active_window": (active_window or {}).get("address"),
        "cursor": list(cursor) if cursor else None,
    }


def snapshot() -> dict[str, Any]:
    """Compact, token-lean view of the whole desktop."""
    return snapshot_from(
        query("monitors"),
        query("workspaces"),
        query("clients"),
        query("activewindow") or None,
        cursor_pos(),
    )
