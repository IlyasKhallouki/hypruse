"""Clipboard module: wl-clipboard subprocess boundary, mocked."""

import subprocess

import pytest

from hypruse import clipboard


class FakeRun:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture(autouse=True)
def wl_tools_present(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_read_text(monkeypatch):
    fake = FakeRun(stdout="héllo".encode())
    monkeypatch.setattr(clipboard.subprocess, "run", fake)
    assert clipboard.read() == "héllo"
    assert fake.calls[0][0] == ["wl-paste", "--no-newline"]


def test_read_empty_clipboard(monkeypatch):
    fake = FakeRun(returncode=1, stderr=b"Nothing is copied\n")
    monkeypatch.setattr(clipboard.subprocess, "run", fake)
    assert clipboard.read() == ""


def test_read_binary_content_errors(monkeypatch):
    fake = FakeRun(stdout=b"\x89PNG\r\n\x1a\n\xff\xfe")
    monkeypatch.setattr(clipboard.subprocess, "run", fake)
    with pytest.raises(clipboard.ClipboardError, match="non-text"):
        clipboard.read()


def test_read_other_failure_errors(monkeypatch):
    fake = FakeRun(returncode=1, stderr=b"No selection\n")
    monkeypatch.setattr(clipboard.subprocess, "run", fake)
    with pytest.raises(clipboard.ClipboardError, match="wl-paste failed"):
        clipboard.read()


def test_write(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(clipboard.subprocess, "run", fake)
    clipboard.write("héllo")
    argv, kwargs = fake.calls[0]
    assert argv == ["wl-copy"]
    assert kwargs["input"] == "héllo".encode()


def test_write_never_captures_output(monkeypatch):
    """wl-copy forks a daemon that inherits captured pipes and never
    closes them: capturing output hangs until the timeout, even though
    the clipboard was set."""
    fake = FakeRun()
    monkeypatch.setattr(clipboard.subprocess, "run", fake)
    clipboard.write("x")
    _, kwargs = fake.calls[0]
    assert kwargs.get("capture_output") is not True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_write_failure_errors(monkeypatch):
    monkeypatch.setattr(clipboard.subprocess, "run", FakeRun(returncode=1))
    with pytest.raises(clipboard.ClipboardError, match="wl-copy failed"):
        clipboard.write("x")


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    with pytest.raises(clipboard.ClipboardError, match="install wl-clipboard"):
        clipboard.read()
