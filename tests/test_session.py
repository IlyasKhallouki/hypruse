import os
import time

from hypruse import session


def make_instance(runtime, sig, age=0.0):
    d = runtime / "hypr" / sig
    d.mkdir(parents=True)
    (d / ".socket.sock").touch()
    ts = time.time() - age
    os.utime(d, (ts, ts))
    return d


def test_discovers_missing_vars(tmp_path, monkeypatch):
    make_instance(tmp_path, "abc123_1_2")
    (tmp_path / "wayland-1").touch()
    (tmp_path / "wayland-1.lock").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    session.ensure_session_env()

    assert os.environ["HYPRLAND_INSTANCE_SIGNATURE"] == "abc123_1_2"
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-1"


def test_prefers_newest_live_instance_and_ignores_dead(tmp_path, monkeypatch):
    make_instance(tmp_path, "old_sig", age=500)
    make_instance(tmp_path, "new_sig", age=5)
    dead = tmp_path / "hypr" / "dead_sig"
    dead.mkdir(parents=True)  # no .socket.sock → not a live instance
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)

    session.ensure_session_env()

    assert os.environ["HYPRLAND_INSTANCE_SIGNATURE"] == "new_sig"


def test_never_overrides_existing_env(tmp_path, monkeypatch):
    make_instance(tmp_path, "discovered")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "explicit")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-7")

    session.ensure_session_env()

    assert os.environ["HYPRLAND_INSTANCE_SIGNATURE"] == "explicit"
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-7"


def test_graceful_when_nothing_to_discover(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    session.ensure_session_env()  # must not raise

    assert "HYPRLAND_INSTANCE_SIGNATURE" not in os.environ
    assert "WAYLAND_DISPLAY" not in os.environ
