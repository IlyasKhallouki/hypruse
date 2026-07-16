"""Activity beacon and kill-switch support.

The seat — cursor, keyboard focus — is shared between the agent and the
human at the desk. hypruse therefore keeps a runtime beacon at
$XDG_RUNTIME_DIR/hypruse/state.json:

    {"pid": 1234, "started": 1789...,
     "last_action": "pointer:click", "last_ts": 1789...}

written at startup, updated on every acting tool call, removed on clean
shutdown. Anything can watch it — the shipped Waybar module (waybar/)
shows a red indicator while an agent has hands on the desktop, and its
click action (or a Hyprland keybind) runs `pkill -f hypruse`: the
process dies mid-action at worst, never holding a button down, because
button press/release pairs are sent inside one tool call.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

_state_path: Path | None = None
_started: float = 0.0


def state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    d = Path(base) / "hypruse"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def _write(payload: dict) -> None:
    if _state_path is None:
        return
    tmp = _state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(_state_path)


def init() -> None:
    """Start the beacon; safe to call once at server startup."""
    global _state_path, _started
    _state_path = state_path()
    _started = time.time()
    _write({"pid": os.getpid(), "started": _started, "last_action": "", "last_ts": 0})
    atexit.register(shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _die)


def touch(action: str) -> None:
    """Record an acting tool call (no-op if init() was never called)."""
    if _state_path is None:
        return
    _write(
        {
            "pid": os.getpid(),
            "started": _started,
            "last_action": action,
            "last_ts": time.time(),
        }
    )


def shutdown() -> None:
    global _state_path
    if _state_path is not None:
        with contextlib.suppress(OSError):
            _state_path.unlink(missing_ok=True)
        _state_path = None


def _die(signum: int, _frame: object) -> None:
    shutdown()
    sys.exit(128 + signum)
