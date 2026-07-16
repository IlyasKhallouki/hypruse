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
