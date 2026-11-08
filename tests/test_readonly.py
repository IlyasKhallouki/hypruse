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


def test_both_instruction_sets_separate_listed_layers_from_visible(reloaded):
    # `hyprctl layers` reports surfaces the compositor tracks, mapped or
    # not, so neither instruction set may let presence read as visibility
    for value in ("1", None):
        text = " ".join((reloaded(value).mcp.instructions or "").split())
        assert "not one you can see" in text


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
        lambda w="", name="", actionable=True, handles=False: [
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


def test_every_tool_declares_a_title_and_whether_it_acts(reloaded, monkeypatch):
    # clients and directories read these hints (approval prompts, quality
    # scores); an observation tool must say it changes nothing, and an
    # acting one must not claim to be harmless
    monkeypatch.setenv("HYPRUSE_CLIPBOARD", "1")
    tools = asyncio.run(reloaded(None).mcp.list_tools())
    assert {t.name for t in tools} == OBSERVE | ACT | {"clipboard"}
    for t in tools:
        assert t.annotations is not None, t.name
        a = t.annotations.model_dump(by_alias=True)  # the wire names, in mcp 1 and 2
        assert a["title"], t.name
        if t.name in OBSERVE:
            assert a["readOnlyHint"] is True and a["openWorldHint"] is False, t.name
        else:
            assert a["readOnlyHint"] is False and a["destructiveHint"] is True, t.name


def test_the_acting_tools_explain_background_mode(reloaded, monkeypatch):
    monkeypatch.delenv("HYPRUSE_BACKGROUND", raising=False)
    module = reloaded(None)
    tools = {t.name: t.description for t in asyncio.run(module.mcp.list_tools())}
    for name in ("click_ui", "keyboard", "pointer", "launch", "sequence", "hypr", "use_bind"):
        assert "background" in tools[name].lower(), name
    assert "agent's own workspaces" in module.mcp.instructions


def test_the_instructions_follow_the_background_setting(reloaded, monkeypatch):
    monkeypatch.setenv("HYPRUSE_BACKGROUND", "1")
    assert "GUARANTEED" in reloaded(None).mcp.instructions
    monkeypatch.setenv("HYPRUSE_BACKGROUND", "0")
    assert "off for this session" in reloaded(None).mcp.instructions
    monkeypatch.setenv("HYPRUSE_BACKGROUND", "1")
    assert "GUARANTEED" not in reloaded("1").mcp.instructions  # read-only acts on nothing


def test_tool_calls_run_one_at_a_time(reloaded):
    # mcp 2 runs each plain tool call on its own worker thread; hypruse's
    # tools share held keys, the seat baseline and the journal, so the
    # registered tools must not overlap even when calls arrive together
    import inspect
    import threading
    import time

    module = reloaded(None)
    inside, overlaps = [0], []

    def tool(x: int = 0) -> int:
        inside[0] += 1
        overlaps.append(inside[0])
        time.sleep(0.02)
        inside[0] -= 1
        return x

    wrapped = module._one_at_a_time(tool)
    assert inspect.signature(wrapped) == inspect.signature(tool)  # the schema source
    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(overlaps) == 1
