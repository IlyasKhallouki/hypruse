"""The backend against a fake hypruse: what each Hermes method runs, how the
coordinates map, and what a refusal turns into. No desktop, no Hermes."""

from __future__ import annotations

import base64
import json
import sys

import pytest
from conftest import AGENT, FIREFOX, HUMAN, OPTIONS

from hermes_hypruse.backend import HypruseBackend, _normalize_combo
from hermes_hypruse.hypruse_cli import child_env, parse

# --- lifecycle -----------------------------------------------------------------


def test_start_checks_the_version(fake):
    fake.set(version="0.1.2")
    b = HypruseBackend(options=dict(OPTIONS))
    with pytest.raises(RuntimeError, match="0.1.2 is too old"):
        b.start()
    fake.set(version="0.13.0")
    b.start()
    assert b.version == (0, 13, 0)


def test_start_names_a_missing_command(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HYPRUSE_CMD", str(tmp_path / "nowhere" / "hypruse"))
    b = HypruseBackend(options=dict(OPTIONS))
    assert b.is_available() is False
    with pytest.raises(RuntimeError, match="not installed"):
        b.start()


def test_available_needs_a_hyprland_session(fake, monkeypatch):
    b = HypruseBackend(options=dict(OPTIONS))
    assert b.is_available() is (sys.platform == "linux")
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE")
    assert b.is_available() is False  # no $XDG_RUNTIME_DIR/hypr/*/.socket.sock either


def test_child_env_keeps_the_session_and_hypruse_settings_only():
    env = child_env({"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "a",
                     "HYPRUSE_BACKGROUND": "1", "WAYLAND_DISPLAY": "wayland-1",
                     "XDG_RUNTIME_DIR": "/run/user/1000", "GITHUB_TOKEN": "t"})
    assert env == {"PATH": "/usr/bin", "HYPRUSE_BACKGROUND": "1", "WAYLAND_DISPLAY": "wayland-1",
                   "XDG_RUNTIME_DIR": "/run/user/1000"}


def test_secrets_never_reach_the_child(backend, fake, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("HYPRUSE_CONFINE", "launched")
    backend.list_windows()
    env = fake.calls()[-1]["env"]
    assert "OPENAI_API_KEY" not in env and "HYPRUSE_CONFINE" in env


def test_parse_reads_the_shell_contract():
    out = parse(3, "", "hypruse: refused: HYPRUSE_READONLY is set\n")
    assert (out.code, out.kind, out.message, out.refused) == (3, "refused", "HYPRUSE_READONLY is set", True)
    out = parse(0, '{"ok":true,"tool":"keyboard","result":"typed 3 characters"}\n', "")
    assert out.ok and out.text() == "typed 3 characters"
    out = parse(1, "", "some warning\nhypruse: error: window '0x1' not found\n")
    assert (out.kind, out.message) == ("error", "window '0x1' not found")


# --- capture --------------------------------------------------------------------------


def test_som_capture_is_marks_with_elements_in_image_pixels(backend, fake):
    cap = backend.capture(mode="som", app="yad")
    assert fake.argvs() == [["desktop"], ["marks", "--window", AGENT]]
    # window at 100,50 size 400x300 on a scale-2 monitor: an 800x600 image
    assert (cap.mode, cap.width, cap.height, cap.app, cap.window_title) == ("som", 800, 600, "yad", "Signup")
    assert cap.image_mime_type == "image/png"
    assert base64.b64decode(cap.png_b64).startswith(b"\x89PNG")
    agree, city, ok = cap.elements
    assert (agree.index, agree.role, agree.label, agree.bounds) == (1, "check box", "Agree [unchecked]",
                                                                    (100, 100, 1, 1))
    assert city.label == "City = 'Rabat'" and city.attributes["point"] == [300, 150]
    assert ok.bounds == (700, 540, 1, 1)
    assert ok.window_id == int(AGENT, 16) and ok.pid == 7001
    # the sticky target Hermes reads for capture_after and the app guard
    assert backend._last_app == "yad"
    assert backend._last_target == {"pid": 7001, "window_id": int(AGENT, 16)}
    # out of the human's sight, which the model is told
    assert "workspace 99" in cap.note and "background" in cap.note
    # the capture file is read and removed
    assert not list((fake.base.parent / "hypruse").glob("shot-*"))


def test_ax_capture_has_elements_but_no_image(backend, fake):
    cap = backend.capture(mode="ax", app="yad")
    assert cap.png_b64 is None and len(cap.elements) == 3
    assert fake.verbs() == ["desktop", "marks"]


def test_vision_capture_downscales_to_the_image_cap(backend, fake):
    cap = backend.capture(mode="vision", app="firefox")
    # 1066 logical px tall at scale 2 is 2132 px: scaled to fit 1456
    argv = fake.argvs()[-1]
    assert argv[:4] == ["screenshot", "--window", FIREFOX, "--stable"]
    assert argv[argv.index("--scale") + 1] == "0.683"
    assert cap.elements == [] and max(cap.width, cap.height) <= 1456


def test_som_without_a_tree_falls_back_to_a_plain_capture(backend, fake):
    fake.set(marks="no_tree")
    cap = backend.capture(mode="som", app="kitty")
    assert fake.verbs() == ["desktop", "marks", "screenshot"]
    assert cap.png_b64 and cap.elements == []
    assert "kitty exposes no accessibility tree" in cap.note


def test_som_without_imagemagick_still_has_an_image_and_elements(backend, fake):
    fake.set(marks="no_magick")
    cap = backend.capture(mode="som", app="yad")
    assert fake.verbs() == ["desktop", "marks", "screenshot"]
    assert cap.png_b64 and len(cap.elements) == 3 and "ImageMagick" in cap.note


@pytest.mark.parametrize(("app", "want"), [
    ("yad", AGENT), ("YAD", AGENT), ("Signup", AGENT), ("fire", FIREFOX), ("inb", FIREFOX),
    (AGENT, AGENT), ("active", HUMAN), (None, HUMAN),
])
def test_app_resolution(backend, fake, app, want):
    backend.capture(mode="vision", app=app)
    assert fake.argvs()[-1][2] == want


def test_exact_window_id_and_sticky_target(backend, fake):
    backend.capture(mode="vision", pid=6000, window_id=int(FIREFOX, 16))
    assert fake.argvs()[-1][2] == FIREFOX
    backend.capture(mode="vision")  # no app: the window chosen last, not the focused one
    assert fake.argvs()[-1][2] == FIREFOX
    backend.capture(mode="vision", pid=0, window_id=0)  # zero-filled ids are no target
    assert fake.argvs()[-1][2] == FIREFOX


def test_unknown_app_fails_and_clears_the_target(backend, fake):
    backend.capture(mode="som", app="yad")
    cap = backend.capture(mode="som", app="gimp")
    assert (cap.width, cap.height) == (0, 0) and "no window matched app='gimp'" in cap.note
    assert "kitty" in cap.note and backend._last_target is None
    assert backend.type_text("hi").ok is False


def test_full_screen_capture(backend, fake):
    cap = backend.capture(app="screen")
    argv = fake.argvs()[-1]
    assert argv[0] == "screenshot" and "--window" not in argv and "--scale" in argv
    assert (cap.mode, cap.app, cap.elements) == ("vision", "screen", [])
    assert backend._last_target is None


def test_capture_reports_a_refusal(backend, fake):
    fake.rule(["marks"], exit=3, message="the session is locked")
    cap = backend.capture(app="yad")
    assert (cap.width, cap.height) == (0, 0) and cap.note == "hypruse refused: the session is locked"


# --- clicks by element ---------------------------------------------------------------


def test_element_click_is_click_ui_by_mark_in_the_background(backend, fake):
    backend.capture(app="yad")
    fake.rule(["click_ui"], result=("pressed check box 'Agree' through its accessibility action (no "
                                    "pointer, the human's focus untouched) in yad; now: checked=true"))
    res = backend.click(element=1)
    assert fake.argvs()[-1] == ["click_ui", "--mark", "1", "--background"]
    assert (res.ok, res.effect, res.verified, res.path) == (True, "confirmed", True, "accessibility")


def test_element_click_no_change_is_a_suspected_noop(backend, fake):
    backend.capture(app="yad")
    fake.rule(["click_ui"], result="pressed check box 'Agree' through its accessibility action "
                                   "(no pointer) in yad; no change seen")
    assert backend.click(element=1).effect == "suspected_noop"


def test_element_click_foreground_and_buttons(backend, fake):
    backend.capture(app="yad")
    fake.rule(["click_ui"], result="clicked mark 3 (push button 'OK') at (450, 320) in yad")
    res = backend.click(element=3, button="right", click_count=2, delivery_mode="foreground")
    assert fake.argvs()[-1] == ["click_ui", "--mark", "3", "--button", "right", "--double", "--foreground"]
    assert res.ok and res.path == "pointer" and res.action == "double_click"


def test_auto_delivery_leaves_the_choice_to_hypruse(fake):
    b = HypruseBackend(options={**OPTIONS, "delivery": "auto"})
    b.capture(app="yad")
    fake.rule(["click_ui"], result="pressed push button 'OK' through its accessibility action in yad")
    b.click(element=3)
    assert fake.argvs()[-1] == ["click_ui", "--mark", "3"]


def test_element_must_come_from_the_last_capture(backend, fake):
    res = backend.click(element=1)
    assert not res.ok and "not in the last capture" in res.message and fake.calls() == []
    backend.capture(mode="vision", app="yad")
    assert not backend.click(element=1).ok


def test_a_marks_call_from_elsewhere_makes_elements_stale(backend, fake):
    backend.capture(app="yad")
    state = fake.base.parent / "hypruse" / "cli-state.json"
    doc = json.loads(state.read_text())
    doc["marks"]["ts"] += 5  # another process took a marks capture since
    state.write_text(json.dumps(doc))
    fake.clear()
    res = backend.click(element=1)
    assert (res.ok, res.code) == (False, "stale_element") and fake.calls() == []


def test_refusal_is_surfaced_once_and_never_retried(backend, fake):
    backend.capture(app="yad")
    fake.clear()
    fake.rule(["click_ui"], exit=3, message="yad is outside HYPRUSE_CONFINE (launched)")
    res = backend.click(element=3)
    assert (res.ok, res.code) == (False, "refused")
    assert "HYPRUSE_CONFINE" in res.message and "do not retry" in res.message
    assert res.escalation is None and res.meta["refused"] is True
    assert fake.verbs() == ["click_ui"]  # one call, no second try in any mode


def test_background_refusal_keeps_hypruse_advice(backend, fake):
    backend.capture(app="yad")
    msg = ("a right click has no accessibility action, only a left click does, so it cannot be done "
           "in the background: pass background=false to do it in the foreground")
    fake.rule(["click_ui"], exit=3, message=msg)
    res = backend.click(element=3, button="right")
    assert (res.ok, res.code) == (False, "background_unavailable") and msg in res.message


def test_modifiers_and_triple_clicks_are_unsupported(backend, fake):
    backend.capture(app="yad")
    fake.clear()
    assert backend.click(element=1, modifiers=["shift"]).code == "unsupported_action"
    assert backend.click(element=1, click_count=3).code == "unsupported_action"
    assert backend.click(element=1, delivery_mode="sideways").code == "bad_delivery_mode"
    assert fake.calls() == []


# --- clicks by coordinate -------------------------------------------------------------


def test_coordinate_click_in_the_background_is_hypruses_refusal(backend, fake):
    backend.capture(app="yad")
    fake.rule(["pointer"], exit=3, message=("the pointer moves the human's real cursor, so it cannot be "
                                            "done in the background: press a control with click_ui"))
    res = backend.click(x=100, y=100)
    # image pixel (100, 100) at scale 2 from origin (100, 50) is global (150, 100)
    assert fake.argvs()[-1] == ["pointer", "click", "150", "100", "--background"]
    assert res.code == "background_unavailable"


def test_coordinate_click_in_the_foreground_on_the_human_window(backend, fake):
    backend.capture(app="kitty")
    # the window moved by (+10, +20) since the capture: the click follows it
    desk = json.loads(json.dumps(fake.scenario["desktop"]))
    desk["windows"][0]["at"] = [974, 27]
    fake.rule(["desktop"], result=desk)
    fake.rule(["pointer"], result="click ok; cursor now at (1000, 60)")
    res = backend.click(x=50, y=40, delivery_mode="foreground")
    assert fake.argvs()[-1] == ["pointer", "click", "999", "47"]  # no placement flag: hypruse's rule holds
    assert res.ok and res.path == "pointer"


def test_coordinate_click_refused_off_the_human_window(backend, fake):
    backend.capture(app="yad")
    fake.clear()
    res = backend.click(x=100, y=100, delivery_mode="foreground")
    assert (res.ok, res.code) == (False, "target_not_frontmost")
    assert fake.verbs() == ["desktop"]  # never reached the pointer


def test_coordinate_click_after_a_resize_is_refused(backend, fake):
    backend.capture(app="kitty")
    desk = json.loads(json.dumps(fake.scenario["desktop"]))
    desk["windows"][0]["size"] = [800, 600]
    fake.rule(["desktop"], result=desk)
    res = backend.click(x=10, y=10, delivery_mode="foreground")
    assert res.code == "stale_target" and "pointer" not in fake.verbs()


def test_bring_to_front_focuses_first(backend, fake):
    backend.capture(app="yad")
    desk = json.loads(json.dumps(fake.scenario["desktop"]))
    desk["active_window"] = AGENT
    fake.rule(["hypr", "focus_window"], result=f"focused {AGENT}")
    fake.rule(["desktop"], result=desk)
    fake.rule(["pointer"], result="click ok; cursor now at (150, 100)")
    res = backend.click(x=100, y=100, delivery_mode="foreground", bring_to_front=True)
    assert res.ok
    assert ["hypr", "focus_window", AGENT] in fake.argvs()
    assert fake.argvs()[-1] == ["pointer", "click", "150", "100"]


def test_coordinates_outside_the_capture_are_refused(backend, fake):
    backend.capture(app="yad")
    fake.clear()
    res = backend.click(x=900, y=10, delivery_mode="foreground")
    assert not res.ok and "outside the last capture (800x600" in res.message and fake.calls() == []


def test_screen_capture_coordinates(backend, fake):
    backend.capture(app="screen")
    fake.rule(["pointer"], result="click ok; cursor now at (500, 250)")
    res = backend.click(x=1000, y=500, delivery_mode="foreground")
    # the monitor at scale 2, scaled by 0.379 to fit 1456: back to global
    assert res.ok
    gx, gy = (float(v) for v in fake.argvs()[-1][2:4])
    assert abs(gx - 1000 / (2 * 0.379)) < 0.01 and abs(gy - 500 / (2 * 0.379)) < 0.01


# --- drag and scroll ----------------------------------------------------------------------


def test_drag_by_elements_in_the_foreground(backend, fake):
    desk = json.loads(json.dumps(fake.scenario["desktop"]))
    desk["active_window"] = AGENT
    fake.set(desktop=desk)
    backend.capture(app="yad")
    fake.rule(["pointer"], result="drag ok; cursor now at (450, 320)")
    res = backend.drag(from_element=1, to_element=3, delivery_mode="foreground")
    assert res.ok and fake.argvs()[-1] == ["pointer", "drag", "150", "100", "450", "320"]


def test_scroll_directions_and_default_point(backend, fake):
    backend.capture(app="yad")
    fake.rule(["pointer"], exit=3, message="the pointer moves the human's real cursor, so it cannot "
                                           "be done in the background")
    backend.scroll(direction="up", amount=4)
    # the middle of the captured window
    assert fake.argvs()[-1] == ["pointer", "scroll", "-4", "0", "--at", "300", "200", "--background"]
    backend.scroll(direction="right", amount=2, element=3)
    assert fake.argvs()[-1] == ["pointer", "scroll", "0", "2", "--at", "450", "320", "--background"]


# --- keyboard -----------------------------------------------------------------------------


def test_type_sends_text_on_stdin_to_the_target_window(backend, fake):
    backend.capture(app="yad")
    fake.rule(["keyboard"], result=("typed 6 characters into the text field in yad through the "
                                    "accessibility interface (the human's focus untouched); the "
                                    "field now holds the typed text (11 characters)"))
    res = backend.type_text("secret")
    call = fake.calls()[-1]
    assert [a for a in call["argv"] if a != "--json"] == ["keyboard", "type", "-", "--window", AGENT,
                                                          "--background"]
    assert call["stdin"] == "secret" and "secret" not in " ".join(call["argv"])
    assert (res.ok, res.effect, res.path) == (True, "confirmed", "accessibility")


def test_password_refusal_is_not_retried(backend, fake):
    backend.capture(app="yad")
    fake.clear()
    fake.rule(["keyboard"], exit=3,
              message="the focused field in yad is a password field; refusing to type into it")
    res = backend.type_text("hunter2", delivery_mode="foreground")
    assert (res.ok, res.code) == (False, "refused") and fake.verbs() == ["keyboard"]


def test_key_combo(backend, fake):
    backend.capture(app="yad")
    fake.rule(["keyboard"], result=f"sent ctrl+a to {AGENT} (yad) with sendshortcut, to the control the "
                                   "app has focused (the human's focus untouched)")
    res = backend.key("ctrl+a")
    assert fake.argvs()[-1] == ["keyboard", "key", "ctrl+a", "--window", AGENT, "--background"]
    assert res.ok and res.path == "sendshortcut" and res.effect == "unverifiable"


def test_focus_warning_reaches_the_model(backend, fake):
    backend.capture(app="yad")
    fake.rule(["keyboard"], result=(f"sent enter to {AGENT} (yad) with sendshortcut, to the control the "
                                    "app has focused; WARNING: the human's focused window changed during "
                                    "this action (now 0x1 on workspace 99), so it may have opened or "
                                    "activated a window"))
    res = backend.key("Return")
    assert res.ok and "WARNING" in res.message and res.meta["warning"].startswith("the human's focused")


@pytest.mark.parametrize(("keys", "want"), [
    ("ctrl+s", "ctrl+s"), ("cmd+shift+t", "cmd+shift+t"), ("ctrl-alt-t", "ctrl+alt+t"),
    ("option+left", "alt+left"), ("Return", "Return"), ("escape", "esc"), ("ctrl+-", "ctrl+minus"),
    ("ctrl++", "ctrl+plus"), ("-", "minus"), ("windows+e", "super+e"), ("F5", "F5"),
])
def test_combo_spelling(keys, want):
    assert _normalize_combo(keys) == (want, "")


def test_fn_and_empty_keys_are_refused(backend, fake):
    backend.capture(app="yad")
    fake.clear()
    assert backend.key("fn+f1").code == "unsupported_action"
    assert backend.key("").ok is False
    assert fake.calls() == []


def test_keyboard_needs_a_target(backend, fake):
    res = backend.type_text("x")
    assert not res.ok and "capture(app=...)" in res.message and fake.calls() == []


# --- listings, focus, the rest ------------------------------------------------------------


def test_list_windows_and_apps(backend, fake):
    windows = backend.list_windows()
    yad = next(w for w in windows if w["app_name"] == "yad")
    assert yad == {"app_name": "yad", "title": "Signup", "pid": 7001, "window_id": int(AGENT, 16),
                   "address": AGENT, "workspace": 99, "bounds": [100, 50, 400, 300],
                   "on_screen": False, "focused": False, "floating": True}
    assert next(w for w in windows if w["address"] == HUMAN)["focused"] is True
    apps = {a["name"]: a for a in backend.list_apps()}
    assert set(apps) == {"kitty", "firefox", "yad"} and apps["yad"]["windows"] == 1


def test_focus_app_selects_without_raising(backend, fake):
    res = backend.focus_app("firefox")
    assert res.ok and fake.verbs() == ["desktop"] and backend._last_app == "firefox"
    fake.rule(["keyboard"], result=f"pressed ctrl+l into {FIREFOX}")
    backend.key("ctrl+l", delivery_mode="foreground")
    assert fake.argvs()[-1] == ["keyboard", "key", "ctrl+l", "--window", FIREFOX, "--foreground"]


def test_focus_app_raise_is_focus_window(backend, fake):
    fake.rule(["hypr"], result=f"focused {AGENT}")
    res = backend.focus_app("yad", raise_window=True)
    assert res.ok and fake.argvs()[-1] == ["hypr", "focus_window", AGENT]
    assert not backend.focus_app("gimp").ok


def test_set_value_is_unsupported(backend):
    assert backend.set_value("Blue", element=2).code == "unsupported_action"


def test_errors_and_no_result(backend, fake):
    backend.capture(app="yad")
    fake.rule(["click_ui"], exit=1, message="no mark 1; current marks: none, call marks() first", once=True)
    res = backend.click(element=1)
    assert (res.ok, res.code) == (False, None) and "no mark 1" in res.message
    fake.rule(["click_ui"], exit=4, result="yad exposes no accessibility tree; use screenshot + zoom instead")
    res = backend.click(element=1)
    assert (res.ok, res.code) == (False, "no_result") and "no accessibility tree" in res.message


def test_dry_run_results_say_so(backend, fake):
    backend.capture(app="yad")
    fake.rule(["click_ui"], result="DRY RUN, nothing was delivered: would press push button 'OK' in yad")
    res = backend.click(element=3)
    assert res.ok and res.meta["dry_run"] is True and res.effect is None


def test_timeout_says_the_outcome_is_unknown(fake):
    from hermes_hypruse.hypruse_cli import HypruseCLI

    cli = HypruseCLI([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    out = cli.run("keyboard", "type", "-")
    assert (out.ok, out.kind) == (False, "timeout") and "may or may not have happened" in out.message


def test_provider_registers_and_reports(fake, capsys):
    import hermes_hypruse as plugin

    seen = []

    class Ctx:
        def register_computer_use_provider(self, provider):
            seen.append(provider)

    plugin.register(Ctx())
    plugin.register(object())  # a context without the seam: no error
    (provider,) = seen
    assert provider.name == "hypruse"
    assert provider.is_available() is (sys.platform == "linux")
    backend = provider.create_backend(permission_mode="unrestricted")
    assert isinstance(backend, HypruseBackend) and backend.permission_mode == "unrestricted"
    assert provider.doctor() == 0
    assert "is hypruse 0.13.1" in capsys.readouterr().out
