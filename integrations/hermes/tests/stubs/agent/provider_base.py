"""Stand-in for hermes-agent's ``agent/provider_base.py`` (see the tools stub)."""

from __future__ import annotations

import abc
from typing import Any


class ProviderBase(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @property
    def display_name(self) -> str:
        return self.name

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": self.display_name, "badge": "", "tag": "", "env_vars": []}
