"""The browser bridge: native messaging frames, the relay, the client, the installer.

Nothing here starts a browser. The extension's side of the pipe is played by the test,
which is exactly what Chrome gives the host: a stdin to read frames from and a stdout to
write them to.
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import struct
import threading
import time
from pathlib import Path

import pytest

from hypruse.browser import client as client_mod
from hypruse.browser import framing, host, install
from hypruse.browser.client import BrowserClient, BrowserError, BrowserUnavailable

# --------------------------------------------------------------------------- framing


def test_a_frame_is_a_little_endian_length_then_json():
    raw = framing.encode({"hello": {"protocol": 1}})
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert json.loads(raw[4:]) == {"hello": {"protocol": 1}}


def test_frames_read_back_one_at_a_time_and_end_cleanly():
    stream = io.BytesIO(framing.encode({"a": 1}) + framing.encode({"b": 2}))
    assert framing.read(stream) == {"a": 1}
    assert framing.read(stream) == {"b": 2}
    assert framing.read(stream) is None


def test_a_frame_cut_short_is_the_end_of_the_stream():
    raw = framing.encode({"a": 1})
    assert framing.read(io.BytesIO(raw[:-2])) is None
    assert framing.read(io.BytesIO(raw[:2])) is None


def test_a_frame_over_the_browser_limit_is_refused_before_it_is_sent():
    with pytest.raises(framing.FrameTooLarge):
        framing.encode({"x": "a" * (framing.MAX_TO_BROWSER + 1)})


def test_a_frame_that_claims_to_be_enormous_is_not_read():
    with pytest.raises(framing.FrameTooLarge):
        framing.read(io.BytesIO(struct.pack("<I", framing.MAX_FROM_BROWSER + 1)))


# --------------------------------------------------------------------------- the relay


class Extension:
    """The browser side of the host's stdin and stdout."""

    def __init__(self) -> None:
        to_host_r, self._to_host = os.pipe()
        self._from_host, from_host_w = os.pipe()
        self.host_stdin = os.fdopen(to_host_r, "rb", buffering=0)
        self.host_stdout = os.fdopen(from_host_w, "wb", buffering=0)
        self._reader = os.fdopen(self._from_host, "rb", buffering=0)
        self.received: list[dict] = []
        self._lock = threading.Lock()
        self.answer = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def send(self, message: dict) -> None:
        os.write(self._to_host, framing.encode(message))

    def hello(self) -> None:
        self.send({"hello": {"protocol": 1, "extension": "0.1.0", "browser": "Chrome/151"}})

    def close(self) -> None:
        if self._to_host >= 0:
            os.close(self._to_host)
            self._to_host = -1

    def _serve(self) -> None:
        while True:
            try:
                message = framing.read(self._reader)
            except (OSError, ValueError):
                return
            if message is None:
                return
            with self._lock:
                self.received.append(message)
            if self.answer and "id" in message:
                result = {"echo": message.get("op"), "args": message.get("args", {})}
                self.send({"id": message["id"], "ok": True, "result": result})


@pytest.fixture
def relay(tmp_path):
    ext = Extension()
    path = tmp_path / "run" / "hypruse" / "browser.sock"
    done = threading.Event()

    def run() -> None:
        host.serve(ext.host_stdin, ext.host_stdout, path)
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ext.hello()
    _until(path.exists)
    yield ext, path, done
    ext.close()
    done.wait(5)


def _until(condition, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition never became true"
        time.sleep(0.01)


def _line_client(path: Path) -> tuple[socket.socket, io.BufferedReader]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    s.settimeout(5)
    return s, s.makefile("rb")


def _send(s: socket.socket, message: dict) -> None:
    s.sendall(json.dumps(message).encode() + b"\n")


def _recv(reader) -> dict:
    return json.loads(reader.readline())


def test_a_client_is_greeted_with_what_is_on_the_other_end(relay):
    _ext, path, _ = relay
    s, reader = _line_client(path)
    hello = _recv(reader)["hello"]
    assert hello["protocol"] == 1
    assert hello["connected"] is True
    assert hello["extension"] == "0.1.0"
    s.close()


def test_a_request_comes_back_under_the_id_the_client_chose(relay):
    ext, path, _ = relay
    s, reader = _line_client(path)
    _recv(reader)
    _send(s, {"id": "mine-1", "op": "tabs.list", "args": {}})
    reply = _recv(reader)
    assert reply == {"id": "mine-1", "ok": True, "result": {"echo": "tabs.list", "args": {}}}
    # the extension saw the host's own id, never the client's
    assert ext.received[-1]["id"] != "mine-1"
    s.close()


def test_two_clients_using_the_same_id_never_get_each_others_answer(relay):
    _ext, path, _ = relay
    a, ra = _line_client(path)
    b, rb = _line_client(path)
    _recv(ra), _recv(rb)
    _send(a, {"id": "1", "op": "from-a"})
    _send(b, {"id": "1", "op": "from-b"})
    assert _recv(ra)["result"]["echo"] == "from-a"
    assert _recv(rb)["result"]["echo"] == "from-b"
    a.close()
    b.close()


def test_an_event_from_the_extension_reaches_every_client(relay):
    ext, path, _ = relay
    a, ra = _line_client(path)
    b, rb = _line_client(path)
    _recv(ra), _recv(rb)
    ext.send({"event": "tabs.changed", "data": {"reason": "activated", "tab_id": 3}})
    assert _recv(ra)["event"] == "tabs.changed"
    assert _recv(rb)["data"]["tab_id"] == 3
    a.close()
    b.close()


def test_a_request_that_is_not_one_is_answered_as_a_bad_request(relay):
    _ext, path, _ = relay
    s, reader = _line_client(path)
    _recv(reader)
    s.sendall(b"not json\n")
    assert _recv(reader)["error"]["code"] == "bad_request"
    _send(s, {"id": "2"})  # no op
    reply = _recv(reader)
    assert reply["id"] == "2" and reply["error"]["code"] == "bad_request"
    s.close()


def test_the_socket_is_the_owners_alone(relay):
    _ext, path, _ = relay
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_host_leaves_when_the_browser_closes_the_pipe(relay):
    ext, path, done = relay
    ext.close()
    assert done.wait(5)
    assert not path.exists()


def test_a_second_host_does_not_take_a_live_socket(relay, tmp_path):
    _ext, path, _ = relay
    other = Extension()
    started = time.monotonic()
    assert host.serve(other.host_stdin, other.host_stdout, path) == host.ALREADY_SERVED
    assert time.monotonic() - started < 5
    # and the first one is still there
    s, reader = _line_client(path)
    assert _recv(reader)["hello"]["connected"] is True
    s.close()


def test_a_stale_socket_file_is_replaced(tmp_path):
    path = tmp_path / "hypruse" / "browser.sock"
    path.parent.mkdir(mode=0o700)
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()  # the file stays, nobody listens
    ext = Extension()
    thread = threading.Thread(
        target=host.serve, args=(ext.host_stdin, ext.host_stdout, path), daemon=True
    )
    thread.start()
    ext.hello()
    _until(lambda: _connectable(path))
    ext.close()
    thread.join(5)


def _connectable(path: Path) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(path))
        s.close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- the client


