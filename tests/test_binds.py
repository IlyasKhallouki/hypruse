import pytest

from hypruse import hyprctl


@pytest.mark.parametrize(
    "mask,names",
    [
        (0, []),
        (64, ["SUPER"]),
        (5, ["CTRL", "SHIFT"]),
        (65, ["SUPER", "SHIFT"]),
        (72, ["SUPER", "ALT"]),
    ],
)
def test_modmask_decode(mask, names):
    assert hyprctl.modmask_to_names(mask) == names


RAW = [
    {
        "modmask": 64,
        "key": "Q",
        "dispatcher": "exec",
        "arg": "kitty",
        "has_description": True,
        "description": "open terminal",
        "submap": "",
        "mouse": False,
        "keycode": 0,
    },
    {
        "modmask": 64,
        "key": "mouse:272",
        "dispatcher": "movewindow",
        "arg": "",
        "submap": "",
        "mouse": True,
        "keycode": 0,
    },
    {
        "modmask": 65,
        "key": "",
        "dispatcher": "movetoworkspace",
        "arg": "2",
        "submap": "",
        "mouse": False,
        "keycode": 10,
    },
    {
        "modmask": 0,
        "key": "escape",
        "dispatcher": "submap",
        "arg": "reset",
        "submap": "resize",
        "mouse": False,
        "keycode": 0,
    },
]


def test_parse_binds_shapes():
    parsed = hyprctl.parse_binds(RAW)
    combos = [b["combo"] for b in parsed]
    assert combos == ["SUPER+Q", "SUPER+SHIFT+code:10", "escape"]
    assert parsed[0]["description"] == "open terminal"
    assert parsed[0]["action"] == "exec" and parsed[0]["arg"] == "kitty"
    assert "description" not in parsed[1]
    assert parsed[2]["submap"] == "resize"


def test_parse_binds_drops_mouse_binds():
    parsed = hyprctl.parse_binds(RAW)
    assert not any("mouse" in b["combo"] for b in parsed)
