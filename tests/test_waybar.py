"""The Waybar module builds its JSON with jq: a crafted last_action must
never blank or forge the indicator (the shell half of the beacon-injection
fix; the Python half is tested in test_safety.py)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "waybar" / "hypruse-status.sh"

needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="needs bash and jq",
)


def run_module(runtime_dir):
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(proc.stdout)


@needs_shell
def test_hostile_last_action_cannot_hide_the_indicator(tmp_path):
    d = tmp_path / "hypruse"
    d.mkdir()
    evil = 'x","class":"idle","text":"'
    (d / "state.json").write_text(
        json.dumps(
            {"pid": os.getpid(), "started": 1, "last_action": evil, "last_ts": 2}
        )
    )
    out = run_module(tmp_path)
    assert out["class"] == "active" and out["text"]  # robot stays visible
    assert evil in out["tooltip"]  # injected text is inert payload, not structure


@needs_shell
def test_idle_when_no_beacon(tmp_path):
    out = run_module(tmp_path)
    assert out == {"text": "", "class": "idle", "tooltip": ""}


@needs_shell
def test_idle_when_pid_is_dead(tmp_path):
    d = tmp_path / "hypruse"
    d.mkdir()
    (d / "state.json").write_text(
        json.dumps({"pid": 2**22 - 1, "started": 1, "last_action": "", "last_ts": 2})
    )
    out = run_module(tmp_path)
    assert out["class"] == "idle"
