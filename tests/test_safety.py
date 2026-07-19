import json
import os

from hypruse import safety


def test_beacon_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    safety.init()
    path = tmp_path / "hypruse" / "state.json"
    state = json.loads(path.read_text())
    assert state["pid"] == os.getpid()
    assert state["started"] > 0

    safety.touch("pointer:click")
    state = json.loads(path.read_text())
    assert state["last_action"] == "pointer:click"
    assert state["last_ts"] >= state["started"]

    safety.shutdown()
    assert not path.exists()


def test_touch_without_init_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    safety.shutdown()  # ensure clean module state
    safety.touch("anything")
    assert not (tmp_path / "hypruse" / "state.json").exists()


def test_touch_sanitizes_agent_controlled_action(tmp_path, monkeypatch):
    # tool arguments reach touch() before validation, and consumers like the
    # Waybar module re-embed last_action in JSON they build: a crafted
    # action string must never carry quotes or structure into the beacon
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    safety.init()
    safety.touch('x","class":"idle","text":"')
    state = json.loads((tmp_path / "hypruse" / "state.json").read_text())
    assert state["last_action"] == "xclass:idletext:"
    safety.touch("a" * 200)
    state = json.loads((tmp_path / "hypruse" / "state.json").read_text())
    assert len(state["last_action"]) == 64
    safety.touch("pointer:click")  # the legitimate vocabulary passes untouched
    state = json.loads((tmp_path / "hypruse" / "state.json").read_text())
    assert state["last_action"] == "pointer:click"
    safety.shutdown()


def test_server_main_registers_drag_release_cleanup(tmp_path, monkeypatch):
    # the glue that arms the kill-switch-mid-drag cleanup: main() must
    # register input.release_held, or shutdown() releases nothing
    from hypruse import server as srv

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(srv.session, "ensure_session_env", lambda: None)
    monkeypatch.setattr(srv.mcp, "run", lambda: None)
    monkeypatch.setattr(srv.sys, "argv", ["hypruse"])

    class Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr(srv.sys, "stdin", Pipe())
    srv.main()
    assert srv.hinput.release_held in safety._cleanups
    safety.shutdown()


def test_shutdown_runs_registered_cleanups(tmp_path, monkeypatch):
    # the SIGTERM kill switch runs shutdown(); a drag's held button must be
    # released by a registered cleanup before the process dies
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    calls = []
    safety.init()
    safety.on_shutdown(lambda: calls.append("released"))
    safety.on_shutdown(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    safety.shutdown()  # a failing cleanup must not block the others
    assert calls == ["released"]
    safety.shutdown()  # cleanups are drained: no double release
    assert calls == ["released"]
