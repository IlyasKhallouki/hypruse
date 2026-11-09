import pytest

from hypruse import keymap

# ask the module itself, so a build that points it at the library by path
# (Nix, where find_library sees nothing) runs these tests instead of skipping
pytestmark = pytest.mark.skipif(keymap._lib() is None, reason="libxkbcommon not installed")


def test_us_layout():
    m = keymap.Keymap("us")
    assert m.chord("a") == ("", "a")
    assert m.chord("A") == ("SHIFT", "a")
    assert m.chord("1") == ("", "1")
    assert m.chord("!") == ("SHIFT", "1")
    assert m.chord(" ") == ("", "space")


def test_french_azerty():
    m = keymap.Keymap("fr")
    assert m.chord("1") == ("SHIFT", "ampersand")  # digits are shifted on AZERTY
    assert m.chord("&") == ("", "ampersand")
    assert m.chord("@") == ("MOD5", "agrave")  # AltGr
    assert m.chord("é") == ("", "eacute")


def test_a_character_the_layout_cannot_type_is_none():
    assert keymap.Keymap("us").chord("上") is None


def test_control_characters_map_to_their_keys():
    m = keymap.Keymap("us")
    assert m.chord("\n") == ("", "Return")
    assert m.chord("\t") == ("", "Tab")


def test_a_keypad_name_is_not_turned_into_a_main_row_key():
    # KP_1 is not "1": on AZERTY that would be Shift and ampersand
    assert keymap.for_layout("fr").chord_for_key("KP_1") is None
