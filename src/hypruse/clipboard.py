"""Clipboard via wl-clipboard (wl-copy / wl-paste).

An opt-in surface: the server registers the clipboard tool only when
HYPRUSE_CLIPBOARD=1 is set, so a default install keeps its documented
"no clipboard access" posture. Text only; non-text content (images,
files) is reported as such rather than returned as bytes.
"""

from __future__ import annotations

import shutil
import subprocess


class ClipboardError(RuntimeError):
    """wl-clipboard missing or the clipboard operation failed."""


def _tool(name: str) -> str:
    if shutil.which(name) is None:
        raise ClipboardError(f"{name} not found, install wl-clipboard for clipboard access")
    return name


def read() -> str:
    """The clipboard's text content; empty string when nothing is copied."""
    proc = subprocess.run(
        [_tool("wl-paste"), "--no-newline"], capture_output=True, timeout=5
    )
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        if "Nothing is copied" in err:
            return ""
        raise ClipboardError(f"wl-paste failed: {err}")
    try:
        return proc.stdout.decode()
    except UnicodeDecodeError:
        raise ClipboardError(
            f"clipboard holds non-text content ({len(proc.stdout)} bytes); "
            "only text is supported"
        ) from None


def write(text: str) -> None:
    # wl-copy forks a daemon to keep serving the clipboard; it inherits
    # captured stdout/stderr pipes and never closes them, so capturing
    # output here hangs until the timeout. DEVNULL both; only the exit
    # code of the (immediately-returning) parent is meaningful.
    proc = subprocess.run(
        [_tool("wl-copy")],
        input=text.encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    if proc.returncode != 0:
        raise ClipboardError(f"wl-copy failed (exit {proc.returncode})")
