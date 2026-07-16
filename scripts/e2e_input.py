"""Human-supervised input verification. TAKES OVER cursor + keyboard ~10s.

Run it, take your hands off, watch: it opens a floating kitty on your
current workspace, clicks into it, scrolls, types a marker — then reads
the terminal's actual screen text back over kitty remote control and
asserts every event arrived. Restores focus and cleans up afterwards.

    uv run python scripts/e2e_input.py
"""

import contextlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from hypruse import hyprctl
from hypruse import input as hinput

MARKER = "hypruse-e2e-ok"


def kitten_text(sock: str) -> str:
    out = subprocess.run(
        ["kitten", "@", "--to", f"unix:{sock}", "get-text", "--extent", "screen"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return out.stdout


def main() -> int:
    for n in (3, 2, 1):
        print(f"hands off in {n}…", flush=True)
        time.sleep(1)

    restore_win = (hyprctl.query("activewindow") or {}).get("address")
    restore_cur = hyprctl.cursor_pos()
    sock = str(Path(tempfile.gettempdir()) / f"hypruse-e2e-{int(time.time())}.sock")

    # cat with mouse reporting on: every click/scroll/key becomes visible text
    inner = (
        r"printf '\e[?1000;1006h' > /dev/tty; stty -icanon -echo min 1 time 0; exec cat -v"
    )
    hyprctl.dispatch(
        "exec",
        "[float; size 900 500; center] kitty --title hypruse-e2e "
        f"-o allow_remote_control=yes --listen-on unix:{sock} bash -c \"{inner}\"",
    )

    win = None
    for _ in range(40):
        time.sleep(0.15)
        win = next(
            (c for c in hyprctl.query("clients") if c.get("title") == "hypruse-e2e"), None
        )
        if win:
            break
    if not win:
        print("FAIL: test window never appeared")
        return 1

    (x, y), (w, h) = win["at"], win["size"]
    cx, cy = x + w // 2, y + h // 2
    ok = True
    try:
        time.sleep(0.6)  # let the shell inside settle
        hinput.click(cx, cy)  # focuses the float and lands a button event
        time.sleep(0.3)
        hinput.scroll(dy=2, x=cx, y=cy)
        time.sleep(0.3)
        hinput.type_text(MARKER)
        time.sleep(0.5)

        text = kitten_text(sock)
        checks = {
            "click (SGR press/release)": "[<0;" in text,
            "scroll (SGR wheel)": "[<64;" in text or "[<65;" in text,
            f"typed marker {MARKER!r}": MARKER in text,
        }
        for name, passed in checks.items():
            print(("PASS " if passed else "FAIL ") + name)
            ok = ok and passed
    finally:
        addr = win["address"]
        pid = win.get("pid")
        if pid:
            subprocess.run(["kill", str(pid)], capture_output=True)
        else:
            hyprctl.dispatch("closewindow", f"address:{addr}")
        time.sleep(0.3)
        hinput.move(*restore_cur)
        if restore_win:
            with contextlib.suppress(hyprctl.HyprctlError):
                hyprctl.dispatch("focuswindow", f"address:{restore_win}")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
