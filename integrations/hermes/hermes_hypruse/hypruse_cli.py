"""The hypruse shell contract, as this provider uses it.

Every call is one ``hypruse <verb> ... --json`` process. The verb prints one
JSON line on stdout (``{"ok", "tool", "result"}``) and exits 0 delivered,
1 error, 2 usage, 3 refused, 4 ran with no result. On 1, 2 and 3 stdout is
empty and stderr carries one ``hypruse: <kind>: <message>`` line. This
module turns that into an :class:`Outcome` and nothing else: it never
retries, so a refusal reaches the caller exactly once.

Why the CLI and not hypruse's Python functions: Hermes runs in its own
virtual environment with exact pins, hypruse lives in another (AUR, uv
tool, pipx), and the CLI wrapper is where hypruse applies its read-only
gate, its cross-process input lock, the journal and the state that carries
a marks numbering from one call to the next. Calling the functions
in-process would skip all of that.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_REFUSED, EXIT_NORESULT = 0, 1, 2, 3, 4
CMD_ENV = "HERMES_HYPRUSE_CMD"
MIN_VERSION = (0, 13, 0)  # background mode, click_ui --mark across CLI calls
_STDERR = re.compile(r"^hypruse: (error|refused|usage): (.*)$")
_VERSION = re.compile(r"hypruse (\d+)\.(\d+)\.(\d+)")

# The child gets the desktop session and hypruse's own settings, never the
# API keys and tokens Hermes holds. HYPRUSE_* are the owner's trust layers
# and must reach hypruse unchanged.
_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ",
    "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "HYPRLAND_INSTANCE_SIGNATURE",
    "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS", "XKB_DEFAULT_LAYOUT",
})
_ENV_PREFIXES = ("HYPRUSE_", "XDG_", "LC_")


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if k in _ENV_NAMES or k.startswith(_ENV_PREFIXES)}


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def resolve_command(configured: Any = None) -> list[str]:
    """The hypruse command: HERMES_HYPRUSE_CMD, else computer_use.hypruse.command,
    else ``hypruse`` on PATH. A string is split like a shell would."""
    raw = os.environ.get(CMD_ENV, "").strip() or configured or "hypruse"
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw if str(part)]
    return shlex.split(str(raw))


def command_found(command: list[str]) -> bool:
    """Cheap: no process is started."""
    if not command:
        return False
    exe = command[0]
    if os.sep in exe:
        return os.access(exe, os.X_OK)
    return shutil.which(exe) is not None


@dataclass
class Outcome:
    code: int
    result: Any = None  # the `result` of the --json line
    kind: str = ""  # "error" | "refused" | "usage" | "timeout" | ""
    message: str = ""  # stderr message without its "hypruse: kind: " prefix
    stdout: str = ""

    @property
    def ok(self) -> bool:
        return self.code == EXIT_OK

    @property
    def refused(self) -> bool:
        return self.code == EXIT_REFUSED

    def text(self) -> str:
        """The result as one string: a plain result, or the text blocks joined."""
        if isinstance(self.result, str):
            return self.result
        if isinstance(self.result, list):
            parts = [b.get("text", "") for b in self.result
                     if isinstance(b, dict) and b.get("type") == "text"]
            if parts:
                return "\n".join(p for p in parts if p)
        if self.result is not None:
            return json.dumps(self.result)
        return self.message


class HypruseCLI:
    def __init__(self, command: list[str], timeout: float = 60.0) -> None:
        self.command = list(command)
        self.timeout = timeout

    def run(self, *args: str, stdin: str | None = None, timeout: float | None = None,
            json_out: bool = True) -> Outcome:
        argv = [*self.command, *args, *(["--json"] if json_out else [])]
        limit = timeout or self.timeout
        try:
            proc = subprocess.run(  # noqa: S603 (argv list, no shell)
                argv, input=stdin if stdin is not None else "",
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=limit, env=child_env(), check=False,
            )
        except FileNotFoundError:
            return Outcome(EXIT_ERROR, kind="error", message=(
                f"the hypruse command {self.command[0]!r} was not found; install hypruse "
                f"0.13 or newer, or set {CMD_ENV} or computer_use.hypruse.command"))
        except subprocess.TimeoutExpired:
            return Outcome(EXIT_ERROR, kind="timeout", message=(
                f"hypruse {args[0] if args else ''} did not answer within {limit:.0f}s; "
                "if it was an action, it may or may not have happened, so capture before "
                "doing it again"))
        except OSError as exc:
            return Outcome(EXIT_ERROR, kind="error", message=f"could not run hypruse: {exc}")
        return parse(proc.returncode, proc.stdout, proc.stderr, json_out=json_out)

    def version(self) -> tuple[int, int, int] | None:
        out = self.run("--version", json_out=False, timeout=15)
        match = _VERSION.search(out.stdout or "")
        return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def parse(code: int, stdout: str, stderr: str, *, json_out: bool = True) -> Outcome:
    message, kind = "", ""
    for line in reversed((stderr or "").strip().splitlines()):
        match = _STDERR.match(line.strip())
        if match:
            kind, message = match.group(1), match.group(2)
            break
    if not message and code not in (EXIT_OK, EXIT_NORESULT):  # 4 says why on stdout
        tail = (stderr or "").strip().splitlines()
        message = tail[-1] if tail else f"hypruse exited {code}"
        kind = kind or "error"
    result: Any = None
    if json_out:
        for line in reversed((stdout or "").strip().splitlines()):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and "result" in doc:
                result = doc["result"]
                break
    return Outcome(code, result=result, kind=kind, message=message, stdout=stdout or "")


# --- reading results ---------------------------------------------------------------


def text_blocks(result: Any) -> list[str]:
    if isinstance(result, list):
        return [b["text"] for b in result
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
    return []


def capture_parts(result: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, list[str]]:
    """(capture metadata, marks legend, other notes) from a screenshot or marks result."""
    meta, legend, notes = None, None, []
    for text in text_blocks(result):
        if text.startswith("screenshot saved, read this file"):
            continue
        try:
            doc = json.loads(text)
        except ValueError:
            notes.append(text)
            continue
        if isinstance(doc, dict) and "geometry" in doc and "path" in doc:
            meta = doc
        elif isinstance(doc, dict) and isinstance(doc.get("legend"), list):
            legend = [e for e in doc["legend"] if isinstance(e, dict)]
        else:
            notes.append(text)
    return meta, legend, notes


def marks_record() -> dict[str, Any] | None:
    """The marks numbering hypruse keeps between CLI calls: its window and when it
    was taken, or None when the state cannot be read. Read only, to notice that a
    marks call from elsewhere replaced ours before a click by number."""
    try:
        state = json.loads((runtime_dir() / "hypruse" / "cli-state.json").read_text())
    except (OSError, ValueError):
        return None
    marks = state.get("marks") if isinstance(state, dict) else None
    if not isinstance(marks, dict) or not isinstance(marks.get("window"), str):
        return None
    return {"window": marks["window"], "ts": marks.get("ts")}
