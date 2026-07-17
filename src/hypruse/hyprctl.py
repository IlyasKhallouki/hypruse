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


def batch_query(commands: list[str]) -> list[Any]:
    """Run several JSON queries in ONE hyprctl invocation (one fork, one
    socket round-trip) instead of one per command, ~4x faster for the
    snapshot. hyprctl concatenates the JSON documents, so split them by
    decoding successive values."""
    spec = " ; ".join(f"j/{c}" for c in commands)
    out = _run("--batch", spec)
    decoder = json.JSONDecoder()
    vals: list[Any] = []
    i, n = 0, len(out)
    while i < n:
        while i < n and out[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            value, i = decoder.raw_decode(out, i)
        except json.JSONDecodeError as exc:
            raise HyprctlError(
                f"unparseable hyprctl --batch output near {out[i : i + 80]!r}"
            ) from exc
        vals.append(value)
    if len(vals) != len(commands):
        raise HyprctlError(
            f"hyprctl --batch returned {len(vals)} results for {len(commands)} commands"
        )
    return vals


def dispatch(name: str, *args: str) -> None:
    """Run a dispatcher; Hyprland answers 'ok' on success, an error string otherwise."""
    out = _run("dispatch", name, *args)
    if out != "ok":
        raise HyprctlError(f"dispatch {name} {' '.join(args)}: {out}")


def cursor_pos() -> tuple[int, int]:
    pos = query("cursorpos")
    return int(pos["x"]), int(pos["y"])


def logical_rect(m: dict[str, Any]) -> tuple[int, int, int, int]:
    """A monitor's rect in global logical coordinates (the one space
    everything else in hypruse uses). hyprctl reports width/height as
    physical mode pixels, so the logical footprint is size/scale, with
    the axes swapped by 90/270-degree transforms (odd transform values).
    This is the single source of truth for monitor geometry."""
    scale = float(m.get("scale", 1.0)) or 1.0
    w, h = m["width"] / scale, m["height"] / scale
    if int(m.get("transform", 0)) % 2:
        w, h = h, w
    return int(m["x"]), int(m["y"]), round(w), round(h)


def contains(rect: tuple[int, int, int, int], x: float, y: float) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def monitor_at(monitors: list[dict[str, Any]], x: float, y: float) -> dict[str, Any] | None:
    """The monitor whose logical rect contains (x, y), or None if the
    point is off every monitor."""
    return next((m for m in monitors if contains(logical_rect(m), x, y)), None)


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


def _monitor(m: dict[str, Any]) -> dict[str, Any]:
    """Trim a hyprctl monitor to a model view, geometry in the same global
    logical space as window `at`/`size` (see logical_rect: hyprctl's raw
    width/height are physical mode pixels)."""
    x, y, w, h = logical_rect(m)
    out: dict[str, Any] = {
        "name": m["name"],
        "geometry": [x, y, w, h],
        "scale": m.get("scale", 1.0),
        "focused": m.get("focused", False),
        "active_workspace": m.get("activeWorkspace", {}).get("id"),
    }
    if int(m.get("transform", 0)):
        out["transform"] = int(m["transform"])
    return out


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
        "monitors": [_monitor(m) for m in monitors],
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
    """Compact, token-lean view of the whole desktop, from one batched
    hyprctl call (~4x faster than five separate queries)."""
    monitors, workspaces, clients, active, cursor = batch_query(
        ["monitors", "workspaces", "clients", "activewindow", "cursorpos"]
    )
    cur = (int(cursor["x"]), int(cursor["y"])) if cursor else None
    return snapshot_from(monitors, workspaces, clients, active or None, cur)


# X11 modifier bits as Hyprland reports them in `binds` modmask
_MOD_BITS = (
    (64, "SUPER"),
    (4, "CTRL"),
    (1, "SHIFT"),
    (8, "ALT"),
    (2, "CAPS"),
    (16, "MOD2"),
    (32, "MOD3"),
    (128, "MOD5"),
)


def modmask_to_names(mask: int) -> list[str]:
    return [name for bit, name in _MOD_BITS if mask & bit]


def parse_binds(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim hyprctl's bind records to what an agent can actually use.

    Mouse binds are dropped (not reproducible through the keyboard tool);
    keycode-only binds keep a code: marker so they are at least visible.
    """
    out: list[dict[str, Any]] = []
    for b in raw:
        if b.get("mouse"):
            continue
        key = b.get("key") or ""
        if not key and b.get("keycode"):
            key = f"code:{b['keycode']}"
        if not key:
            continue
        combo = "+".join([*modmask_to_names(int(b.get("modmask", 0))), key])
        entry: dict[str, Any] = {
            "combo": combo,
            "action": b.get("dispatcher", ""),
            "arg": b.get("arg", ""),
        }
        if b.get("description"):
            entry["description"] = b["description"]
        if b.get("submap"):
            entry["submap"] = b["submap"]
        out.append(entry)
    return out


def binds() -> list[dict[str, Any]]:
    return parse_binds(query("binds"))


def _norm_combo(combo: str) -> str:
    return combo.upper().replace(" ", "")


def find_bind(combo: str) -> dict[str, Any] | None:
    target = _norm_combo(combo)
    for b in binds():
        if _norm_combo(b["combo"]) == target:
            return b
    return None
