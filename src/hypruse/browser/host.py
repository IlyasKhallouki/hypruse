"""`hypruse browser-host`: the relay Chrome starts for the hypruse-browser extension.

One side is Chrome's pipe (native messaging frames on stdin and stdout). The other is a unix
socket in `$XDG_RUNTIME_DIR/hypruse/`, mode 0600 in a 0700 directory, where any number of
local clients speak newline-delimited JSON. The host holds no state beyond the requests in
flight:

- a client's request id is swapped for one of the host's own before it reaches the
  extension, and swapped back on the way out, so two clients can never collide;
- the extension's events go to every client;
- when Chrome closes the pipe the host removes its socket and exits.

Nothing but frames may ever be written to stdout: Chrome reads every byte of it as a frame.
Diagnostics go to stderr, and never include what a request asked for, since a request can
carry text the owner typed.

The socket is bound only after the extension has said hello, so a client that connects
always learns which extension and browser it is talking to. Each Chrome profile with the
extension starts a host of its own, and sees only its own windows, so a host that finds the
first socket taken takes the next free one (`client.slot_paths`) rather than stepping aside:
the owner works in more than one profile.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

from hypruse.browser import framing
from hypruse.browser.client import slot_paths

PROTOCOL = 1
SERVED = 0
ALREADY_SERVED = 1
NO_EXTENSION = 2

# how long to wait for the extension's hello before giving up on this launch
HELLO_WAIT_S = 5.0
MAX_LINE = framing.MAX_TO_BROWSER
MAX_ID = 128
MAX_OP = 64


def _log(message: str) -> None:
    print(f"hypruse browser-host: {message}", file=sys.stderr, flush=True)


class _Client:
    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.lock = threading.Lock()
        self.alive = True

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with self.lock:
            if not self.alive:
                return
            try:
                self.conn.sendall(line)
            except OSError:
                self.alive = False


class _Relay:
    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.out_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.hello: dict[str, Any] = {}
        self.said_hello = threading.Event()
        self.closed = threading.Event()
        self.clients: list[_Client] = []
        self.pending: dict[str, tuple[_Client, Any]] = {}
        self.ids = itertools.count(1)

    # ------------------------------------------------------------------ the browser side

    def read_browser(self) -> None:
        try:
            while True:
                message = framing.read(self.stdin)
                if message is None:
                    break
                self._from_browser(message)
        except (OSError, ValueError) as exc:
            _log(f"the browser pipe failed: {type(exc).__name__}")
        finally:
            self.closed.set()
            self.said_hello.set()

    def _from_browser(self, message: dict[str, Any]) -> None:
        if "hello" in message and isinstance(message["hello"], dict):
            self.hello = message["hello"]
            self.said_hello.set()
            return
        if "event" in message:
            with self.state_lock:
                clients = list(self.clients)
            for client in clients:
                client.send(message)
            return
        hid = message.get("id")
        with self.state_lock:
            waiting = self.pending.pop(hid, None) if isinstance(hid, str) else None
        if waiting is None:
            return
        client, original = waiting
        client.send({**message, "id": original})

    def to_browser(self, message: dict[str, Any]) -> None:
        frame = framing.encode(message)
        with self.out_lock:
            self.stdout.write(frame)
            self.stdout.flush()

    # ------------------------------------------------------------------ the client side

    def greet(self, client: _Client) -> None:
        client.send(
            {
                "hello": {
                    "protocol": PROTOCOL,
                    "connected": not self.closed.is_set() and bool(self.hello),
                    "extension": str(self.hello.get("extension", "")),
                    "browser": str(self.hello.get("browser", "")),
                }
            }
        )

    def serve_client(self, client: _Client) -> None:
        with self.state_lock:
            self.clients.append(client)
        self.greet(client)
        buffered = b""
        try:
            while client.alive:
                chunk = client.conn.recv(65536)
                if not chunk:
                    break
                buffered += chunk
                if len(buffered) > MAX_LINE and b"\n" not in buffered:
                    client.send(_error(None, "bad_request", "a request line is too long"))
                    break
                while b"\n" in buffered:
                    line, buffered = buffered.split(b"\n", 1)
                    if line.strip():
                        self._request(client, line)
        except OSError:
            pass
        finally:
            client.alive = False
            with self.state_lock:
                if client in self.clients:
                    self.clients.remove(client)
                for hid in [h for h, (c, _) in self.pending.items() if c is client]:
                    del self.pending[hid]
            with contextlib.suppress(OSError):
                client.conn.close()

    def _request(self, client: _Client, line: bytes) -> None:
        try:
            request = json.loads(line)
        except ValueError:
            client.send(_error(None, "bad_request", "a request must be one line of JSON"))
            return
        if not isinstance(request, dict):
            client.send(_error(None, "bad_request", "a request must be a JSON object"))
            return
        original = request.get("id")
        op = request.get("op")
        args = request.get("args", {})
        if not isinstance(original, (str, int)) or len(str(original)) > MAX_ID:
            client.send(_error(original, "bad_request", "a request needs an id"))
            return
        if not isinstance(op, str) or not op or len(op) > MAX_OP:
            client.send(_error(original, "bad_request", "a request needs an op"))
            return
        if not isinstance(args, dict):
            client.send(_error(original, "bad_request", "args must be an object"))
            return
        if self.closed.is_set():
            client.send(_error(original, "no_extension", "the browser has gone away"))
            return
        hid = f"h{next(self.ids)}"
        with self.state_lock:
            self.pending[hid] = (client, original)
        try:
            self.to_browser({"id": hid, "op": op, "args": args})
        except framing.FrameTooLarge:
            with self.state_lock:
                self.pending.pop(hid, None)
            client.send(_error(original, "bad_request", "the request is too large for Chrome"))
        except OSError:
            with self.state_lock:
                self.pending.pop(hid, None)
            client.send(_error(original, "no_extension", "the browser has gone away"))

    def close_clients(self) -> None:
        with self.state_lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            client.alive = False
            with contextlib.suppress(OSError):
                client.conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                client.conn.close()


def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def _live(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _bind(path: Path) -> socket.socket:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid == os.getuid():
        os.chmod(path.parent, 0o700)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        server.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    server.listen(16)
    server.settimeout(0.2)
    return server


def serve(stdin: BinaryIO, stdout: BinaryIO, path: Path | None = None) -> int:
    """Relay until Chrome closes the pipe. Returns SERVED, ALREADY_SERVED or NO_EXTENSION.

    `path` is the first slot; this host takes the first one no live host holds.
    """
    free = [slot for slot in slot_paths(path) if not (slot.exists() and _live(slot))]
    if not free:
        _log("every browser slot is taken; leaving them alone")
        return ALREADY_SERVED
    path = free[0]
    relay = _Relay(stdin, stdout)
    reader = threading.Thread(target=relay.read_browser, name="browser-pipe", daemon=True)
    reader.start()
    relay.said_hello.wait(HELLO_WAIT_S)
    if relay.closed.is_set() or not relay.hello:
        _log("the extension did not say hello")
        return NO_EXTENSION
    server = _bind(path)
    try:
        while not relay.closed.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            conn.settimeout(None)
            client = _Client(conn)
            threading.Thread(
                target=relay.serve_client, args=(client,), name="browser-client", daemon=True
            ).start()
    finally:
        server.close()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        relay.close_clients()
    return SERVED


def main(argv: list[str]) -> int:
    """Chrome passes the extension's origin as the first argument; nothing here needs it."""
    del argv
    code = serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0 if code in (SERVED, ALREADY_SERVED) else 1
