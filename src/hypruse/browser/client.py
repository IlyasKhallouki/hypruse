"""A blocking client for the browser host's socket.

One request at a time per client, each with a deadline. Events that arrive while a call is
waiting are kept (the newest 50) rather than dropped, for a caller that wants them. A call
that times out closes the connection, because its answer may still arrive and would then be
read as the answer to the next call; the next call reconnects.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "hypruse" / "browser.sock"


class BrowserUnavailable(Exception):
    """No host is listening: Chrome is closed, or the extension is not loaded."""


class BrowserError(Exception):
    """The extension, or the host, answered with an error."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class BrowserClient:
    def __init__(self, path: str | Path | None = None, *, timeout: float = 3.0) -> None:
        self.path = Path(path) if path is not None else socket_path()
        self.timeout = timeout
        self.hello: dict[str, Any] = {}
        self.events: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def __enter__(self) -> BrowserClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"BrowserClient({str(self.path)!r})"

    # ------------------------------------------------------------------ connection

    def connect(self) -> dict[str, Any]:
        if self._sock is not None:
            return self.hello
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise BrowserUnavailable(
                f"no browser host at {self.path}: is Chrome running with hypruse-browser?"
            ) from exc
        self._sock, self._buffer = sock, b""
        try:
            first = self._read(time.monotonic() + self.timeout)
        except BrowserError as exc:
            self.close()
            raise BrowserUnavailable("the browser host did not say hello") from exc
        self.hello = first.get("hello", {}) if isinstance(first, dict) else {}
        return self.hello

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock, self._buffer = None, b""

    def _read(self, deadline: float) -> dict[str, Any]:
        assert self._sock is not None
        while b"\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BrowserError("timeout", "no answer in time")
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError as exc:
                raise BrowserError("timeout", "no answer in time") from exc
            except OSError as exc:
                raise BrowserError("no_extension", "the browser host went away") from exc
            if not chunk:
                raise BrowserError("no_extension", "the browser host went away")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    # ------------------------------------------------------------------ calls

    def call(
        self, op: str, args: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self.connect()
            assert self._sock is not None
            ident = f"{os.getpid()}-{next(self._ids)}"
            line = json.dumps({"id": ident, "op": op, "args": args or {}}) + "\n"
            try:
                self._sock.sendall(line.encode())
            except OSError as exc:
                self.close()
                raise BrowserError("no_extension", "the browser host went away") from exc
            deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
            try:
                while True:
                    message = self._read(deadline)
                    if "event" in message:
                        self.events.append(message)
                        continue
                    if message.get("id") == ident:
                        break
            except BrowserError:
                self.close()
                raise
        if message.get("ok"):
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        error = message.get("error") or {}
        raise BrowserError(str(error.get("code", "internal")), str(error.get("message", "")))

    # ------------------------------------------------------------------ the ops

    def ping(self) -> dict[str, Any]:
        return self.call("ping")

    def tabs(self) -> list[dict[str, Any]]:
        return list(self.call("tabs.list").get("tabs", []))

    def activate(self, tab_id: int) -> None:
        self.call("tabs.activate", {"tab_id": tab_id})

    def open(self, url: str, where: str = "new") -> int:
        return int(self.call("tabs.open", {"url": url, "where": where}).get("tab_id", 0))

    def close_tab(self, tab_id: int) -> None:
        self.call("tabs.close", {"tab_id": tab_id})

    def history(self, tab_id: int, direction: str) -> None:
        self.call("tabs.history", {"tab_id": tab_id, "direction": direction})

    def snapshot(
        self, tab_id: int | None = None, *, max: int = 120, settle_ms: int = 0
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"max": max, "settle_ms": settle_ms}
        if tab_id is not None:
            args["tab_id"] = tab_id
        # a settling snapshot waits in the page, so the deadline grows with it
        return self.call("page.snapshot", args, timeout=self.timeout + settle_ms / 1000)

    def click(self, snapshot_id: str, element_id: str) -> None:
        self.call("page.click", {"snapshot_id": snapshot_id, "element_id": element_id})

    def type(self, snapshot_id: str, element_id: str, text: str, *, submit: bool = False) -> None:
        self.call(
            "page.type",
            {"snapshot_id": snapshot_id, "element_id": element_id, "text": text, "submit": submit},
        )

    def scroll(self, direction: str, tab_id: int | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"direction": direction}
        if tab_id is not None:
            args["tab_id"] = tab_id
        return self.call("page.scroll", args)

    def top_sites(self) -> list[dict[str, Any]]:
        return list(self.call("sites.top").get("sites", []))
