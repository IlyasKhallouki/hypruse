"""hypruse command line.

`hypruse` with no arguments runs the MCP stdio server (what clients
spawn). Two human-facing subcommands wrap the first-run experience:

    hypruse doctor   diagnose the environment, exit 0 only if all green
    hypruse init     register hypruse in detected MCP clients (asks per
                     client, backs up configs), then run doctor

init never overwrites an existing hypruse entry: if a client already has
one, whatever its shape, it is reported and left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

DESKTOP_CONFIG = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

DESKTOP_ENTRY = {
    "command": "uvx",
    "args": ["hypruse"],
    "env": {"HYPRUSE_SCREENSHOT_MODE": "image"},
}

GENERIC_SNIPPET = """\
For any other MCP client, add a stdio server:
  command: uvx
  args: [hypruse]
"""


# --- doctor -----------------------------------------------------------------


def _check_deps() -> tuple[bool, str]:
    missing = [t for t in ("grim", "wtype") if shutil.which(t) is None]
    if missing:
        return False, f"missing: {', '.join(missing)} (install via your package manager)"
    return True, "grim, wtype found"


def _check_session() -> tuple[bool, str]:
    from hypruse import hyprctl, session

    session.ensure_session_env()
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return False, "no Hyprland instance found (is Hyprland running?)"
    try:
        monitors = hyprctl.query("monitors")
    except hyprctl.HyprctlError as exc:
        return False, str(exc)
    return True, f"instance {sig[:12]}..., {len(monitors)} monitor(s)"


def _check_events() -> tuple[bool, str]:
    from hypruse import events

    try:
        events.EventStream().close()
    except events.EventError as exc:
        return False, str(exc)
    return True, "event socket reachable"


def _check_pointer() -> tuple[bool, str]:
    from hypruse import wire

    try:
        with wire.VirtualPointer():
            pass
    except wire.WireError as exc:
        return False, str(exc)
    return True, "virtual-pointer handshake ok"


def _check_screenshot() -> tuple[bool, str]:
    from hypruse import screenshot

    try:
        data, _meta = screenshot.capture(region="0,0,8x8")
    except screenshot.ScreenshotError as exc:
        return False, str(exc)
    return True, f"grim capture ok ({len(data)} bytes)"


def _mode_note() -> tuple[bool, str]:
    parts = []
    if os.environ.get("HYPRUSE_READONLY", "").lower() in ("1", "true", "yes", "on"):
        parts.append("READ-ONLY mode")
    parts.append(f"screenshots: {os.environ.get('HYPRUSE_SCREENSHOT_MODE', 'file')} mode")
    return True, ", ".join(parts)


CHECKS = (
    ("dependencies", _check_deps),
    ("session", _check_session),
    ("events", _check_events),
    ("pointer", _check_pointer),
    ("screenshot", _check_screenshot),
    ("mode", _mode_note),
)


def doctor() -> int:
    failures = 0
    for name, check in CHECKS:
        try:
            ok, detail = check()
        except Exception as exc:  # a check must never crash the report
            ok, detail = False, f"unexpected: {exc}"
        mark = "[ok]  " if ok else "[FAIL]"
        print(f"{mark} {name:12s} {detail}")
        failures += 0 if ok else 1
    if failures:
        print(f"\n{failures} check(s) failed. See README troubleshooting.")
        return 1
    print("\nAll checks passed. hypruse is ready.")
    return 0


# --- init -------------------------------------------------------------------


def merge_desktop_config(cfg: dict) -> tuple[dict, bool]:
    """Add the hypruse server entry; never touch an existing one."""
    servers = cfg.setdefault("mcpServers", {})
    if "hypruse" in servers:
        return cfg, False
    servers["hypruse"] = dict(DESKTOP_ENTRY)
    return cfg, True


def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{question} [auto-yes]")
        return True
    if not sys.stdin.isatty():
        print(f"{question} [skipped: non-interactive, rerun with --yes]")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _init_claude_code(assume_yes: bool) -> None:
    if shutil.which("claude") is None:
        print("- Claude Code: not found on PATH, skipping")
        return
    cmd = ["claude", "mcp", "add", "-s", "user", "hypruse", "--", "uvx", "hypruse"]
    if not _ask(f"- Claude Code found. Register hypruse? (runs: {' '.join(cmd)})", assume_yes):
        return
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    last_line = out.splitlines()[-1] if out else "ok"
    print(f"  {'done' if proc.returncode == 0 else 'note'}: {last_line}")


def _init_claude_desktop(assume_yes: bool) -> None:
    if not DESKTOP_CONFIG.parent.exists():
        print("- Claude Desktop: not found, skipping")
        return
    cfg = {}
    if DESKTOP_CONFIG.exists():
        try:
            cfg = json.loads(DESKTOP_CONFIG.read_text())
        except json.JSONDecodeError:
            print(f"- Claude Desktop: {DESKTOP_CONFIG} is not valid JSON, fix it first")
            return
    merged, changed = merge_desktop_config(cfg)
    if not changed:
        print("- Claude Desktop: already configured, leaving as is")
        return
    if not _ask(
        f"- Claude Desktop found. Add hypruse to {DESKTOP_CONFIG}? (a .bak copy is kept)",
        assume_yes,
    ):
        return
    if DESKTOP_CONFIG.exists():
        backup = DESKTOP_CONFIG.with_name(DESKTOP_CONFIG.name + f".bak.{int(time.time())}")
        shutil.copy2(DESKTOP_CONFIG, backup)
        print(f"  backup: {backup}")
    DESKTOP_CONFIG.write_text(json.dumps(merged, indent=2) + "\n")
    print("  written. Restart Claude Desktop to load it.")


def init(assume_yes: bool) -> int:
    print("hypruse init: registering with detected MCP clients\n")
    _init_claude_code(assume_yes)
    _init_claude_desktop(assume_yes)
    print(f"\n{GENERIC_SNIPPET}")
    print("Running doctor:\n")
    return doctor()


# --- stop (emergency) -------------------------------------------------------


def stop() -> int:
    """Emergency stop: signal a running hypruse server to shut down, which
    releases any held pointer button and clears the beacon on the way out.
    Cleaner than `pkill -f hypruse` (it targets the beacon's own pid and
    triggers the graceful SIGTERM path), and safe to bind to a key:

        bind = SUPER SHIFT, BackSpace, exec, hypruse stop
    """
    from hypruse import safety

    path = safety.state_path()
    if not path.exists():
        print("no active hypruse session (no beacon found)")
        return 0
    try:
        pid = int(json.loads(path.read_text()).get("pid"))
    except (json.JSONDecodeError, TypeError, ValueError):
        print(f"beacon at {path} is unreadable; run: pkill -f hypruse")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        import contextlib

        with contextlib.suppress(OSError):
            path.unlink()
        print(f"hypruse pid {pid} was already gone; cleared the stale beacon")
        return 0
    except PermissionError:
        print(f"not permitted to signal pid {pid}")
        return 1
    print(f"stopped hypruse (pid {pid})")
    return 0


# --- entry ------------------------------------------------------------------

_USAGE = """\
usage: hypruse [doctor | init [--yes] | stop | --version]

no arguments   run the MCP stdio server (this is what MCP clients spawn)
doctor         diagnose dependencies, session, protocols; exit 0 if green
init           register hypruse in detected MCP clients, then run doctor
stop           emergency stop: signal a running server to shut down safely
               (bind it: bind = SUPER SHIFT, BackSpace, exec, hypruse stop)
"""


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        from hypruse.server import main as server_main

        server_main()
        return
    if argv[0] == "doctor":
        sys.exit(doctor())
    if argv[0] == "init":
        sys.exit(init(assume_yes="--yes" in argv[1:]))
    if argv[0] == "stop":
        sys.exit(stop())
    print(_USAGE, file=sys.stderr)
    sys.exit(2)
