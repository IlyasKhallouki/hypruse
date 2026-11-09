"""hypruse as a Hermes computer-use provider (``computer_use.backend: hypruse``).

Hermes finds computer-use providers by directory, not by entry point: this
directory goes to ``$HERMES_HOME/plugins/hypruse/`` (``hermes plugins
install``, a symlink, or ``hermes-hypruse install``), and ``register(ctx)``
hands Hermes a :class:`HypruseProvider`. The backend drives the ``hypruse``
CLI, so it needs hypruse 0.13 or newer on PATH and nothing in Hermes'
environment.
"""

from __future__ import annotations

import subprocess
import sys

from tools.computer_use.backend import ComputerUseBackend, ComputerUseProvider

__version__ = "0.1.0"


class HypruseProvider(ComputerUseProvider):
    name = "hypruse"
    display_name = "hypruse (Hyprland, background mode)"

    def create_backend(self, *, permission_mode: str) -> ComputerUseBackend:
        from .backend import HypruseBackend

        return HypruseBackend(permission_mode=permission_mode)

    def is_available(self) -> bool:
        """Linux, a reachable Hyprland session and a hypruse command. Starts nothing."""
        from .backend import load_options, session_reachable
        from .hypruse_cli import command_found, resolve_command

        if sys.platform != "linux":
            return False
        return command_found(resolve_command(load_options().get("command"))) and session_reachable()

    def get_setup_schema(self) -> dict:
        return {"name": self.display_name, "badge": "",
                "tag": "Hyprland: accessibility clicks and typing on the agent's own workspaces",
                "env_vars": []}

    def doctor(self) -> int:
        """``hermes computer-use doctor``: the command and its version, then hypruse's
        own ``doctor``."""
        from .backend import load_options
        from .hypruse_cli import MIN_VERSION, HypruseCLI, child_env, command_found, resolve_command

        command = resolve_command(load_options().get("command"))
        print(f"hypruse provider {__version__} for Hermes")
        if not command_found(command):
            print(f"  [FAIL] hypruse command {command[0]!r} not found (install hypruse, or set "
                  "HERMES_HYPRUSE_CMD or computer_use.hypruse.command)")
            return 1
        version = HypruseCLI(command).version()
        wanted = ".".join(map(str, MIN_VERSION))
        if version is None or version < MIN_VERSION:
            found = ".".join(map(str, version)) if version else "an unknown version"
            print(f"  [FAIL] {' '.join(command)} is {found}; this provider needs {wanted} or newer")
            return 1
        print(f"  [ok]   {' '.join(command)} is hypruse {'.'.join(map(str, version))}")
        try:
            proc = subprocess.run([*command, "doctor"], capture_output=True, text=True,  # noqa: S603
                                  timeout=60, env=child_env(), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"  [FAIL] hypruse doctor did not run: {exc}")
            return 1
        print(proc.stdout.rstrip())
        return proc.returncode


def register(ctx) -> None:
    # `hermes plugins validate` probes register() with a generic context that may
    # not have the computer-use seam; the provider loader's context does.
    register_provider = getattr(ctx, "register_computer_use_provider", None)
    if register_provider is not None:
        register_provider(HypruseProvider())
