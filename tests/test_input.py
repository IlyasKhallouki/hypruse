import pytest

from hypruse import input as hinput


def test_parse_combo_mods_and_key():
    assert hinput.parse_combo("ctrl+shift+t") == (["ctrl", "shift"], "t")
    assert hinput.parse_combo("super+enter") == (["logo"], "Return")
    assert hinput.parse_combo("Alt+F4") == (["alt"], "F4")
    assert hinput.parse_combo("esc") == ([], "Escape")
    assert hinput.parse_combo("q") == ([], "q")


def test_parse_combo_bare_modifier_tap():
    assert hinput.parse_combo("super") == (["logo"], None)


def test_parse_combo_passes_unknown_keysyms_through():
    assert hinput.parse_combo("XF86AudioPlay") == ([], "XF86AudioPlay")


def test_parse_combo_rejects_garbage():
    with pytest.raises(hinput.InputError):
        hinput.parse_combo("")
    with pytest.raises(hinput.InputError, match="unknown modifier"):
        hinput.parse_combo("banana+t")


def test_combo_to_wtype_args_press_release_order():
    args = hinput.combo_to_wtype_args(["ctrl", "shift"], "t")
    assert args == ["-M", "ctrl", "-M", "shift", "-k", "t", "-m", "shift", "-m", "ctrl"]
    assert hinput.combo_to_wtype_args(["logo"], None) == ["-M", "logo", "-m", "logo"]


def test_click_validates_before_touching_compositor():
    with pytest.raises(hinput.InputError, match="both x and y"):
        hinput.click(x=100)  # y missing — must fail before any socket use
    with pytest.raises(hinput.InputError, match="unknown button"):
        hinput.click(button="laser")


def test_scroll_requires_a_direction():
    with pytest.raises(hinput.InputError, match="non-zero"):
        hinput.scroll()