def test_the_client_calls_and_returns_the_result(relay):
    _ext, path, _ = relay
    with BrowserClient(path, timeout=5) as browser:
        assert browser.hello["extension"] == "0.1.0"
        assert browser.call("tabs.list") == {"echo": "tabs.list", "args": {}}
        assert browser.snapshot(settle_ms=300)["args"] == {"max": 120, "settle_ms": 300}


def test_an_error_from_the_extension_becomes_an_exception_with_its_code(relay):
    ext, path, _ = relay
    ext.answer = False
    with BrowserClient(path, timeout=5) as browser:
        # the extension answers this one by hand, with an error

        def reply_with_error() -> None:
            _until(lambda: any("id" in m for m in ext.received))
            hid = [m for m in ext.received if "id" in m][-1]["id"]
            ext.send({"id": hid, "ok": False, "error": {"code": "stale_snapshot", "message": "x"}})

        threading.Thread(target=reply_with_error, daemon=True).start()
        with pytest.raises(BrowserError) as caught:
            browser.click("s1", "e2")
        assert caught.value.code == "stale_snapshot"


def test_a_call_nobody_answers_times_out(relay):
    ext, path, _ = relay
    ext.answer = False
    with BrowserClient(path, timeout=0.3) as browser, pytest.raises(BrowserError) as caught:
        browser.call("tabs.list")
    assert caught.value.code == "timeout"


def test_no_socket_means_no_browser(tmp_path):
    with pytest.raises(BrowserUnavailable):
        BrowserClient(tmp_path / "nothing.sock").connect()


def test_the_default_socket_lives_in_the_runtime_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert client_mod.socket_path() == tmp_path / "hypruse" / "browser.sock"


# --------------------------------------------------------------------------- the installer


def test_install_writes_a_manifest_for_every_browser_that_is_there(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "google-chrome").mkdir(parents=True)
    (home / ".config" / "BraveSoftware" / "Brave-Browser").mkdir(parents=True)
    written = install.install(home=home, python="/usr/bin/python3")
    names = sorted(p.parent.parent.name for p in written.manifests)
    assert names == ["Brave-Browser", "google-chrome"]
    manifest = json.loads(written.manifests[0].read_text())
    assert manifest["name"] == "dev.hypruse.browser"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == [f"chrome-extension://{install.EXTENSION_ID}/"]
    assert manifest["path"] == str(written.launcher)


def test_the_launcher_runs_the_host_with_the_python_that_installed_it(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "chromium").mkdir(parents=True)
    written = install.install(home=home, python="/opt/venv/bin/python")
    text = written.launcher.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert '"/opt/venv/bin/python" -m hypruse browser-host "$@"' in text
    assert written.launcher.stat().st_mode & stat.S_IXUSR


def test_a_browser_named_explicitly_is_installed_even_before_its_first_run(tmp_path):
    home = tmp_path / "home"
    written = install.install(home=home, browsers=("chromium",), python="/usr/bin/python3")
    assert len(written.manifests) == 1 and written.manifests[0].exists()


def test_uninstall_removes_what_install_wrote(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "google-chrome").mkdir(parents=True)
    written = install.install(home=home, python="/usr/bin/python3")
    removed = install.uninstall(home=home)
    assert set(removed) == {*written.manifests, written.launcher}
    assert not any(p.exists() for p in removed)


def test_status_says_where_it_is_installed_and_whether_a_browser_is_connected(tmp_path, relay):
    _ext, path, _ = relay
    home = tmp_path / "home"
    (home / ".config" / "google-chrome").mkdir(parents=True)
    install.install(home=home, python="/usr/bin/python3")
    found = install.status(home=home, socket=path)
    assert found.installed == ("chrome",)
    assert found.extension == "0.1.0"
    assert install.status(home=home, socket=tmp_path / "none.sock").extension == ""
