"""Full MCP stdio round-trips against a live session:  pytest -m e2e

This is the layer that catches serialization bugs the unit tests cannot:
tool results here have passed through FastMCP's converter and the MCP
wire, exactly what an MCP client receives. Regression source: a desktop
client got 'Unable to serialize unknown type: …fastmcp…Image' from
screenshot's image mode while unit tests were green.
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent

pytestmark = pytest.mark.e2e

needs_hyprland = pytest.mark.skipif(
    not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
    reason="no live Hyprland session",
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def call(tool: str, args: dict, mode: str):
    async def go():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "hypruse"],
            env={**os.environ, "HYPRUSE_SCREENSHOT_MODE": mode},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args)

    return asyncio.run(go())


@needs_hyprland
def test_desktop_roundtrip():
    result = call("desktop", {}, "file")
    assert not result.isError
    text = next(c.text for c in result.content if isinstance(c, TextContent))
    state = json.loads(text)
    assert state["monitors"]


JPEG_MAGIC = b"\xff\xd8\xff"


@needs_hyprland
def test_screenshot_roundtrip_file_mode():
    # default is fast JPEG
    result = call("screenshot", {}, "file")
    assert not result.isError, result.content
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    assert any("screenshot saved" in t for t in texts)
    path = texts[0].split()[-1]  # path is the final token, nothing after it
    with open(path, "rb") as f:
        assert f.read(3) == JPEG_MAGIC


@needs_hyprland
def test_screenshot_roundtrip_lossless_file_mode():
    # lossless=true opts back into PNG
    result = call("screenshot", {"lossless": True}, "file")
    assert not result.isError, result.content
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    path = texts[0].split()[-1]
    with open(path, "rb") as f:
        assert f.read(8) == PNG_MAGIC


@needs_hyprland
def test_screenshot_roundtrip_image_mode():
    result = call("screenshot", {}, "image")
    assert not result.isError, result.content
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert images, f"no ImageContent in {[type(c).__name__ for c in result.content]}"
    raw = base64.b64decode(images[0].data)
    assert images[0].mimeType in ("image/png", "image/jpeg")
    assert raw[:8] == PNG_MAGIC or raw[:3] == JPEG_MAGIC
    assert len(raw) <= 700_000, "image mode must respect the transport budget"
    metas = [c.text for c in result.content if isinstance(c, TextContent)]
    assert "geometry" in metas[-1]


@needs_hyprland
def test_zoom_roundtrip_file_mode():
    result = call("zoom", {"x": 400, "y": 300}, "file")
    assert not result.isError, result.content
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    meta = json.loads(texts[-1])
    assert meta["target"] == "zoom"
    assert meta["point"] == [400, 300]
    assert meta["geometry"][2:] == [480, 360]
    # native resolution: image size is the logical box times the monitor
    # scale (whatever that scale is), per the mapping contract
    assert abs(meta["image"][0] - 480 * meta["scale"]) <= 2
    assert abs(meta["image"][1] - 360 * meta["scale"]) <= 2
    path = texts[0].split()[-1]
    with open(path, "rb") as f:
        assert f.read(3) == JPEG_MAGIC


needs_yad = pytest.mark.skipif(shutil.which("yad") is None, reason="yad not installed")


@needs_hyprland
@needs_yad
def test_ui_roundtrip_reads_a11y_tree():
    """Launch a real GTK dialog, ask the ui tool for its tree over MCP, and
    confirm it returns the named buttons with global click points inside
    the window. Exercises AT-SPI + busctl + coordinate mapping end to end."""
    from hypruse import hyprctl

    subprocess.run(
        ["hyprctl", "dispatch", "exec",
         "[float; center] yad --title=hypruse-mcp-test --button=Approve:0 "
         "--button=Deny:1 --width=400 --height=200"],
        capture_output=True,
    )
    try:
        win = None
        for _ in range(20):
            time.sleep(0.2)
            win = next((c for c in hyprctl.query("clients") if c.get("class") == "yad"), None)
            if win:
                break
        assert win, "yad did not launch"
        result = call("ui", {"window": win["address"]}, "file")
        assert not result.isError, result.content
        # FastMCP serializes a list return as one content block per element
        elements = [json.loads(c.text) for c in result.content if isinstance(c, TextContent)]
        names = {e["name"]: e for e in elements}
        assert "Approve" in names and "Deny" in names
        ax, ay = win["at"]
        aw, ah = win["size"]
        for e in elements:  # every click point lands inside the window
            assert ax <= e["x"] < ax + aw and ay <= e["y"] < ay + ah
            assert e["clickable"] is True
    finally:
        subprocess.run(["hyprctl", "dispatch", "closewindow", "class:yad"], capture_output=True)
