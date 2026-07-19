"""Hyprland event socket (socket2) listener.

Hyprland broadcasts desktop events as lines of "EVENT>>DATA" on
$XDG_RUNTIME_DIR/hypr/<instance>/.socket2.sock. Waiting on real events
beats polling: launch knows the moment a window maps, and agents can
block on "the window whose title contains X changed" instead of
sleeping and hoping.

Field notes that matter for correctness:
- socket2 window addresses come WITHOUT the 0x prefix that hyprctl
  clients use; normalize before comparing.
- DATA is comma-separated but titles may contain commas, so each event
  is split with its own maxsplit.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class EventError(RuntimeError):
    """The event socket is unreachable."""


# event name -> (field names, maxsplit for the comma split)
_SCHEMAS: dict[str, tuple[tuple[str, ...], int]] = {
    "openwindow": (("address", "workspace", "class", "title"), 3),
    "closewindow": (("address",), 0),
    "movewindow": (("address", "workspace"), 1),
    "workspace": (("name",), 0),
    "activewindowv2": (("address",), 0),
    "windowtitlev2": (("address", "title"), 1),
    "windowtitle": (("address",), 0),
    # layer-shell surfaces: launchers, bars, notification popups, lock
    # screens; namespace is the surface's app-chosen layer name
    "openlayer": (("namespace",), 0),
    "closelayer": (("namespace",), 0),
    "urgent": (("address",), 0),
    # screen sharing started/stopped: state 0|1, owner 0=monitor 1=window
    "screencast": (("state", "owner"), 1),
}

_ADDRESS_FIELDS = {"address"}


def parse_event(line: str) -> tuple[str, dict[str, Any]] | None:
    """'openwindow>>abc123,2,kitty,~' -> ('openwindow', {...}) or None."""
    name, sep, data = line.partition(">>")
    if not sep:
        return None
    schema = _SCHEMAS.get(name)
    if schema is None:
        return name, {"data": data}
    fields, maxsplit = schema
    parts = data.split(",", maxsplit)
    payload: dict[str, Any] = {}
    # strict=False: events may legitimately carry fewer fields than the schema
    for field, value in zip(fields, parts, strict=False):
        if field in _ADDRESS_FIELDS and value and not value.startswith("0x"):
            value = "0x" + value
        payload[field] = value
    return name, payload


def _socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        raise EventError("HYPRLAND_INSTANCE_SIGNATURE not set")
    return Path(runtime) / "hypr" / sig / ".socket2.sock"


class EventStream:
    """A connection to socket2. Connect BEFORE triggering the action you
    want to observe, so the event cannot slip past."""

    def __init__(self) -> None:
        path = _socket_path()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.connect(str(path))
        except OSError as exc:
            self._sock.close()
            raise EventError(f"cannot connect to Hyprland event socket: {exc}") from exc
        self._buf = b""

    def wait_for(
        self,
        names: set[str],
        matcher: Callable[[str, dict[str, Any]], bool] | None,
        timeout: float,
    ) -> tuple[str, dict[str, Any]] | None:
        """First matching event within timeout seconds, else None."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                continue
            except OSError as exc:
                raise EventError(f"event socket died: {exc}") from exc
            if not chunk:
                raise EventError("event socket closed by compositor")
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                parsed = parse_event(line.decode(errors="replace"))
                if parsed is None:
                    continue
                name, payload = parsed
                if name in names and (matcher is None or matcher(name, payload)):
                    return name, payload

    def drain(self, settle: float = 0.08) -> list[tuple[str, dict[str, Any]]]:
        """Collect the events that arrive within `settle` seconds, then
        return them (in order). Unlike wait_for this does not block on a
        specific event; it answers 'did anything happen just now?', which
        is how a batched sequence detects the desktop changing under it."""
        out: list[tuple[str, dict[str, Any]]] = []
        deadline = time.monotonic() + settle
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return out
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                return out
            except OSError as exc:
                raise EventError(f"event socket died: {exc}") from exc
            if not chunk:
                raise EventError("event socket closed by compositor")
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                parsed = parse_event(line.decode(errors="replace"))
                if parsed is not None:
                    out.append(parsed)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> EventStream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
