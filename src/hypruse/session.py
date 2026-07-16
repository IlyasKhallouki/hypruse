"""Session discovery for stripped environments.

Desktop apps launched via dbus/systemd activation often run their MCP
servers without the session variables a terminal has —
HYPRLAND_INSTANCE_SIGNATURE and WAYLAND_DISPLAY in particular. Both are
recoverable from the filesystem, because the sockets they point at live
in deterministic places:

    $XDG_RUNTIME_DIR/hypr/<instance-signature>/.socket.sock
    $XDG_RUNTIME_DIR/wayland-<n>

ensure_session_env() fills any missing variable from what it finds
(preferring the most recently started Hyprland instance when several
linger), so hypruse works no matter how its host process was launched.
"""

from __future__ import annotations

import os
from pathlib import Path


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def discover_hyprland_instance(runtime: Path) -> str | None:
    hypr = runtime / "hypr"
    if not hypr.is_dir():
        return None
    live = [d for d in hypr.iterdir() if d.is_dir() and (d / ".socket.sock").exists()]
    if not live:
        return None
    live.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return live[0].name


def discover_wayland_display(runtime: Path) -> str | None:
    socks = [
        p.name
        for p in runtime.glob("wayland-*")
        if not p.name.endswith(".lock")
    ]
    return sorted(socks)[0] if socks else None


def ensure_session_env() -> None:
    """Fill missing session variables in-place; never overrides existing ones."""
    runtime = _runtime_dir()
    os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime))
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        sig = discover_hyprland_instance(runtime)
        if sig:
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = sig
    if not os.environ.get("WAYLAND_DISPLAY"):
        disp = discover_wayland_display(runtime)
        if disp:
            os.environ["WAYLAND_DISPLAY"] = disp
