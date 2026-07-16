import asyncio
import importlib

import pytest

from hypruse import server as srv

OBSERVE = {"desktop", "screenshot", "binds", "wait_for"}
ACT = {"pointer", "keyboard", "hypr", "launch"}


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
