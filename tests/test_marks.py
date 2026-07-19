"""Set-of-Marks capture (marks) and click-by-name/mark (click_ui)."""

import json
import shutil
import subprocess

import pytest
from mcp.types import TextContent

from hypruse import server as srv


def make_client():
    return {
        "address": "0xa", "class": "gedit", "title": "doc",
        "at": [100, 50], "size": [800, 600], "pid": 42,
    }


ELEMENTS = [
    {"role": "push button", "name": "Save", "x": 150, "y": 80, "clickable": True},
    {"role": "push button", "name": "Save As", "x": 250, "y": 80, "clickable": True},
    {"role": "text", "name": "Body", "x": 400, "y": 300, "clickable": True, "value": "hi"},
]

META = {
    "target": "window", "geometry": [100, 50, 800, 600],
    "image": [800, 600], "format": "jpeg", "scale": 1.0,
}


@pytest.fixture
def wired(monkeypatch):
    calls = {"clicks": [], "dispatch": []}
    client = make_client()
    monkeypatch.setattr(srv.safety, "touch", lambda *a: None)
    monkeypatch.setattr(srv, "_resolve_window", lambda w="": client)
    monkeypatch.setattr(
        srv, "_ui_read",
        lambda w="", name="", actionable=True: [
            e for e in ELEMENTS if name.lower() in e["name"].lower()
        ],
    )
    monkeypatch.setattr(srv.hyprctl, "dispatch", lambda *a: calls["dispatch"].append(a))
    monkeypatch.setattr(
        srv.hinput, "click",
        lambda x, y, button="left", double=False: calls["clicks"].append((x, y, button)),
    )
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)
    calls["client"] = client
    return calls


def test_click_ui_exact_name_wins_over_substring(wired):
    # 'Save' substring-matches both buttons, but exactly one exact match
    out = srv.click_ui(name="Save")
    assert wired["clicks"] == [(150, 80, "left")]
    assert ("focuswindow", "address:0xa") in wired["dispatch"]  # focused first
    assert "Save" in out


def test_click_ui_ambiguous_returns_candidates(wired):
    out = srv.click_ui(name="Sav")
    assert wired["clicks"] == []  # never guesses
    assert isinstance(out, list) and "ambiguous" in out[0].text
    names = [e["name"] for e in json.loads(out[1].text)]
    assert names == ["Save", "Save As"]


def test_click_ui_index_disambiguates(wired):
    out = srv.click_ui(name="Sav", index=1)
    assert wired["clicks"] == [(250, 80, "left")]
    assert "Save As" in out
    with pytest.raises(ValueError, match="out of range"):
        srv.click_ui(name="Sav", index=5)


def test_click_ui_requires_exactly_one_selector(wired):
    with pytest.raises(ValueError, match="exactly one"):
        srv.click_ui()
    with pytest.raises(ValueError, match="exactly one"):
        srv.click_ui(name="Save", mark=1)


def test_click_ui_no_tree_falls_back(wired, monkeypatch):
    monkeypatch.setattr(srv, "_ui_read", lambda *a, **k: "gedit exposes no tree")
    out = srv.click_ui(name="Save")
    assert out == "gedit exposes no tree"
    assert wired["clicks"] == []


def test_marks_builds_legend_and_stores_relative_offsets(wired, monkeypatch):
    monkeypatch.setattr(srv, "_grab_env", lambda **kw: (b"IMG", dict(META)))
    drawn = {}

    def fake_draw(data, fmt, points):
        drawn["points"] = points
        return b"MARKED"

    monkeypatch.setattr(srv, "_draw_marks", fake_draw)
    monkeypatch.setattr(
        srv, "_package", lambda data, meta: [TextContent(type="text", text=f"pkg:{len(data)}")]
    )
    out = srv.marks()
    # mark pixels: (global - origin) * scale
    assert drawn["points"] == [(1, 50, 30), (2, 150, 30), (3, 300, 250)]
    legend = json.loads(out[-1].text)["legend"]
    assert [x["mark"] for x in legend] == [1, 2, 3]
    assert legend[2]["value"] == "hi"
    # stored offsets are window-relative, re-anchored at click time
    assert srv._last_marks["items"][1] == {"dx": 50, "dy": 30, "label": "push button 'Save'"}
    assert out[0].text == "pkg:6"  # the MARKED image was packaged, not the original


def test_marks_pixel_math_honors_scale(wired, monkeypatch):
    meta = dict(META, scale=2.0)
    monkeypatch.setattr(srv, "_grab_env", lambda **kw: (b"IMG", meta))
    drawn = {}
    monkeypatch.setattr(
        srv, "_draw_marks", lambda d, f, p: drawn.update(points=p) or b"M"
    )
    monkeypatch.setattr(srv, "_package", lambda data, meta: [])
    srv.marks()
    assert drawn["points"][0] == (1, 100, 60)  # (150-100)*2, (80-50)*2


def test_marks_without_imagemagick_returns_legend(wired, monkeypatch):
    monkeypatch.setattr(srv, "_grab_env", lambda **kw: (b"IMG", dict(META)))
    monkeypatch.setattr(srv, "_draw_marks", lambda *a: None)
    out = srv.marks()
    assert "ImageMagick not found" in out[0].text
    assert json.loads(out[1].text)["legend"]  # coordinates still exact


def test_marks_no_tree_falls_back(wired, monkeypatch):
    monkeypatch.setattr(srv, "_ui_read", lambda *a, **k: "no tree")
    assert srv.marks() == "no tree"


def test_click_mark_reanchors_to_moved_window(wired, monkeypatch):
    monkeypatch.setattr(srv, "_grab_env", lambda **kw: (b"IMG", dict(META)))
    monkeypatch.setattr(srv, "_draw_marks", lambda *a: b"M")
    monkeypatch.setattr(srv, "_package", lambda data, meta: [])
    srv.marks()
    wired["client"]["at"] = [300, 200]  # the window moved since the capture
    out = srv.click_ui(mark=1)
    assert wired["clicks"] == [(350, 230, "left")]  # 300+50, 200+30
    assert "mark 1" in out


def test_click_unknown_mark_raises(wired):
    srv._last_marks = {}
    with pytest.raises(ValueError, match="no mark"):
        srv.click_ui(mark=7)


@pytest.mark.skipif(shutil.which("magick") is None, reason="needs imagemagick")
def test_draw_marks_real_imagemagick(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    src = tmp_path / "src.jpg"
    subprocess.run(["magick", "-size", "60x40", "xc:white", str(src)], check=True)
    from hypruse import screenshot as shot

    out = srv._draw_marks(src.read_bytes(), "jpeg", [(1, 15, 15), (12, 45, 25)])
    assert out is not None and out != src.read_bytes()
    assert shot.image_size(out) == (60, 40)  # dims unchanged, marks drawn


def test_click_ui_then_ui_observes_the_clicked_window(wired, monkeypatch):
    # the click may hand focus to a dialog it spawned; then='ui' must show
    # the window the agent clicked, not whatever holds focus afterwards
    reads = []

    def ui_read(w="", name="", actionable=True):
        reads.append(w)
        return [e for e in ELEMENTS if name.lower() in e["name"].lower()]

    monkeypatch.setattr(srv, "_ui_read", ui_read)
    srv.click_ui(name="Body", then="ui")
    assert reads == ["0xa", "0xa"]  # resolution, then the fused observation
