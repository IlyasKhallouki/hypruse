"""The D-Bus client: the codec against hand-checked bytes and round trips,
replies matched to their call, and a real bus (a private dbus-daemon, never
the desktop's) for authentication and the calls a11y makes."""

import os
import shutil
import socket
import subprocess
import threading

import pytest

from hypruse import a11y, dbuswire

# bound at import time, so conftest's autouse stub does not hide it
from hypruse.dbuswire import connection as real_connection  # noqa: E402

_BUS = "org.freedesktop.DBus"
_BUS_PATH = "/org/freedesktop/DBus"

# --- codec ------------------------------------------------------------------


def test_signatures_split_into_complete_types():
    assert dbuswire.split_signature("a(so)ib") == ["a(so)", "i", "b"]
    assert dbuswire.split_signature("a(ua(so))") == ["a(ua(so))"]
    assert dbuswire.split_signature("a{sv}s") == ["a{sv}", "s"]
    with pytest.raises(dbuswire.DBusError):
        dbuswire.split_signature("a(so")


def test_values_are_aligned_to_their_size():
    assert dbuswire.marshal("yu", [1, 2]) == b"\x01\0\0\0\x02\0\0\0"
    # a string's length, its bytes, a NUL, then the next value aligned
    assert dbuswire.marshal("si", ["ab", 7]) == b"\x02\0\0\0ab\0\0\x07\0\0\0"


def test_an_empty_array_of_structs_still_pads_to_its_element():
    data = dbuswire.marshal("a(so)i", [[], 5])
    assert data == b"\0\0\0\0" + b"\0" * 4 + b"\x05\0\0\0"
    assert dbuswire.unmarshal("a(so)i", data) == [[], 5]


@pytest.mark.parametrize(("sig", "value"), [
    ("s", "héllo ✓"), ("o", "/org/a11y/atspi/accessible/2147484444"), ("g", "a(so)"),
    ("i", -3), ("u", 4000000000), ("b", True), ("y", 255), ("x", -(2**40)), ("d", 1.5),
    ("as", ["a", "bc"]), ("au", [8, 0]), ("a(so)", [[":1.5", "/a"], [":1.6", "/b"]]),
    ("(iiii)", [1, -2, 300, 40]), ("a(ua(so))", [[1, [[":1.5", "/l"]]], [2, []]]),
    ("a{ss}", {"tag": "div", "id": "x"}),
])
def test_every_type_in_use_round_trips(sig, value):
    assert dbuswire.unmarshal(sig, dbuswire.marshal(sig, [value])) == [value]


def test_a_variant_reads_as_its_value():
    data = dbuswire.marshal("v", [("a(so)", [[":1.5", "/a"]])])
    assert dbuswire.unmarshal("v", data) == [[[":1.5", "/a"]]]


def test_structs_read_as_lists_the_shape_busctl_gave():
    assert dbuswire.unmarshal("(ii)", dbuswire.marshal("(ii)", [(1, 2)])) == [[1, 2]]


def test_a_truncated_message_is_an_error_not_a_short_value():
    data = dbuswire.marshal("s", ["hello"])
    with pytest.raises(dbuswire.DBusError, match="truncated"):
        dbuswire.unmarshal("s", data[:5])


def test_socket_addresses():
    assert dbuswire.socket_path("unix:path=/run/user/1000/at-spi/bus_1") == (
        "/run/user/1000/at-spi/bus_1")
    assert dbuswire.socket_path("unix:abstract=/tmp/dbus-X,guid=ab") == "\0/tmp/dbus-X"
    assert dbuswire.socket_path("tcp:host=x,port=1;unix:path=/a%2cb") == "/a,b"
    with pytest.raises(dbuswire.Unreachable):
        dbuswire.socket_path("tcp:host=localhost,port=1")


# --- replies matched to their call, over a socket pair ----------------------


def _reply(serial, sig="", args=(), error=""):
    body = dbuswire.marshal(sig, args)
    fields = [(5, ("u", serial))]
    if error:
        fields.append((4, ("s", error)))
    if sig:
        fields.append((8, ("g", sig)))
    w = dbuswire._Writer(b"l" + bytes([3 if error else 2, 0, 1])
                         + dbuswire.struct.pack("<II", len(body), 1000 + serial))
    w.put("a(yv)", fields)
    w._pad(8)
    return bytes(w.buf) + body


