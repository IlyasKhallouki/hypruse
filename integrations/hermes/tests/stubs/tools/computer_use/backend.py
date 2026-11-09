"""Stand-in for hermes-agent's ``tools/computer_use/backend.py`` (the provider
contract), so the unit tests run with nothing but pytest. Field for field the
same as hermes-agent at 8c1a9c0 (2026-11-09). The contract tests run the
provider against the real module instead (HERMES_AGENT_SRC)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent.provider_base import ProviderBase


@dataclass
class UIElement:
    index: int
    role: str
    label: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    app: str = ""
    pid: int = 0
    window_id: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    element_token: str | None = None


@dataclass
class CaptureResult:
    mode: str
    width: int
    height: int
    png_b64: str | None = None
    elements: list[UIElement] = field(default_factory=list)
    app: str = ""
    window_title: str = ""
    png_bytes_len: int = 0
    image_mime_type: str | None = None
    note: str = ""
    ax_max_elements: int = 0


@dataclass
class ActionResult:
    ok: bool
    action: str
    message: str = ""
    capture: CaptureResult | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    effect: str | None = None
    escalation: dict[str, Any] | None = None
    path: str | None = None
    degraded: bool | None = None
    delivery_mode: str | None = None
    code: str | None = None


class ComputerUseBackend(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def capture(self, mode: str = "som", app: str | None = None, pid: int | None = None,
                window_id: int | None = None) -> CaptureResult: ...

    @abstractmethod
    def click(self, *, element=None, x=None, y=None, button="left", click_count=1, modifiers=None,
              delivery_mode=None, bring_to_front=False) -> ActionResult: ...

    @abstractmethod
    def drag(self, *, from_element=None, to_element=None, from_xy=None, to_xy=None, button="left",
             modifiers=None, delivery_mode=None, bring_to_front=False) -> ActionResult: ...

    @abstractmethod
    def scroll(self, *, direction, amount=3, element=None, x=None, y=None, modifiers=None,
               delivery_mode=None, bring_to_front=False) -> ActionResult: ...

    @abstractmethod
    def type_text(self, text, *, delivery_mode=None, bring_to_front=False) -> ActionResult: ...

    @abstractmethod
    def key(self, keys, *, delivery_mode=None, bring_to_front=False) -> ActionResult: ...

    @abstractmethod
    def list_apps(self) -> list[dict[str, Any]]: ...

    def list_windows(self) -> list[dict[str, Any]]:
        return []

    @abstractmethod
    def focus_app(self, app, raise_window=False) -> ActionResult: ...

    @abstractmethod
    def set_value(self, value, element=None) -> ActionResult: ...

    def wait(self, seconds: float) -> ActionResult:
        time.sleep(max(0.0, min(seconds, 30.0)))
        return ActionResult(ok=True, action="wait", message=f"waited {seconds:.2f}s")


class ComputerUseProvider(ProviderBase):
    @abstractmethod
    def create_backend(self, *, permission_mode: str) -> ComputerUseBackend: ...

    def is_available(self) -> bool:
        return True

    def doctor(self) -> int | None:
        return None
