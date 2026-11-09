"""Fixtures: a fake hypruse on a fake desktop, and the Hermes contract.

The provider imports ``tools.computer_use.backend`` from Hermes. With
HERMES_AGENT_SRC pointing at a hermes-agent checkout (and its dependencies
installed) the tests use the real module; otherwise the stub in ``stubs/``.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FAKE = HERE / "fake_hypruse.py"

_hermes = os.environ.get("HERMES_AGENT_SRC")
if _hermes:
    sys.path.insert(0, _hermes)
else:
    try:
        import tools.computer_use.backend  # noqa: F401  (Hermes is installed here)
    except ImportError:
        sys.path.insert(0, str(HERE / "stubs"))
if (ROOT / "hermes_hypruse").is_dir():  # not when copied into hermes-agent's test tree
    sys.path.insert(0, str(ROOT))

HUMAN = "0x55a447938f40"  # the human's kitty, focused, on screen
AGENT = "0x564859226b80"  # a yad the agent launched on workspace 99
FIREFOX = "0x55a4479a1200"

DESKTOP = {
    "monitors": [{"name": "eDP-1", "geometry": [0, 0, 1920, 1080], "scale": 2.0, "focused": True,
                  "active_workspace": 7}],
    "workspaces": [
        {"id": 7, "name": "7", "monitor": "eDP-1", "windows": 2, "visible": True},
        {"id": 99, "name": "99", "monitor": "eDP-1", "windows": 1, "visible": False},
    ],
    "windows": [
        {"address": HUMAN, "workspace": 7, "class": "kitty", "title": "~", "at": [964, 7],
         "size": [949, 1066], "floating": False, "pid": 5419},
        {"address": FIREFOX, "workspace": 7, "class": "firefox", "title": "Inbox", "at": [7, 7],
         "size": [950, 1066], "floating": True, "pid": 6000},
        {"address": AGENT, "workspace": 99, "class": "yad", "title": "Signup", "at": [100, 50],
         "size": [400, 300], "floating": True, "pid": 7001},
    ],
    "active_window": HUMAN,
    "cursor": [642, 516],
    "layers": [],
}

LEGEND = [
    {"mark": 1, "role": "check box", "name": "Agree", "x": 150, "y": 100, "clickable": True,
     "checked": False},
    {"mark": 2, "role": "text", "name": "City", "x": 300, "y": 150, "clickable": True,
     "value": "Rabat"},
    {"mark": 3, "role": "push button", "name": "OK", "x": 450, "y": 320, "clickable": True},
]


class Fake:
    """The scenario the fake answers from, and the calls it saw."""

    def __init__(self, runtime: Path) -> None:
        self.base = runtime / "fake-hypruse"
        self.base.mkdir(parents=True)
        self.scenario = {"version": "0.13.1", "desktop": copy.deepcopy(DESKTOP), "scale": 2.0,
                         "legend": copy.deepcopy(LEGEND), "rules": []}
        self.save()

    def save(self) -> None:
        (self.base / "scenario.json").write_text(json.dumps(self.scenario))

    def load(self) -> None:  # the fake drops a used `once` rule from the file
        self.scenario = json.loads((self.base / "scenario.json").read_text())

    def rule(self, argv: list[str], exit: int = 0, result=None, message: str = "",
             once: bool = False) -> None:
        self.load()
        self.scenario["rules"].append({"argv": argv, "exit": exit, "result": result,
                                       "message": message, "once": once})
        self.save()

    def set(self, **changes) -> None:
        self.load()
        self.scenario.update(changes)
        self.save()

    def calls(self) -> list[dict]:
        log = self.base / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def argvs(self) -> list[list[str]]:
        return [[a for a in c["argv"] if a != "--json"] for c in self.calls()]

    def verbs(self) -> list[str]:
        return [a[0] for a in self.argvs()]

    def clear(self) -> None:
        (self.base / "calls.jsonl").unlink(missing_ok=True)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "fake-instance")
    monkeypatch.setenv("HERMES_HYPRUSE_CMD", f"{sys.executable} {FAKE}")
    return Fake(runtime)


OPTIONS = {"command": None, "delivery": "background", "timeout": 30.0, "max_image_dimension": 1456}


@pytest.fixture
def backend(fake):
    from hermes_hypruse.backend import HypruseBackend

    b = HypruseBackend(options=dict(OPTIONS))
    b.start()
    fake.clear()
    return b