def _paired():
    ours, theirs = socket.socketpair()
    conn = dbuswire.Connection.__new__(dbuswire.Connection)
    conn.closed, conn._buf, conn._serial, conn._lock = False, bytearray(), 0, threading.Lock()
    conn._sock = ours
    return conn, theirs


def test_a_stale_reply_is_skipped_for_the_one_asked_for():
    conn, peer = _paired()
    peer.sendall(_reply(99, "s", ["old"]) + _reply(1, "s", ["mine"]))
    assert conn.call("x", "/p", "i", "M") == ["mine"]


def test_an_error_reply_raises_and_the_connection_stays_usable():
    conn, peer = _paired()
    peer.sendall(_reply(1, "s", ["no such object"], error="org.a11y.Error.Gone"))
    with pytest.raises(dbuswire.CallError, match="Gone: no such object") as err:
        conn.call("x", "/p", "i", "M")
    assert err.value.name == "org.a11y.Error.Gone" and not conn.closed
    peer.sendall(_reply(2, "b", [True]))
    assert conn.call("x", "/p", "i", "M") == [True]


def test_no_answer_closes_the_connection_so_a_late_reply_is_never_misread():
    conn, peer = _paired()
    with pytest.raises(dbuswire.DBusError, match="no answer to M") as err:
        conn.call("x", "/p", "i", "M", timeout=0.05)
    assert not isinstance(err.value, dbuswire.CallError) and conn.closed
    with pytest.raises(dbuswire.DBusError, match="closed"):
        conn.call("x", "/p", "i", "M")


def test_a_closed_peer_is_an_error():
    conn, peer = _paired()
    peer.close()
    with pytest.raises(dbuswire.DBusError):
        conn.call("x", "/p", "i", "M", timeout=1)
    assert conn.closed


def test_a_big_endian_message_is_refused_not_misread():
    conn, peer = _paired()
    peer.sendall(b"B" + _reply(1, "s", ["x"])[1:])
    with pytest.raises(dbuswire.DBusError, match="big-endian"):
        conn.call("x", "/p", "i", "M", timeout=1)


def test_the_call_carries_its_arguments():
    conn, peer = _paired()
    peer.sendall(_reply(1, "b", [True]))
    conn.call("app", "/obj", "org.a11y.atspi.EditableText", "InsertText", "isi", [2, "é", 2])
    sent = peer.recv(65536)
    body_len, _, fields_len = dbuswire.struct.unpack_from("<III", sent, 4)
    fields = dict(dbuswire._Reader(sent, 12).get("a(yv)"))
    assert fields[1] == "/obj" and fields[3] == "InsertText" and fields[6] == "app"
    assert fields[8] == "isi"
    body_at = 16 + fields_len + (-(16 + fields_len) % 8)
    assert dbuswire.unmarshal("isi", sent, body_at) == [2, "é", 2]


# --- fast paths held to the generic codec ------------------------------------


def _old_header(serial, body_len, dest, path, iface, member, sig):
    fields = [(1, ("o", path)), (2, ("s", iface)), (3, ("s", member)), (6, ("s", dest))]
    if sig:
        fields.append((8, ("g", sig)))
    w = dbuswire._Writer(b"l" + bytes([1, 0, 1]) + dbuswire.struct.pack("<II", body_len, serial))
    w.put("a(yv)", fields)
    w._pad(8)
    return bytes(w.buf)


@pytest.mark.parametrize("dest, path, iface, member, sig", [
    ("x", "/p", "i", "M", ""),
    (":1.42", "/org/a11y/atspi/accessible/1234", "org.freedesktop.DBus.Properties", "Get", "ss"),
    ("org.a11y.Bus", "/", "org.a11y.atspi.Accessible", "GetChildren", ""),
    ("a" * 7, "/" + "b" * 13, "c" * 5, "d" * 3, "isi"),
    ("é", "/ü", "ï", "ô", "a(so)"),
])
def test_the_call_header_is_the_bytes_the_generic_writer_makes(dest, path, iface, member, sig):
    for serial, body_len in ((1, 0), (77, 12), (2**31, 999)):
        assert dbuswire._call_header(serial, body_len, dest, path, iface, member, sig) == \
            _old_header(serial, body_len, dest, path, iface, member, sig)


