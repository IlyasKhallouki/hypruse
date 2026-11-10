"""Minimal D-Bus client, for the accessibility bus.

AT-SPI reads used to run one busctl process per call: about 10 ms each,
and nearly 20 through Flatpak's D-Bus proxy, so walking a large web app's
tree (ZapZap's 904 objects) took 41 seconds. One connection held open
answers the same call in about 1.5 ms. This is only what hypruse needs: a
unix socket, EXTERNAL authentication, method calls one at a time and their
replies, little-endian messages. An address it cannot use raises
Unreachable, and the caller falls back to busctl.

Wire format (D-Bus specification, "Message Protocol"):
    header: y endianness 'l', y type, y flags, y version 1, u body length,
            u serial, a(yv) header fields, padded to 8
    body:   the arguments, marshalled per the SIGNATURE header field
Every value is aligned to its own size (structs and dict entries to 8),
counted from the start of the message.
"""

from __future__ import annotations

import contextlib
import functools
import os
import socket
import struct
import threading
import time
from typing import Any
from urllib.parse import unquote


class DBusError(RuntimeError):
    """A call did not complete: no answer in time, or the connection broke."""


class Unreachable(DBusError):
    """No connection could be made to this address."""


class CallError(DBusError):
    """The other side answered the call with a D-Bus error."""

    def __init__(self, name: str, message: str):
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name


_BUS = "org.freedesktop.DBus"
_METHOD_CALL, _METHOD_RETURN, _ERROR = 1, 2, 3
# header field codes
_PATH, _INTERFACE, _MEMBER, _ERROR_NAME, _REPLY_SERIAL, _DESTINATION = 1, 2, 3, 4, 5, 6
_SIGNATURE = 8
_MAX_MESSAGE = 1 << 27  # the specification's limit

_FIELD_SIG = {_PATH: b"o", _INTERFACE: b"s", _MEMBER: b"s", _DESTINATION: b"s",
              _SIGNATURE: b"g"}

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8,
          "h": 4, "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "{": 8, "v": 1}
_FIXED = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q",
          "t": "Q", "d": "d", "h": "I"}


def split_signature(sig: str) -> list[str]:
    """The single complete types of a signature: "a(so)ib" is ["a(so)", "i", "b"]."""
    return list(_split(sig))


@functools.lru_cache(maxsize=256)
def _split(sig: str) -> tuple[str, ...]:
    # a walk reads the same few signatures thousands of times
    out, i = [], 0
    while i < len(sig):
        j = _type_end(sig, i)
        out.append(sig[i:j])
        i = j
    return tuple(out)


