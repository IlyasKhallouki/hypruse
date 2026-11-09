"""A stand-in for the ``hypruse`` CLI: the same argv, stdout, stderr and exit
codes as the real shell contract, from a scenario file, with no desktop.

Everything lives under ``$XDG_RUNTIME_DIR`` (the one variable the provider is
sure to pass through): ``fake-hypruse/scenario.json`` says what to answer,
``fake-hypruse/calls.jsonl`` records every call (argv and stdin), and captures
land in ``hypruse/`` like the real ones, with marks state in
``hypruse/cli-state.json``.

Scenario keys: ``version``; ``desktop``; ``scale`` (the window or monitor
scale, default 1.0); ``legend`` (marks items); ``marks`` ("ok", "no_tree",
"no_magick"); ``rules``, a list of ``{"argv": [...prefix], "exit": N,
"result": ..., "message": "...", "once": bool}`` matched against the argv
without ``--json``, first match wins.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

RUN = Path(os.environ["XDG_RUNTIME_DIR"])
BASE = RUN / "fake-hypruse"
KINDS = {1: "error", 2: "usage", 3: "refused"}


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def emit(tool: str, result, code: int = 0) -> int:
    print(json.dumps({"ok": code == 0, "tool": tool, "result": result}, separators=(",", ":")))
    return code


def fail(code: int, message: str) -> int:
    print(f"hypruse: {KINDS.get(code, 'error')}: {message}", file=sys.stderr)
    return code


def flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def shot(scenario: dict, args: list[str]) -> tuple[dict, list[dict]]:
    desktop = scenario["desktop"]
    window = flag(args, "--window")
    scale = float(scenario.get("scale", 1.0))
    if window:
        w = next(c for c in desktop["windows"] if c["address"] == window)
        geometry = [*w["at"], *w["size"]]
    else:
        m = next(m for m in desktop["monitors"] if m.get("focused"))
        geometry = list(m["geometry"])
    factor = float(flag(args, "--scale") or 1.0)
    scale *= factor
    image = [round(geometry[2] * scale), round(geometry[3] * scale)]
    out = RUN / "hypruse"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"shot-{time.time_ns()}.png"
    path.write_bytes(png(*image))
    meta = {"geometry": geometry, "scale": scale, "image": image, "format": "png", "path": str(path)}
    if window:
        meta.update(scenario.get("window_meta", {}))
    return meta, [text(f"screenshot saved, read this file to view the screen: {path}"),
                  text(json.dumps(meta))]


def marks(scenario: dict, args: list[str]) -> int:
    window = flag(args, "--window")
    how = scenario.get("marks", "ok")
    if how == "no_tree":
        cls = next(c for c in scenario["desktop"]["windows"] if c["address"] == window)["class"]
        return emit("marks", f"{cls} exposes no accessibility tree; use screenshot + zoom instead", 4)
    legend = scenario.get("legend", [])
    state = {"v": 1, "instance": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""),
             "marks": {"window": window, "ts": time.time(), "class": "x", "items": {}}}
    (RUN / "hypruse").mkdir(parents=True, exist_ok=True)
    (RUN / "hypruse" / "cli-state.json").write_text(json.dumps(state))
    legend_block = text(json.dumps({"legend": legend, "hint": "click_ui(mark=N) clicks a numbered mark"}))
    if how == "no_magick":
        return emit("marks", [text("ImageMagick not found, returning the legend only"), legend_block])
    meta, blocks = shot(scenario, args)
    meta["target"] = "marks"
    blocks[1] = text(json.dumps(meta))
    return emit("marks", [*blocks, legend_block])


def main(argv: list[str]) -> int:
    scenario_path = BASE / "scenario.json"
    scenario = json.loads(scenario_path.read_text())
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    passed = sorted(k for k in os.environ if not k.startswith(("XDG_", "LC_")))
    with open(BASE / "calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "stdin": stdin, "env": passed}) + "\n")
    if argv == ["--version"]:
        print(f"hypruse {scenario.get('version', '0.13.1')}")
        return 0
    args = [a for a in argv if a != "--json"]
    for i, rule in enumerate(scenario.get("rules", [])):
        if args[: len(rule["argv"])] == rule["argv"]:
            if rule.get("once"):
                scenario["rules"].pop(i)
                scenario_path.write_text(json.dumps(scenario))
            code = int(rule.get("exit", 0))
            if code in KINDS:
                return fail(code, rule.get("message", "nope"))
            return emit(args[0], rule.get("result", "ok"), code)
    verb = args[0]
    if verb == "desktop":
        return emit("desktop", scenario["desktop"])
    if verb == "screenshot":
        return emit("screenshot", shot(scenario, args)[1])
    if verb == "marks":
        return marks(scenario, args)
    if verb == "doctor":
        print("[ok]   dependencies grim, wtype found\n\nAll checks passed. hypruse is ready.")
        return 0
    return fail(2, f"the fake has no answer for {args}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