def _message(kind, fields, sig="", args=()):
    body = dbuswire.marshal(sig, args)
    w = dbuswire._Writer(b"l" + bytes([kind, 0, 1]) + dbuswire.struct.pack("<II", len(body), 5))
    w.put("a(yv)", fields + ([(8, ("g", sig))] if sig else []))
    w._pad(8)
    return bytes(w.buf) + body


@pytest.mark.parametrize("kind, fields, sig", [
    (2, [(5, ("u", 9)), (7, ("s", ":1.5"))], "s"),
    (2, [(7, ("s", ":1.5")), (6, ("s", ":1.9")), (5, ("u", 2**32 - 1))], ""),
    (3, [(4, ("s", "org.a11y.Error.Gone")), (5, ("u", 3))], "s"),
    (4, [(1, ("o", "/org/a11y")), (2, ("s", "org.a11y.atspi.Event")), (3, ("s", "Changed"))], "s"),
    (2, [(5, ("u", 1)), (9, ("u", 2))], "a(so)"),  # unix fds: a field it does not use
])
def test_the_reply_fields_are_what_the_generic_reader_finds(kind, fields, sig):
    msg = _message(kind, fields, sig, [[] if sig == "a(so)" else "x"] if sig else [])
    generic = dict(dbuswire._Reader(msg, 12).get("a(yv)"))
    assert dbuswire._reply_fields(msg) == (generic.get(5), generic.get(8, ""), generic.get(4, ""))


@pytest.mark.parametrize("sig, value", [
    ("u", 0), ("u", 2**32 - 1),
    ("s", ""), ("s", "push button"), ("s", "日本語 ✓"),
    ("v", ("s", "")), ("v", ("s", "Save as…")), ("v", ("u", 7)), ("v", ("b", True)),
    ("v", ("a(so)", [[":1.2", "/a"]])),
    ("a(so)", []), ("a(so)", [[":1.2", "/a"]]),
    ("a(so)", [[":1." + str(i), "/org/a11y/atspi/accessible/" + "x" * (i % 9)] for i in range(40)]),
])
def test_the_fast_bodies_read_what_the_generic_reader_reads(sig, value):
    for pad in (0, 8, 16, 24):  # a body starts 8-aligned, after a header of any length
        data = b"\0" * pad + dbuswire.marshal(sig, [value])
        reference = [dbuswire._Reader(data, pad).get(t) for t in dbuswire.split_signature(sig)]
        assert dbuswire._FAST_BODIES[sig](data, pad) == reference
    body = dbuswire.marshal(sig, [value])
    assert dbuswire.unmarshal(sig, body) == [dbuswire._Reader(body).get(sig)]


@pytest.mark.parametrize("sig, value", [("u", 5), ("s", "abc"), ("v", ("s", "abc")),
                                        ("a(so)", [[":1.2", "/a"], [":1.3", "/bb"]])])
def test_a_truncated_body_is_an_error_on_the_fast_paths_too(sig, value):
    body = dbuswire.marshal(sig, [value])
    for cut in range(1, len(body)):
        with pytest.raises(dbuswire.DBusError):
            dbuswire.unmarshal(sig, body[:-cut])


# --- several calls in one round trip -------------------------------------------


def test_a_batch_returns_each_reply_in_call_order_whatever_order_they_come_in():
    conn, peer = _paired()
    peer.sendall(_reply(3, "u", [30]) + _reply(1, "s", ["one"])
                 + _reply(2, "a(so)", [[[":1.2", "/k"]]]))
    out = conn.call_many([("x", "/a", "i", "M", "", ()), ("x", "/b", "i", "M", "", ()),
                          ("x", "/c", "i", "M", "", ())])
    assert out == [["one"], [[[":1.2", "/k"]]], [30]]
    sent = peer.recv(65536)
    assert sent.count(b"\x03\x01s\0") == 3  # three member fields: three messages in one send


def test_an_error_reply_takes_its_own_slot_and_the_rest_still_arrive():
    conn, peer = _paired()
    peer.sendall(_reply(1, "s", ["gone"], error="org.a11y.Error.Gone") + _reply(2, "u", [4]))
    out = conn.call_many([("x", "/a", "i", "M", "", ()), ("x", "/b", "i", "M", "", ())])
    assert isinstance(out[0], dbuswire.CallError) and out[0].name == "org.a11y.Error.Gone"
    assert out[1] == [4] and not conn.closed