def _type_end(sig: str, i: int) -> int:
    if i >= len(sig):
        raise DBusError(f"incomplete signature {sig!r}")
    c = sig[i]
    if c == "a":
        return _type_end(sig, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        j = i + 1
        while j < len(sig) and sig[j] != close:
            j = _type_end(sig, j)
        if j >= len(sig):
            raise DBusError(f"unclosed {c!r} in signature {sig!r}")
        return j + 1
    if c not in _ALIGN:
        raise DBusError(f"unknown type {c!r} in signature {sig!r}")
    return i + 1


class _Writer:
    def __init__(self, prefix: bytes = b""):
        self.buf = bytearray(prefix)

    def _pad(self, n: int) -> None:
        self.buf += b"\0" * (-len(self.buf) % n)

    def put(self, t: str, v: Any) -> None:
        c = t[0]
        self._pad(_ALIGN[c])
        if c in _FIXED:
            self.buf += struct.pack("<" + _FIXED[c], v)
        elif c in "so":
            data = str(v).encode()
            self.buf += struct.pack("<I", len(data)) + data + b"\0"
        elif c == "g":
            data = str(v).encode()
            self.buf += bytes([len(data)]) + data + b"\0"
        elif c == "v":
            sig, value = v
            self.put("g", sig)
            self.put(sig, value)
        elif c == "a":
            item = t[1:]
            at = len(self.buf)
            self.buf += b"\0\0\0\0"
            self._pad(_ALIGN[item[0]])  # even for an empty array
            start = len(self.buf)
            for x in (v.items() if item[0] == "{" else v):
                self.put(item, x)
            struct.pack_into("<I", self.buf, at, len(self.buf) - start)
        elif c in "({":
            for st, sv in zip(_split(t[1:-1]), v, strict=True):
                self.put(st, sv)


class _Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data, self.off = data, offset

    def _pad(self, n: int) -> None:
        self.off += -self.off % n

    def _need(self, n: int) -> None:
        if self.off + n > len(self.data):
            raise DBusError("truncated message")

    def get(self, t: str) -> Any:
        c = t[0]
        self._pad(_ALIGN[c])
        if c in _FIXED:
            fmt = "<" + _FIXED[c]
            size = struct.calcsize(fmt)
            self._need(size)
            (v,) = struct.unpack_from(fmt, self.data, self.off)
            self.off += size
            return bool(v) if c == "b" else v
        if c in "so":
            self._need(4)
            (n,) = struct.unpack_from("<I", self.data, self.off)
            self._need(4 + n + 1)
            v = self.data[self.off + 4:self.off + 4 + n].decode("utf-8", "replace")
            self.off += 4 + n + 1
            return v
        if c == "g":
            self._need(1)
            n = self.data[self.off]
            self._need(1 + n + 1)
            v = self.data[self.off + 1:self.off + 1 + n].decode("ascii", "replace")
            self.off += 1 + n + 1
            return v
        if c == "v":
            return self.get(self.get("g"))
        if c == "a":
            self._need(4)
            (n,) = struct.unpack_from("<I", self.data, self.off)
            self.off += 4
            item = t[1:]
            self._pad(_ALIGN[item[0]])
            end = self.off + n
            if end > len(self.data):
                raise DBusError("truncated message")
            out = []
            while self.off < end:
                out.append(self.get(item))
            return dict(out) if item[0] == "{" else out
        if c in "({":
            # a list, not a tuple: the shape busctl's JSON gave every caller
            return [self.get(st) for st in _split(t[1:-1])]
        raise DBusError(f"unknown type {c!r}")


def marshal(sig: str, args: list[Any] | tuple[Any, ...]) -> bytes:
    """The body bytes for these arguments (a body starts 8-aligned)."""
    types = _split(sig)
    if len(types) != len(args):
        raise DBusError(f"signature {sig!r} takes {len(types)} arguments, got {len(args)}")
    w = _Writer()
    for t, v in zip(types, args, strict=True):
        w.put(t, v)
    return bytes(w.buf)


def unmarshal(sig: str, data: bytes, offset: int = 0) -> list[Any]:
    fast = _FAST_BODIES.get(sig)
    if fast is not None:
        return fast(data, offset)
    r = _Reader(data, offset)
    return [r.get(t) for t in _split(sig)]


# The replies an accessibility walk reads thousands of times, unpacked
# without the generic reader's recursion. Each returns exactly what
# _Reader would; tests hold them to it.
_U32 = struct.Struct("<I")


def _str_at(data: bytes, off: int) -> tuple[str, int]:
    """A string or object path at an already 4-aligned offset, and the offset after it."""
    if off + 4 > len(data):
        raise DBusError("truncated message")
    (n,) = _U32.unpack_from(data, off)
    end = off + 4 + n
    if end + 1 > len(data):
        raise DBusError("truncated message")
    return data[off + 4:end].decode("utf-8", "replace"), end + 1


def _body_u(data: bytes, off: int) -> list[Any]:
    off += -off % 4
    if off + 4 > len(data):
        raise DBusError("truncated message")
    return [_U32.unpack_from(data, off)[0]]


def _body_s(data: bytes, off: int) -> list[Any]:
    return [_str_at(data, off + (-off % 4))[0]]


def _body_v(data: bytes, off: int) -> list[Any]:
    # a property value: the common case is a string; anything else goes the
    # generic way
    if off + 3 <= len(data) and data[off] == 1 and data[off + 1:off + 3] == b"s\0":
        at = off + 3
        return [_str_at(data, at + (-at % 4))[0]]
    return [_Reader(data, off).get("v")]


def _body_a_so(data: bytes, off: int) -> list[Any]:
    off += -off % 4
    if off + 4 > len(data):
        raise DBusError("truncated message")
    (n,) = _U32.unpack_from(data, off)
    off += 4
    off += -off % 8
    end = off + n
    if end > len(data):
        raise DBusError("truncated message")
    out = []
    while off < end:
        off += -off % 8
        svc, off = _str_at(data, off)
        path, off = _str_at(data, off + (-off % 4))
        out.append([svc, path])
    return [out]


_FAST_BODIES = {"u": _body_u, "s": _body_s, "v": _body_v, "a(so)": _body_a_so}


def _call_header(serial: int, body_len: int, dest: str, path: str, iface: str,
                 member: str, sig: str) -> bytes:
    """A method call's header, padded to 8: the bytes _Writer.put("a(yv)")
    writes for these fields, in the same order."""
    fields = bytearray()
    for code, value in ((_PATH, path), (_INTERFACE, iface), (_MEMBER, member),
                        (_DESTINATION, dest), (_SIGNATURE, sig)):
        if code == _SIGNATURE and not value:
            continue
        fields += b"\0" * (-len(fields) % 8)  # each field struct, counted from offset 16
        data = value.encode()
        if code == _SIGNATURE:
            fields += bytes([code, 1]) + b"g\0" + bytes([len(data)]) + data + b"\0"
        else:
            fields += bytes([code, 1]) + _FIELD_SIG[code] + b"\0"
            fields += b"\0" * (-(16 + len(fields)) % 4)
            fields += _U32.pack(len(data)) + data + b"\0"
    head = (b"l" + bytes([_METHOD_CALL, 0, 1]) + struct.pack("<III", body_len, serial, len(fields))
            + bytes(fields))
    return head + b"\0" * (-len(head) % 8)


def _reply_fields(msg: bytes) -> tuple[int | None, str, str]:
    """reply serial, signature and error name of a message's header, or
    the generic reader's answer for a field this does not expect."""
    serial, sig, err = None, "", ""
    (n,) = _U32.unpack_from(msg, 12)
    off, end = 16, 16 + n
    if end > len(msg):
        raise DBusError("truncated message")
    while off < end:
        off += -off % 8
        code = msg[off]
        vsig_len = msg[off + 1]
        vsig = msg[off + 2:off + 2 + vsig_len]
        off += 3 + vsig_len
        if vsig == b"u":
            off += -off % 4
            value: Any = _U32.unpack_from(msg, off)[0]
            off += 4
        elif vsig in (b"s", b"o"):
            value, off = _str_at(msg, off + (-off % 4))
        elif vsig == b"g":
            k = msg[off]
            value = msg[off + 1:off + 1 + k].decode("ascii", "replace")
            off += k + 2
        else:
            r = _Reader(msg, off)
            value = r.get(vsig.decode("ascii", "replace"))
            off = r.off
        if code == _REPLY_SERIAL:
            serial = value
        elif code == _SIGNATURE:
            sig = value
        elif code == _ERROR_NAME:
            err = value
    return serial, sig, err


def socket_path(address: str) -> str:
    """The socket of the first unix entry of a bus address, an abstract one
    with its leading NUL. Raises Unreachable for anything else."""
    for entry in address.split(";"):
        kind, _, params = entry.partition(":")
        if kind != "unix":
            continue
        keys = dict(p.split("=", 1) for p in params.split(",") if "=" in p)
        if "path" in keys:
            return unquote(keys["path"])
        if "abstract" in keys:
            return "\0" + unquote(keys["abstract"])
    raise Unreachable(f"no unix socket in the bus address {address!r}")


class Connection:
    """One authenticated connection to a message bus. Calls run one at a
    time (a lock, since the server runs tools on threads), each waiting for
    its own reply. A call that times out or breaks closes the connection,
    so a late reply can never be taken for the next call's."""

    def __init__(self, address: str, timeout: float = 5.0):
        self.closed = False
        self._buf = bytearray()
        self._serial = 0
        self._lock = threading.Lock()
        path = socket_path(address)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(path)
            self._authenticate()
            self.unique_name = self.call(_BUS, "/org/freedesktop/DBus", _BUS, "Hello",
                                         timeout=timeout)[0]
        except (OSError, DBusError) as exc:
            self.close()
            raise Unreachable(f"cannot connect to the bus at {address!r}: {exc}") from None

    def _authenticate(self) -> None:
        uid = str(os.getuid()).encode().hex().encode()
        self._sock.sendall(b"\0AUTH EXTERNAL " + uid + b"\r\n")
        line = self._line()
        if not line.startswith(b"OK "):
            raise DBusError(f"the bus refused authentication ({line.decode(errors='replace')})")
        self._sock.sendall(b"BEGIN\r\n")

    def _line(self) -> bytes:
        while b"\r\n" not in self._buf:
            if len(self._buf) > 16384:
                raise DBusError("no end to the bus's authentication reply")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise DBusError("the bus closed the connection")
            self._buf += chunk
        line, _, rest = bytes(self._buf).partition(b"\r\n")
        self._buf = bytearray(rest)
        return line

    def close(self) -> None:
        self.closed = True
        with contextlib.suppress(OSError):
            self._sock.close()

    def call(self, dest: str, path: str, iface: str, member: str, sig: str = "",
             args: list[Any] | tuple[Any, ...] = (), timeout: float = 10.0) -> list[Any]:
        """The reply's arguments as a list. Raises CallError for an error
        reply (the connection stays usable), DBusError when no reply came
        (the connection is closed)."""
        body = marshal(sig, args)
        with self._lock:
            if self.closed:
                raise DBusError("the connection is closed")
            self._serial += 1
            serial = self._serial
            head = _call_header(serial, len(body), dest, path, iface, member, sig)
            try:
                self._sock.settimeout(timeout)
                self._sock.sendall(head + body)
                return self._reply(serial, time.monotonic() + timeout)
            except (OSError, DBusError) as exc:
                if isinstance(exc, CallError):
                    raise
                self.close()
                if isinstance(exc, TimeoutError):
                    raise DBusError(f"no answer to {member} within {timeout:g} s (the app "
                                    "may still act on it)") from None
                raise DBusError(f"{member}: {exc}") from None

    def _reply(self, serial: int, deadline: float) -> list[Any]:
        while True:
            msg, body_at = self._message(deadline)
            kind = msg[1]
            reply_to, sig, err = _reply_fields(msg)
            if kind not in (_METHOD_RETURN, _ERROR) or reply_to != serial:
                continue  # a signal (NameAcquired), or nothing of ours
            values = unmarshal(sig, msg, body_at)
            if kind == _ERROR:
                text = values[0] if values and isinstance(values[0], str) else ""
                raise CallError(err or "error", text)
            return values

    def call_many(self, calls: list[tuple[str, str, str, str, str, tuple[Any, ...]]],
                  timeout: float = 10.0) -> list[Any]:
        """Several calls sent together, their replies read as they come:
        one round trip instead of one per call. calls holds (dest, path,
        iface, member, sig, args); the result holds, in the same order, each
        reply's arguments as a list, or the CallError it answered with.
        `timeout` is the longest wait for the next reply, not for all of
        them. A lost connection raises DBusError for the batch and closes
        it, as call does."""
        if not calls:
            return []
        bodies = [marshal(sig, args) for _d, _p, _i, _m, sig, args in calls]
        with self._lock:
            if self.closed:
                raise DBusError("the connection is closed")
            first = self._serial + 1
            out = bytearray()
            for (dest, path, iface, member, sig, _args), body in zip(calls, bodies, strict=True):
                self._serial += 1
                out += _call_header(self._serial, len(body), dest, path, iface, member, sig)
                out += body
            last = self._serial
            results: list[Any] = [None] * len(calls)
            pending = len(calls)
            deadline = time.monotonic() + timeout
            try:
                self._sock.settimeout(timeout)
                self._sock.sendall(out)
                while pending:
                    msg, body_at = self._message(deadline)
                    kind = msg[1]
                    reply_to, sig, err = _reply_fields(msg)
                    ours = (kind in (_METHOD_RETURN, _ERROR) and reply_to is not None
                            and first <= reply_to <= last and results[reply_to - first] is None)
                    if not ours:
                        continue  # a signal, or a reply to an earlier call
                    values = unmarshal(sig, msg, body_at)
                    if kind == _ERROR:
                        text = values[0] if values and isinstance(values[0], str) else ""
                        values = CallError(err or "error", text)
                    results[reply_to - first] = values
                    pending -= 1
                    # each reply restarts the clock: a batch tolerates the
                    # same slow app the same calls one at a time would
                    deadline = time.monotonic() + timeout
                return results
            except (OSError, DBusError) as exc:
                self.close()
                if isinstance(exc, TimeoutError):
                    raise DBusError(
                        f"no answer to {len(calls)} calls within {timeout:g} s") from None
                raise DBusError(f"{calls[0][3]}: {exc}") from None

    def _message(self, deadline: float) -> tuple[bytes, int]:
        while True:
            if len(self._buf) >= 16:
                if self._buf[0:1] != b"l":
                    raise DBusError("a big-endian message, which this client does not read")
                body_len, _, fields_len = struct.unpack_from("<III", self._buf, 4)
                body_at = 16 + fields_len + (-(16 + fields_len) % 8)
                total = body_at + body_len
                if total > _MAX_MESSAGE:
                    raise DBusError("a message larger than D-Bus allows")
                if len(self._buf) >= total:
                    msg = bytes(self._buf[:total])
                    del self._buf[:total]
                    return msg, body_at
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self._sock.settimeout(left)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise DBusError("the bus closed the connection")
            self._buf += chunk


_connections: dict[str, Connection] = {}
_connections_lock = threading.Lock()


def connection(address: str) -> Connection:
    """The process's connection to this bus, opened on first use and again
    after one breaks. Raises Unreachable."""
    with _connections_lock:
        conn = _connections.get(address)
        if conn is None or conn.closed:
            conn = _connections[address] = Connection(address)
        return conn
