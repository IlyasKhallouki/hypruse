"""Chrome native messaging frames: a 32-bit length in native byte order, then UTF-8 JSON.

Native order is little endian on every machine hypruse runs on, so it is written as such.
Chrome refuses a message from the host larger than 1 MB and sends the host up to 64 MiB.
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

MAX_TO_BROWSER = 1024 * 1024
MAX_FROM_BROWSER = 64 * 1024 * 1024
_LENGTH = struct.Struct("<I")


class FrameTooLarge(ValueError):
    """A message past what Chrome allows in that direction."""


def encode(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_TO_BROWSER:
        raise FrameTooLarge(f"{len(body)} bytes is over Chrome's {MAX_TO_BROWSER} byte limit")
    return _LENGTH.pack(len(body)) + body


def _exactly(stream: BinaryIO, count: int) -> bytes | None:
    """`count` bytes, or None when the stream ended first."""
    chunks: list[bytes] = []
    while count:
        chunk = stream.read(count)
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read(stream: BinaryIO) -> dict[str, Any] | None:
    """The next message, or None at the end of the stream (Chrome closed the pipe)."""
    head = _exactly(stream, _LENGTH.size)
    if head is None:
        return None
    (length,) = _LENGTH.unpack(head)
    if length > MAX_FROM_BROWSER:
        raise FrameTooLarge(f"a frame claiming {length} bytes")
    body = _exactly(stream, length)
    if body is None:
        return None
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError("a native message must be a JSON object")
    return message