def test_a_batch_skips_stale_replies_and_signals():
    conn, peer = _paired()
    conn._serial = 10  # earlier calls took serials 1 to 10
    signal = _message(4, [(1, ("o", "/p")), (2, ("s", "i")), (3, ("s", "Sig"))], "s", ["x"])
    peer.sendall(_reply(9, "s", ["stale"]) + signal + _reply(11, "s", ["mine"]))
    assert conn.call_many([("x", "/a", "i", "M", "", ())]) == [["mine"]]


def test_a_batch_that_loses_its_answer_closes_the_connection():
    conn, peer = _paired()
    peer.sendall(_reply(1, "s", ["only one"]))
    with pytest.raises(dbuswire.DBusError, match="no answer to 2 calls"):
        conn.call_many([("x", "/a", "i", "M", "", ()), ("x", "/b", "i", "M", "", ())], timeout=0.05)
    assert conn.closed


def test_a_batch_waits_for_each_reply_not_for_the_whole_batch():
    # a slow app that answers every call within the timeout must not fail a
    # batch whose replies together take longer than it
    conn, peer = _paired()
    import time as _time

    def answer():
        data = b""
        while data.count(b"\x03\x01s\0") < 30:
            data += peer.recv(65536)
        for serial in range(1, 31):
            _time.sleep(0.01)
            peer.sendall(_reply(serial, "u", [serial]))

    t = threading.Thread(target=answer)
    t.start()
    out = conn.call_many([("x", f"/o{i}", "i", "M", "", ()) for i in range(30)], timeout=0.15)
    t.join()
    assert out == [[i] for i in range(1, 31)] and not conn.closed


def test_a_busctl_batch_reads_a_property_as_a_plain_value(monkeypatch):
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")

    def run(argv, **k):
        if argv[4] == "get-property":
            return subprocess.CompletedProcess(argv, 0, '{"type":"s","data":"Save"}', "")
        return subprocess.CompletedProcess(argv, 0, '{"type":"u","data":[43]}', "")

    monkeypatch.setattr(a11y.subprocess, "run", run)
    # conftest makes dbuswire.connection raise Unreachable: the busctl path
    bus = a11y.Bus("unix:path=/x")
    out = bus.call_many([("app", "/o", "org.freedesktop.DBus.Properties", "Get", "ss",
                          "org.a11y.atspi.Accessible", "Name"),
                         ("app", "/o", "org.a11y.atspi.Accessible", "GetRole")])
    assert out == [["Save"], [43]]


def test_an_empty_batch_sends_nothing():
    conn, peer = _paired()
    assert conn.call_many([]) == [] and conn._serial == 0


# --- a11y.Bus over the connection --------------------------------------------


