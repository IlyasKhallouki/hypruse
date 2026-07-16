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
import sys

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


@needs_hyprland
def test_screenshot_roundtrip_file_mode():
    result = call("screenshot", {}, "file")
    assert not result.isError, result.content
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    assert any("screenshot written to" in t for t in texts)
    path = texts[0].split(" ")[3]
    with open(path, "rb") as f:
        assert f.read(8) == PNG_MAGIC


JPEG_MAGIC = b"\xff\xd8\xff"


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
