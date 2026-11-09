"""The provider through Hermes' own ``computer_use`` tool, as a user gets it:
found as a directory under ``$HERMES_HOME/plugins/``, selected by
``computer_use.backend``, behind the approval gate, its results shaped into
what the model reads. Needs hermes-agent importable (HERMES_AGENT_SRC, or
this file inside hermes-agent's test tree); skipped otherwise."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("plugins.computer_use", reason="hermes-agent is not importable")

PROVIDER = Path(os.environ.get("HYPRUSE_PROVIDER_DIR")
                or Path(__file__).resolve().parents[1] / "hermes_hypruse")
YAD = "0x564859226b80"  # the agent's window in the fake desktop


@pytest.fixture
def hermes(tmp_path, fake, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.computer_use import tool

    home = tmp_path / "hermes-home"
    shutil.copytree(PROVIDER, home / "plugins" / "hypruse",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (home / "config.yaml").write_text("computer_use:\n  backend: hypruse\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")  # someone to ask; the callback says "once"
    token = set_hermes_home_override(str(home))
    tool.reset_backend_for_tests()
    tool.set_approval_callback(lambda command, description, **kw: "once")
    monkeypatch.setattr(tool, "_should_route_through_aux_vision", lambda: False)
    try:
        yield tool
    finally:
        tool.set_approval_callback(None)
        tool.reset_backend_for_tests()
        reset_hermes_home_override(token)
        for name in [m for m in sys.modules if m.startswith("_hermes_user_computer_use.")]:
            del sys.modules[name]


def call(tool, **args):
    out = tool.handle_computer_use(args, session_id="s1")
    return out if isinstance(out, dict) else json.loads(out)


def test_discovered_selected_and_available(hermes):
    from hermes_cli.tools_config_providers import _computer_use_provider_rows
    from plugins.computer_use import discover_computer_use_providers, get_active_provider

    assert "hypruse" in dict(discover_computer_use_providers())
    assert any(r["computer_use_backend"] == "hypruse" for r in _computer_use_provider_rows())
    provider = get_active_provider()
    assert provider.name == "hypruse" and type(provider).__module__ == "_hermes_user_computer_use.hypruse"
    assert hermes.check_computer_use_requirements() is True


def test_som_capture_reaches_the_model_with_numbered_elements(hermes, fake):
    out = call(hermes, action="capture", app="yad")
    assert out["_multimodal"] is True
    summary = out["content"][0]["text"]
    assert summary.startswith("capture mode=som 800x600 app=yad window='Signup'")
    assert "#1 check box 'Agree [unchecked]' @ (100, 100, 1, 1) [yad]" in summary
    assert "workspace 99" in summary  # the hidden-window note
    assert "native desktop coordinates" not in summary  # bounds and image share one space
    assert out["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")


def test_ax_capture_is_text_only(hermes):
    out = call(hermes, action="capture", mode="ax", app="yad")
    assert out["mode"] == "ax" and out["total_elements"] == 3
    assert out["elements"][2] == {"index": 3, "role": "push button", "label": "OK",
                                  "bounds": [700, 540, 1, 1], "app": "yad"}


def test_element_click_and_its_verdict(hermes, fake):
    call(hermes, action="capture", app="yad")
    fake.rule(["click_ui"], result=("pressed check box 'Agree' through its accessibility action (no "
                                    "pointer, the human's focus untouched) in yad; now: checked=true"))
    out = call(hermes, action="click", element=1)
    assert out["ok"] is True and out["effect"] == "confirmed" and out["verdict"] == {"decision": "done"}
    assert fake.argvs()[-1] == ["click_ui", "--mark", "1", "--background"]


def test_a_refusal_reaches_the_model_once(hermes, fake):
    call(hermes, action="capture", app="yad")
    fake.clear()
    fake.rule(["click_ui"], exit=3, message="yad is not in HYPRUSE_CONFINE")
    out = call(hermes, action="click", element=3)
    assert out["ok"] is False and out["code"] == "refused"
    assert "HYPRUSE_CONFINE" in out["message"] and "do not retry" in out["message"]
    assert fake.verbs() == ["click_ui"]


def test_capture_after_recaptures_the_same_window(hermes, fake):
    call(hermes, action="capture", app="yad")
    fake.clear()
    fake.rule(["click_ui"], result="pressed push button 'OK' through its accessibility action in yad")
    out = call(hermes, action="click", element=3, capture_after=True)
    # the fake's image did not change, so Hermes' dedup sends the text and elements only
    assert out["ok"] is True and out["screen_unchanged"] is True and out["total_elements"] == 3
    assert fake.argvs()[1:] == [["desktop"], ["marks", "--window", YAD]]


def test_type_goes_to_the_sticky_window_and_the_app_guard_holds(hermes, fake):
    call(hermes, action="capture", app="yad")
    fake.rule(["keyboard"], result=("typed 5 characters into the text field in yad through the "
                                    "accessibility interface; the field now holds the typed text "
                                    "(5 characters)"))
    out = call(hermes, action="type", text="hello")
    assert out["ok"] is True and fake.calls()[-1]["stdin"] == "hello"
    fake.clear()
    out = call(hermes, action="type", text="hello", app="firefox")
    assert out["code"] == "input_target_mismatch" and fake.calls() == []


def test_hermes_hard_blocks_come_first(hermes, fake):
    call(hermes, action="capture", app="yad")
    fake.clear()
    out = call(hermes, action="key", keys="ctrl+alt+delete")
    assert "blocked key combo" in out["error"] and fake.calls() == []


def test_listings_and_unsupported(hermes):
    windows = call(hermes, action="list_windows")
    assert windows["count"] == 3
    assert {w["app_name"] for w in windows["windows"]} == {"kitty", "firefox", "yad"}
    out = call(hermes, action="set_value", value="Blue", element=2)
    assert out["ok"] is False and out["code"] == "unsupported_action"


def test_doctor_and_release(hermes):
    from plugins.computer_use import get_active_provider

    call(hermes, action="capture", app="yad")
    assert hermes.release_computer_use_session("s1") is True
    assert get_active_provider().doctor() == 0
