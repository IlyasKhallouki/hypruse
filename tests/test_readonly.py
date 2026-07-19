import asyncio
import importlib
import re

import pytest

from hypruse import server as srv

OBSERVE = {"desktop", "screenshot", "zoom", "ui", "marks", "binds", "wait_for"}
ACT = {"pointer", "keyboard", "click_ui", "hypr", "launch", "use_bind", "sequence"}


def _tool_names(module):
    return {t.name for t in asyncio.run(module.mcp.list_tools())}


@pytest.fixture
def reloaded(monkeypatch):
    """Reload hypruse.server with a given HYPRUSE_READONLY value and always
    restore the full-tool module afterwards (other tests hold references)."""

    def _with_env(value: str | None):
        if value is None:
            monkeypatch.delenv("HYPRUSE_READONLY", raising=False)
        else:
            monkeypatch.setenv("HYPRUSE_READONLY", value)
        return importlib.reload(srv)

    yield _with_env
    monkeypatch.delenv("HYPRUSE_READONLY", raising=False)
    importlib.reload(srv)


def test_readonly_exposes_only_observation_tools(reloaded):
    names = _tool_names(reloaded("1"))
    assert names == OBSERVE
    assert "READ-ONLY MODE" in reloaded("1").mcp.instructions


def test_default_exposes_everything(reloaded):
    names = _tool_names(reloaded(None))
    assert names == OBSERVE | ACT
    assert "READ-ONLY" not in reloaded(None).mcp.instructions


@pytest.mark.parametrize(
    "value,expect_readonly",
    [("true", True), ("0", False), ("off", False), ("YES", True)],
)
def test_env_value_parsing(reloaded, value, expect_readonly):
    names = _tool_names(reloaded(value))
    assert (names == OBSERVE) is expect_readonly


def test_clipboard_is_double_gated(reloaded, monkeypatch):
    # absent by default, present with the opt-in flag, never in read-only
    assert "clipboard" not in _tool_names(reloaded(None))
    monkeypatch.setenv("HYPRUSE_CLIPBOARD", "1")
    assert "clipboard" in _tool_names(reloaded(None))
    assert _tool_names(reloaded("1")) == OBSERVE
    monkeypatch.delenv("HYPRUSE_CLIPBOARD")
    assert "clipboard" not in _tool_names(reloaded(None))


# every acting tool name, as a word (so "hypruse"/"Hyprland" do not match
# "hypr", and "launchers" does not match "launch")
_ACT_REF = re.compile(
    r"\b(pointer|keyboard|click_ui|hypr|launch|use_bind|sequence|clipboard)\b"
)


def test_readonly_surface_advertises_no_acting_tools(reloaded):
    # the audit found the read-only INSTRUCTIONS and the observation
    # docstrings still walking the agent through the 7 stripped tools
    module = reloaded("1")
    leak = _ACT_REF.search(module.mcp.instructions or "")
    assert leak is None, f"instructions advertise {leak.group(0)!r} in read-only mode"
    for tool in asyncio.run(module.mcp.list_tools()):
        leak = _ACT_REF.search(tool.description or "")
        assert leak is None, f"{tool.name} advertises {leak.group(0)!r} in read-only mode"


def test_readonly_marks_hint_does_not_advertise_click_ui(reloaded, monkeypatch):
    module = reloaded("1")
    monkeypatch.setattr(module.safety, "touch", lambda *a: None)
    monkeypatch.setattr(
        module, "_resolve_window",
        lambda w="": {"address": "0xa", "at": [0, 0], "size": [10, 10], "class": "x"},
    )
    monkeypatch.setattr(
        module, "_ui_read",
        lambda w="", name="", actionable=True: [
            {"role": "button", "name": "Ok", "x": 5, "y": 5, "clickable": True}
        ],
    )
    monkeypatch.setattr(
        module, "_grab_env",
        lambda **kw: (b"IMG", {"geometry": [0, 0, 10, 10], "scale": 1.0,
                               "image": [10, 10], "format": "jpeg"}),
    )
    monkeypatch.setattr(module, "_draw_marks", lambda *a: None)  # legend-only path
    out = module.marks(window="0xa")
    legend_json = out[-1].text
    assert "click_ui" not in legend_json


def test_full_mode_docstrings_still_reference_acting_tools(reloaded):
    # the read-only rewrite must not bleed into the default surface
    module = reloaded(None)
    tools = {t.name: t.description for t in asyncio.run(module.mcp.list_tools())}
    assert "click_ui" in tools["ui"]
    assert "use_bind" in tools["binds"]
    assert "click_ui(mark=N)" in tools["marks"]
