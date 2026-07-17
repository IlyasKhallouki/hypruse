import struct

from hypruse import wire


def test_wl_string_padding():
    assert wire.wl_string("a") == struct.pack("<I", 2) + b"a\x00" + b"\x00\x00"
    assert wire.wl_string("abc") == struct.pack("<I", 4) + b"abc\x00"
    assert wire.wl_string("") == struct.pack("<I", 1) + b"\x00" + b"\x00\x00\x00"
    assert len(wire.wl_string("hello world")) % 4 == 0


def test_to_fixed_24_8():
    assert wire.to_fixed(1.0) == 256
    assert wire.to_fixed(-1.5) == -384
    assert wire.to_fixed(0.0) == 0
    assert wire.to_fixed(15.0) == 3840


def test_encode_msg_header():
    msg = wire.encode_msg(7, 4)
    obj, sizeop = struct.unpack("<II", msg)
    assert obj == 7
    assert sizeop >> 16 == 8  # header-only size
    assert sizeop & 0xFFFF == 4

    body = struct.pack("<III", 1, 2, 3)
    msg = wire.encode_msg(3, 2, body)
    _, sizeop = struct.unpack_from("<II", msg)
    assert sizeop >> 16 == 8 + 12


def test_parse_events_handles_partials():
    a = wire.encode_msg(1, 0, b"AAAA")
    b = wire.encode_msg(2, 1, b"BBBBBBBB")
    stream = a + b
    events, rest = wire.parse_events(stream + b[:5])  # trailing partial copy
    assert [(o, op) for o, op, _ in events] == [(1, 0), (2, 1)]
    assert events[1][2] == b"BBBBBBBB"
    assert rest == b[:5]


def test_parse_global_roundtrip():
    body = (
        struct.pack("<I", 33)
        + wire.wl_string("zwlr_virtual_pointer_manager_v1")
        + struct.pack("<I", 2)
    )
    name, interface, version = wire.parse_global(body)
    assert (name, interface, version) == (33, "zwlr_virtual_pointer_manager_v1", 2)


def test_parse_error_roundtrip():
    body = struct.pack("<II", 5, 1) + wire.wl_string("invalid axis")
    assert wire.parse_error(body) == (5, 1, "invalid axis")


def test_linux_button_codes():
    assert wire.BUTTONS["left"] == 0x110
    assert wire.BUTTONS["right"] == 0x111


def test_scroll_messages_whole_notches_are_discrete():
    msgs = wire.scroll_messages(dy=3)
    opcodes = [op for op, _ in msgs]
    assert opcodes == [wire.PTR_AXIS_SOURCE, wire.PTR_AXIS_DISCRETE, wire.PTR_FRAME]
    _, axis, value, discrete = struct.unpack("<IIii", msgs[1][1])
    assert (axis, discrete) == (wire.AXIS_VERTICAL, 3)
    assert value == wire.to_fixed(3 * wire.SCROLL_UNITS_PER_NOTCH)


def test_scroll_messages_negative_notch():
    msgs = wire.scroll_messages(dy=-1)
    _, axis, value, discrete = struct.unpack("<IIii", msgs[1][1])
    assert (axis, discrete) == (wire.AXIS_VERTICAL, -1)
    assert value == wire.to_fixed(-wire.SCROLL_UNITS_PER_NOTCH)


def test_scroll_messages_fractional_stays_continuous():
    msgs = wire.scroll_messages(dy=0.5)
    opcodes = [op for op, _ in msgs]
    assert opcodes == [wire.PTR_AXIS_SOURCE, wire.PTR_AXIS, wire.PTR_FRAME]
    _, axis, value = struct.unpack("<IIi", msgs[1][1])
    assert axis == wire.AXIS_VERTICAL
    assert value == wire.to_fixed(0.5 * wire.SCROLL_UNITS_PER_NOTCH)


def test_scroll_messages_v1_compositor_stays_continuous():
    """axis_discrete is since=2; sending it to a v1-bound pointer is a
    protocol error that kills the connection."""
    msgs = wire.scroll_messages(dy=3, discrete_ok=False)
    opcodes = [op for op, _ in msgs]
    assert wire.PTR_AXIS_DISCRETE not in opcodes
    assert opcodes == [wire.PTR_AXIS_SOURCE, wire.PTR_AXIS, wire.PTR_FRAME]
    _, axis, value = struct.unpack("<IIi", msgs[1][1])
    assert (axis, value) == (wire.AXIS_VERTICAL, wire.to_fixed(3 * wire.SCROLL_UNITS_PER_NOTCH))


def test_scroll_uses_bound_version(monkeypatch):
    """The pointer inherits the manager's version: only bind >= 2 may
    speak axis_discrete."""
    sent = []

    class FakePointer(wire.VirtualPointer):
        def __init__(self, version):
            self._version = version
            self._pointer = 42

        def _send(self, obj, opcode, body=b""):
            sent.append(opcode)

        def _roundtrip(self, collect_globals=False):
            return []

    FakePointer(2).scroll(dy=1)
    assert wire.PTR_AXIS_DISCRETE in sent
    sent.clear()
    FakePointer(1).scroll(dy=1)
    assert wire.PTR_AXIS_DISCRETE not in sent
    assert wire.PTR_AXIS in sent


def test_scroll_messages_both_axes_one_frame():
    msgs = wire.scroll_messages(dy=1, dx=-2)
    opcodes = [op for op, _ in msgs]
    assert opcodes == [
        wire.PTR_AXIS_SOURCE,
        wire.PTR_AXIS_DISCRETE,
        wire.PTR_AXIS_DISCRETE,
        wire.PTR_FRAME,
    ]
    _, axis_h, _, discrete_h = struct.unpack("<IIii", msgs[2][1])
    assert (axis_h, discrete_h) == (wire.AXIS_HORIZONTAL, -2)
    assert wire.BUTTONS["middle"] == 0x112