class _FakeConnection:
    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error = [], reply, error

    def call(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.reply

    def call_many(self, calls):
        # as Connection.call_many: an error reply fills its own slot, a lost
        # connection raises for the whole batch
        self.calls.extend(calls)
        if isinstance(self.error, dbuswire.CallError):
            return [self.error for _ in calls]
        if self.error:
            raise self.error
        return [self.reply for _ in calls]


def test_bus_passes_typed_arguments(monkeypatch):
    conn = _FakeConnection(reply=[True])
    monkeypatch.setattr(dbuswire, "connection", lambda address: conn)
    bus = a11y.Bus("unix:path=/x")
    bus.call("app", "/o", "org.a11y.atspi.EditableText", "InsertText", "isi", "3", "hi", "2")
    bus.call("app", "/o", "org.a11y.atspi.Component", "GetExtents", "u", "1")
    assert conn.calls[0][4:] == ("isi", [3, "hi", 2])
    assert conn.calls[1][4:] == ("u", [1])


def test_bus_reads_a_property_through_properties_get(monkeypatch):
    conn = _FakeConnection(reply=["Send"])
    monkeypatch.setattr(dbuswire, "connection", lambda address: conn)
    assert a11y.Bus("unix:path=/x").prop("app", "/o", "org.a11y.atspi.Accessible", "Name") == "Send"
    assert conn.calls[0][2:] == ("org.freedesktop.DBus.Properties", "Get", "ss",
                                 ["org.a11y.atspi.Accessible", "Name"])


def test_a_refusal_reads_as_absence_and_a_lost_answer_does_not(monkeypatch):
    # an object the app says is gone has no children; an app that did not
    # answer must not pass for one with no children
    conn = _FakeConnection(error=dbuswire.CallError("org.freedesktop.DBus.Error.UnknownObject", ""))
    monkeypatch.setattr(dbuswire, "connection", lambda address: conn)
    bus = a11y.Bus("unix:path=/x")
    assert a11y._children(bus, "app", "/o") == []
    conn.error = dbuswire.DBusError("no answer to GetChildren within 10 s")
    with pytest.raises(a11y.A11yError, match="no answer") as err:
        a11y._children(bus, "app", "/o")
    assert not isinstance(err.value, a11y.CallRefused)
    with pytest.raises(a11y.A11yError):
        a11y.find_elements(bus, "app", "/o")


def test_bus_falls_back_to_busctl_when_the_socket_is_unreachable(monkeypatch):
    monkeypatch.setattr(a11y.shutil, "which", lambda n: "/usr/bin/busctl")
    argv = []
    monkeypatch.setattr(a11y.subprocess, "run", lambda a, **k: argv.append(a) or
                        subprocess.CompletedProcess(a, 0, '{"type":"s","data":"x"}', ""))
    # conftest makes dbuswire.connection raise Unreachable
    assert a11y.Bus("unix:path=/x").prop("app", "/o", "i", "Name") == "x"
    assert argv[0][:2] == ["busctl", "--json=short"]


# --- a real bus: a private dbus-daemon ---------------------------------------


@pytest.fixture
def private_bus(monkeypatch, tmp_path):
    if not shutil.which("dbus-daemon"):
        pytest.skip("needs dbus-daemon")
    config = tmp_path / "bus.conf"
    config.write_text(
        "<!DOCTYPE busconfig PUBLIC \"-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN\" "
        "\"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd\">"
        f"<busconfig><type>session</type><listen>unix:dir={tmp_path}</listen>"
        "<auth>EXTERNAL</auth><policy context=\"default\"><allow send_destination=\"*\"/>"
        "<allow eavesdrop=\"true\"/><allow own=\"*\"/></policy></busconfig>")
    proc = subprocess.Popen(["dbus-daemon", "--nofork", "--print-address=1",
                             f"--config-file={config}"],
                            stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    monkeypatch.setattr(dbuswire, "connection", real_connection)
    yield address
    proc.terminate()
    proc.wait(timeout=5)


def test_a_real_bus_authenticates_and_answers(private_bus):
    conn = dbuswire.Connection(private_bus)
    assert conn.unique_name.startswith(":")
    assert isinstance(conn.call(_BUS, _BUS_PATH, _BUS, "GetId")[0], str)
    assert _BUS in conn.call(_BUS, _BUS_PATH, _BUS, "ListNames")[0]
    assert conn.call(_BUS, _BUS_PATH, _BUS, "NameHasOwner", "s", [_BUS]) == [True]
    with pytest.raises(dbuswire.CallError, match="NameHasNoOwner"):
        conn.call(_BUS, _BUS_PATH, _BUS, "GetNameOwner", "s", ["org.example.Nobody"])
    assert conn.call(_BUS, _BUS_PATH, _BUS, "GetConnectionUnixProcessID", "s",
                     [conn.unique_name]) == [os.getpid()]
    conn.close()


def test_a11y_bus_works_over_a_real_bus(private_bus):
    bus = a11y.Bus(private_bus)
    assert bus.call(_BUS, _BUS_PATH, _BUS, "NameHasOwner", "s", _BUS) == [True]
    assert isinstance(bus.prop(_BUS, _BUS_PATH, _BUS, "Features"), list)
    me = dbuswire.connection(private_bus).unique_name
    assert bus.conn_pid(me) == os.getpid()
    with pytest.raises(a11y.CallRefused):
        bus.call(_BUS, _BUS_PATH, _BUS, "GetNameOwner", "s", "org.example.Nobody")


def test_one_connection_serves_the_process_and_is_replaced_once_broken(private_bus):
    first = dbuswire.connection(private_bus)
    assert dbuswire.connection(private_bus) is first
    first.close()
    second = dbuswire.connection(private_bus)
    assert second is not first and not second.closed
    second.close()
