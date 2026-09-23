"""The tools as shell verbs, for agents that run commands instead of MCP.

One vocabulary: the verbs are the MCP tool names, a tool's `action` is a
positional sub-verb, and the calls go through the same module-level
functions the MCP server registers, so every trust guard, the journal and
the activity beacon apply unchanged (`hypruse replay` already drives the
tools this way). What this module adds is the shell contract:

    stdout   the result, compact plain text by default, `--json` for the
             raw result as one line; a capture prints its file path and its
             coordinate metadata, never bytes
    stderr   one line on failure: `hypruse: error|refused|usage: <message>`
    exit     0 delivered, 1 error, 2 usage, 3 refused by a trust layer or a
             mode gate, 4 ran but produced no result (a timeout, no
             accessibility tree, an ambiguous name, a sequence that stopped)

and the state a fresh process would otherwise lose (see cli_state).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from hypruse import __version__

OBSERVE = ("desktop", "screenshot", "zoom", "ui", "marks", "binds", "wait_for")
ACT = ("pointer", "keyboard", "click_ui", "hypr", "launch", "use_bind", "sequence")
OWNER = ("doctor", "init", "stop", "journal", "replay", "skill", "browser", "browser-host")
ALIASES = {"click-ui": "click_ui", "use-bind": "use_bind", "wait-for": "wait_for"}
THEN = ("none", "desktop", "ui", "screenshot")
HYPR_ACTIONS = (
    "workspace", "focus_window", "move_window", "close_window", "fullscreen", "toggle_floating",
)
WAIT_EVENTS = (
    "window_open", "window_close", "workspace", "title_change",
    "layer_open", "layer_close", "urgent", "screencast",
)

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_REFUSED, EXIT_NORESULT = 0, 1, 2, 3, 4

_S = argparse.SUPPRESS  # absent flags stay absent, so the tool sees its own defaults


class Usage(Exception):
    """A call that cannot be turned into a tool call. Exit 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # one line on stderr; --help is the next step
        sys.stderr.write(f"hypruse: usage: {message}\n")
        sys.exit(EXIT_USAGE)


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="hypruse", add_help=False)
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True

    def json_flag(v: argparse.ArgumentParser) -> None:
        v.add_argument("--json", action="store_true", default=_S,
                       help="print the raw result as one JSON line")

    def verb(name: str, help_: str, aliases: tuple[str, ...] = ()) -> argparse.ArgumentParser:
        v = sub.add_parser(name, aliases=list(aliases), help=help_, description=help_)
        json_flag(v)
        return v

    def acting(v: argparse.ArgumentParser, auth: bool = False) -> None:
        # argparse hands everything after an action word to that action's
        # parser, so the flags an agent writes at the end must live there too
        if not any(a.dest == "json" for a in v._actions):
            json_flag(v)
        v.add_argument("--then", choices=THEN, default=_S,
                       help="append a fresh view of the effect to this call")
        v.add_argument("--dry-run", dest="dry_run", action="store_true", default=_S,
                       help="run every check, deliver nothing, report the plan")
        if auth:
            v.add_argument("--allow-auth", dest="allow_auth", action="store_true", default=_S,
                           help="override the refusal to drive an authentication prompt")

    def capture(v: argparse.ArgumentParser) -> None:
        v.add_argument("--stable", action="store_true", default=_S,
                       help="wait for the frame to settle first")
        v.add_argument("--lossless", action="store_true", default=_S, help="PNG instead of JPEG")
        v.add_argument("--out", default=_S, metavar="PATH", help="move the capture to PATH")

    verb("desktop", "monitors, workspaces, windows, active window, cursor, layers")

    v = verb("screenshot", "capture the focused monitor, a window, or a region")
    v.add_argument("--window", default=_S, metavar="ADDR", help="window address, or 'active'")
    v.add_argument("--region", default=_S, metavar="x,y,WxH")
    v.add_argument("--scale", type=float, default=_S, metavar="F", help="deliberate downscale")
    capture(v)

    v = verb("zoom", "native-resolution recapture around a point, the precision step")
    v.add_argument("x", type=float)
    v.add_argument("y", type=float)
    v.add_argument("--size", default=_S, metavar="WxH")
    v.add_argument("--window", default=_S, metavar="ADDR", help="clamp to this window")
    capture(v)

    v = verb("ui", "a window's controls by name with exact click points and values")
    v.add_argument("--window", default=_S, metavar="ADDR")
    v.add_argument("--name", default=_S, metavar="TEXT", help="filter by accessible name")
    v.add_argument("--all", action="store_true", default=_S,
                   help="include controls that are not actionable (text fields, labels)")

    v = verb("marks", "the window with its controls numbered, plus the legend")
    v.add_argument("--window", default=_S, metavar="ADDR")
    v.add_argument("--name", default=_S, metavar="TEXT")
    v.add_argument("--out", default=_S, metavar="PATH", help="move the capture to PATH")

    verb("binds", "the owner's keybinds, decoded")

    v = verb("wait_for", "block on a compositor event instead of sleeping", ("wait-for",))
    v.add_argument("event", choices=WAIT_EVENTS)
    v.add_argument("--match", default=_S, metavar="TEXT", help="substring filter")
    v.add_argument("--timeout", type=float, default=_S, metavar="S", help="1-60, default 10")

    v = verb("pointer", "move, click, drag or scroll in global coordinates")
    ps = v.add_subparsers(dest="action", metavar="ACTION")
    ps.required = True
    a = ps.add_parser("move", help="move the cursor to X Y")
    a.add_argument("x", type=float)
    a.add_argument("y", type=float)
    a = ps.add_parser("click", help="click at X Y, or at the cursor")
    a.add_argument("coords", nargs="*", type=float, metavar="X Y")
    a.add_argument("--button", choices=("left", "right", "middle"), default=_S)
    a.add_argument("--double", action="store_true", default=_S)
    a = ps.add_parser("drag", help="drag from X Y to TO_X TO_Y")
    a.add_argument("x", type=float)
    a.add_argument("y", type=float)
    a.add_argument("to_x", type=float, metavar="TO_X")
    a.add_argument("to_y", type=float, metavar="TO_Y")
    a.add_argument("--button", choices=("left", "right", "middle"), default=_S)
    a = ps.add_parser("scroll", help="scroll DY notches (positive: content down), DX sideways")
    a.add_argument("dy", type=float, metavar="DY")
    a.add_argument("dx", type=float, nargs="?", default=_S, metavar="DX")
    a.add_argument("--at", nargs=2, type=float, default=_S, metavar=("X", "Y"),
                   help="move there first")
    for a in ps.choices.values():
        acting(a, auth=True)

    v = verb("keyboard", "type text or press an app-level combo")
    ks = v.add_subparsers(dest="action", metavar="ACTION")
    ks.required = True
    a = ks.add_parser("type", help="type TEXT (unicode-safe); '-' reads stdin")
    a.add_argument("text", metavar="TEXT")
    a = ks.add_parser("key", help="press COMBO: ctrl+shift+t, esc, F5, enter")
    a.add_argument("keys", metavar="COMBO")
    for a in ks.choices.values():
        a.add_argument("--window", default=_S, metavar="ADDR", help="focus this window first")
        acting(a, auth=True)

    v = verb("click_ui", "click a control by accessible name, or by marks number", ("click-ui",))
    v.add_argument("name", nargs="?", default=_S, metavar="NAME")
    v.add_argument("--mark", type=int, default=_S, metavar="N", help="from the last marks call")
    v.add_argument("--window", default=_S, metavar="ADDR")
    v.add_argument("--index", type=int, default=_S, metavar="I",
                   help="pick among ambiguous candidates (0-based)")
    v.add_argument("--button", choices=("left", "right", "middle"), default=_S)
    v.add_argument("--double", action="store_true", default=_S)
    acting(v, auth=True)

    v = verb("hypr", "workspace and window operations over IPC")
    hs = v.add_subparsers(dest="action", metavar="ACTION")
    hs.required = True
    a = hs.add_parser("workspace", help="switch to WS (number, name, special:name)")
    a.add_argument("workspace", metavar="WS")
    a = hs.add_parser("focus_window", help="focus the window at ADDR")
    a.add_argument("target", metavar="ADDR")
    a = hs.add_parser("move_window", help="move ADDR to WS, silently")
    a.add_argument("target", metavar="ADDR")
    a.add_argument("workspace", metavar="WS")
    a = hs.add_parser("close_window", help="close ADDR and wait for the destroy")
    a.add_argument("target", metavar="ADDR")
    a = hs.add_parser("fullscreen", help="toggle fullscreen on ADDR (default: active)")
    a.add_argument("target", nargs="?", default=_S, metavar="ADDR")
    a = hs.add_parser("toggle_floating", help="toggle floating on ADDR (default: active)")
    a.add_argument("target", nargs="?", default=_S, metavar="ADDR")
    for a in hs.choices.values():
        acting(a)

    v = verb("launch", "start an app and return its window; put flags before it, or after --")
    v.add_argument("command", nargs="+", metavar="COMMAND")
    v.add_argument("--workspace", default=_S, metavar="WS", help="open it there, silently")
    v.add_argument("--wait", type=float, default=_S, metavar="S", help="1-30, default 8")
    v.add_argument("--dry-run", dest="dry_run", action="store_true", default=_S)

    v = verb("use_bind", "run one of the owner's keybinds by combo", ("use-bind",))
    v.add_argument("combo", metavar="COMBO")
    acting(v)

    v = verb("sequence", "run steps in one call; stops when the desktop changes unexpectedly")
    v.add_argument("steps", metavar="STEPS",
                   help="a JSON array of {op, ...} steps, @file.json, or - for stdin")
    v.add_argument("--no-stop-on-change", dest="no_stop", action="store_true", default=_S)
    acting(v)

    v = verb("clipboard", "read or write the text clipboard (needs HYPRUSE_CLIPBOARD=1)")
    cs = v.add_subparsers(dest="action", metavar="ACTION")
    cs.required = True
    json_flag(cs.add_parser("read", help="print the clipboard text"))
    a = cs.add_parser("write", help="replace it with TEXT; '-' reads stdin")
    a.add_argument("text", metavar="TEXT")
    a.add_argument("--dry-run", dest="dry_run", action="store_true", default=_S)
    json_flag(a)

    # the owner subcommands that take no flags of their own; journal, replay
    # and skill own their argv and are routed before this parser sees them
    sub.add_parser("doctor", help="diagnose the environment")
    v = sub.add_parser("init", help="register hypruse with detected MCP clients")
    v.add_argument("--yes", action="store_true")
    v.add_argument("--skill", action="store_true", help="also install the agent skill")
    sub.add_parser("stop", help="emergency stop of a running server")
    sub.add_parser("serve", help="run the MCP stdio server (same as no arguments)")
    return p


# --- normalization --------------------------------------------------------------


def _stdin_text(value: str) -> str:
    if value != "-":
        return value
    text = sys.stdin.read()
    return text[:-1] if text.endswith("\n") else text


def _steps(arg: str) -> list[dict[str, Any]]:
    if arg == "-":
        raw = sys.stdin.read()
    elif arg.startswith("@"):
        try:
            raw = Path(arg[1:]).expanduser().read_text()
        except OSError as exc:
            raise Usage(f"cannot read steps file: {exc}") from exc
    else:
        raw = arg
    try:
        steps = json.loads(raw)
    except ValueError as exc:
        raise Usage(f"STEPS is not valid JSON ({exc})") from exc
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise Usage("STEPS must be a JSON array of {\"op\": ..., ...} objects")
    if not steps:
        raise Usage("STEPS must contain at least one step")
    return steps


def normalize(verb: str, ns: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """The tool's keyword arguments, built only from what the caller passed
    (so the journal records the call as asked), plus the shell-side options
    that are not tool arguments."""
    d = {k: v for k, v in vars(ns).items() if k != "verb"}
    opts = {
        "json_out": bool(d.pop("json", False)),
        "out_path": d.pop("out", None),
        "dry_run": bool(d.pop("dry_run", False)),
    }
    if verb == "ui":
        if d.pop("all", False):
            d["actionable"] = False
    elif verb == "wait_for":
        if "timeout" in d:
            d["timeout_s"] = d.pop("timeout")
    elif verb == "pointer":
        if d["action"] == "click":
            coords = d.pop("coords")
            if len(coords) == 2:
                d["x"], d["y"] = coords
            elif coords:
                raise Usage("click takes X Y, or nothing to click at the cursor")
        elif d["action"] == "scroll":
            d["scroll_dy"] = d.pop("dy")
            if "dx" in d:
                d["scroll_dx"] = d.pop("dx")
            if "at" in d:
                d["x"], d["y"] = d.pop("at")
    elif verb == "keyboard":
        if "text" in d:
            d["text"] = _stdin_text(d["text"])
    elif verb == "click_ui":
        if ("name" in d) == ("mark" in d):
            raise Usage("pass NAME or --mark N, not both, not neither")
    elif verb == "launch":
        words = d.pop("command")
        d["command"] = words[0] if len(words) == 1 else shlex.join(words)
        if "wait" in d:
            d["wait_s"] = d.pop("wait")
    elif verb == "sequence":
        d["steps"] = _steps(d["steps"])
        if d.pop("no_stop", False):
            d["stop_on_change"] = False
    elif verb == "clipboard" and "text" in d:
        d["text"] = _stdin_text(d["text"])
    return d, opts


# --- running --------------------------------------------------------------------


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _fail(kind: str, message: str, code: int) -> int:
    text = _CONTROL.sub(".", str(message)).strip()
    print(f"hypruse: {kind}: {text}", file=sys.stderr)
    return code


def lock_path() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "hypruse"
    base.mkdir(parents=True, exist_ok=True)
    return base / "cli.lock"


def _lock() -> Any:
    """Acting verbs are serialized across processes: two parallel calls could
    otherwise interleave a drag's press and release on the one virtual
    pointer, which the server avoids with an in-process lock. The holder's
    pid goes into the file once the lock is held (never before: opening
    for truncation would wipe the pid of the verb we are waiting on), which
    is how `hypruse stop` reaches a verb when a server owns the beacon."""
    fh = open(lock_path(), "a+")  # noqa: SIM115 (held until the process exits)
    fcntl.flock(fh, fcntl.LOCK_EX)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run(tool: str, kwargs: dict[str, Any], *, json_out: bool = False,
        out_path: str | None = None, dry_run: bool = False) -> int:
    """Call one tool the way the MCP server would, then speak the shell contract."""
    if _flag("HYPRUSE_READONLY") and (tool in ACT or tool == "clipboard"):
        return _fail(
            "refused",
            f"HYPRUSE_READONLY is set: {tool} is not available in read-only mode "
            f"(observation verbs: {', '.join(OBSERVE)})",
            EXIT_REFUSED,
        )
    if tool == "clipboard" and not _flag("HYPRUSE_CLIPBOARD"):
        return _fail(
            "refused",
            "the clipboard is opt-in: set HYPRUSE_CLIPBOARD=1 (clipboards hold passwords)",
            EXIT_REFUSED,
        )
    # a shell caller can only read a file, so the transport is never inline
    # base64, whatever the environment says (that setting is for MCP hosts)
    os.environ["HYPRUSE_SCREENSHOT_MODE"] = "file"
    if dry_run:
        os.environ["HYPRUSE_DRYRUN"] = "1"

    from hypruse import cli, cli_state, journal, safety, server, session, trust
    from hypruse import input as hinput

    session.ensure_session_env()
    server.use_plain_blocks()  # a shell prints text and paths; no MCP types, no MCP import
    acting = tool in ACT or (tool == "clipboard" and kwargs.get("action") == "write")
    lock = None
    if acting:
        try:
            lock = _lock()
            if not cli._take_beacon():  # a live server keeps its beacon; we still arm cleanup
                safety.arm()
        except OSError as exc:
            return _fail("error", f"cannot prepare the runtime directory: {exc}", EXIT_ERROR)
        safety.on_shutdown(hinput.release_held)
    journal.set_source("cli")
    journal.start(__version__, source="cli")
    cli_state.restore()
    if tool == "launch" and trust.marking_on() and not cli_state.flag("marking_installed"):
        trust.init_marking()  # once per compositor instance, not per call
        cli_state.set_flag("marking_installed", True)
    try:
        result = getattr(server, tool)(**kwargs)
    except trust.TrustError as exc:
        return _fail("refused", str(exc), EXIT_REFUSED)
    except (ValueError, TypeError) as exc:
        return _fail("error", str(exc), EXIT_ERROR)
    except Exception as exc:  # IPC, capture, a11y, wire: one line, not a traceback
        return _fail("error", f"{type(exc).__name__}: {exc}", EXIT_ERROR)
    finally:
        cli_state.save()
        if lock is not None:
            lock.close()
    return emit(tool, result, json_out=json_out, out_path=out_path)


# --- output -----------------------------------------------------------------------

# window titles and accessible names are written by whatever is on screen and
# land on a terminal an agent's harness parses: same whitelist as the journal
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _safe(value: Any) -> str:
    return _CONTROL.sub(".", str(value))


def _q(value: Any) -> str:
    """Quoted, control characters stripped, unicode kept (a title is read
    by an agent, not stored in a protocol)."""
    return json.dumps(_safe(value), ensure_ascii=False)


def _is_block(x: Any) -> bool:
    return hasattr(x, "type") and (hasattr(x, "text") or hasattr(x, "data"))


def _plain(result: Any) -> Any:
    """The result as JSON-able data: content blocks become small objects."""
    if isinstance(result, list) and result and _is_block(result[0]):
        out = []
        for b in result:
            if getattr(b, "type", "") == "text":
                out.append({"type": "text", "text": b.text})
            else:
                out.append({"type": getattr(b, "type", "?"),
                            "mimeType": getattr(b, "mimeType", "")})
        return out
    return result


def _move_capture(result: Any, out_path: str) -> Any:
    """Honor --out: the capture block's file moves and its metadata follows."""
    if not (isinstance(result, list) and result and _is_block(result[0])):
        return result
    for block in result:
        text = getattr(block, "text", None)
        if not text:
            continue
        with contextlib.suppress(ValueError):
            meta = json.loads(text)
            if isinstance(meta, dict) and "geometry" in meta and "path" in meta:
                dest = Path(out_path).expanduser()
                if dest.is_dir():
                    dest = dest / Path(meta["path"]).name
                shutil.move(meta["path"], dest)
                meta["path"] = str(dest)
                block.text = json.dumps(meta)
                break
    return result


def exit_for(tool: str, result: Any) -> int:
    """Exit 4 is 'it ran and there is nothing to act on'. The tools say so
    in prose (a timeout note, an ambiguity, a window that exposes no tree),
    which a model reads fine; a shell wants the code."""
    if isinstance(result, str):
        if result.startswith("DRY RUN"):
            return EXIT_OK
        if tool == "wait_for":
            if result.startswith("timeout:"):
                return EXIT_NORESULT
            return EXIT_ERROR if result.startswith("event socket unavailable") else EXIT_OK
        if tool == "launch":
            return EXIT_NORESULT if result.startswith("launched, but no new window") else EXIT_OK
        if tool in ("ui", "marks"):
            return EXIT_NORESULT  # no tree, no matching elements, or the read failed
        if tool == "click_ui":  # a plain string is either the click, or why there was none
            return EXIT_OK if result.startswith("clicked ") else EXIT_NORESULT
        if tool == "sequence" and ": stopped after" in result.split("\n", 1)[0]:
            return EXIT_NORESULT
        return EXIT_OK
    if isinstance(result, list) and result and _is_block(result[0]):
        head = getattr(result[0], "text", "") or ""
        if tool == "click_ui" and " is ambiguous (" in head:
            return EXIT_NORESULT
        if tool == "sequence" and ": stopped after" in head.split("\n", 1)[0]:
            return EXIT_NORESULT
    return EXIT_OK


def emit(tool: str, result: Any, *, json_out: bool = False, out_path: str | None = None) -> int:
    code = exit_for(tool, result)
    if out_path:
        try:
            result = _move_capture(result, out_path)
        except OSError as exc:
            # the capture itself succeeded and still sits in the runtime dir
            print(render(tool, result))
            return _fail("error", f"cannot move the capture to {out_path}: {exc}", EXIT_ERROR)
    if json_out:
        print(json.dumps({"ok": code == EXIT_OK, "tool": tool, "result": _plain(result)},
                         separators=(",", ":")))
    else:
        text = render(tool, result)
        if text:
            print(text)
    return code


def render(tool: str, result: Any) -> str:
    if isinstance(result, str):
        return _safe(result)
    if isinstance(result, dict):
        return _render_dict(tool, result)
    if isinstance(result, list):
        if result and _is_block(result[0]):
            lines = []
            for block in result:
                text = getattr(block, "text", None)
                if text is None:
                    lines.append(f"[{getattr(block, 'type', '?')} block omitted]")
                else:
                    lines.append(_render_text_block(text))
            return "\n".join(line for line in lines if line)
        if all(isinstance(x, dict) for x in result):
            return _render_items(tool, result)
    return json.dumps(result, separators=(",", ":"))


def _render_text_block(text: str) -> str:
    if text.startswith("screenshot saved, read this file"):
        return ""  # the metadata block carries the path
    try:
        data = json.loads(text)
    except ValueError:
        return _safe(text)
    if isinstance(data, dict):
        if "monitors" in data:
            return _render_desktop(data)
        if "geometry" in data and "path" in data:
            return data["path"] + "\n" + json.dumps(data, separators=(",", ":"))
        if isinstance(data.get("legend"), list):
            return _render_ui(data["legend"], key="mark")
        return json.dumps(data, separators=(",", ":"))
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return _render_items("", data)
    return _safe(text)


def _render_dict(tool: str, d: dict[str, Any]) -> str:
    if "monitors" in d:
        return _render_desktop(d)
    if tool == "wait_for" and "event" in d:
        # `openwindow address=0x.. class=firefox title="Inbox"`, with an
        # `already: ` prefix when the condition held before the wait began
        rest = dict(d)
        head = _safe(rest.pop("event"))
        already = rest.pop("already", False)
        pairs = " ".join(f"{k}={_scalar(v)}" for k, v in rest.items())
        return ("already: " if already else "") + f"{head} {pairs}".rstrip()
    if tool == "launch" and "address" in d:
        line = (f"{d['address']} {_safe(d.get('class', ''))} {_q(d.get('title', ''))} "
                f"workspace {d.get('workspace')}")
        if d.get("note"):
            line += f"\nnote {_safe(d['note'])}"
        return line
    return " ".join(f"{k}={_scalar(v)}" for k, v in d.items())


def _scalar(v: Any) -> str:
    if isinstance(v, str):
        return _q(v) if (" " in v or not v or '"' in v) else _safe(v)
    return json.dumps(v, separators=(",", ":"))


def _render_items(tool: str, items: list[dict[str, Any]]) -> str:
    if items and "role" in items[0]:
        return _render_ui(items)
    if tool == "binds" or (items and "combo" in items[0]):
        lines = []
        for b in items:
            parts = [_safe(b.get("combo", ""))]
            action = f"{_safe(b.get('action', ''))} {_safe(b.get('arg', ''))}".strip()
            parts.append(action)
            if b.get("description"):
                parts.append(_q(b["description"]))
            if b.get("submap"):
                parts.append(f"submap {_safe(b['submap'])}")
            lines.append("  ".join(parts))
        return "\n".join(lines)
    return "\n".join(json.dumps(x, separators=(",", ":")) for x in items)


def _render_ui(items: list[dict[str, Any]], key: str = "") -> str:
    lines = []
    for i, e in enumerate(items):
        n = e.get(key, i) if key else i
        role, name = _safe(e.get("role", "")), _q(e.get("name", ""))
        line = f"[{n}] {role} {name} @{e.get('x')},{e.get('y')}"
        if "value" in e:
            line += f" value={_q(e['value'])}"
        if "percent" in e:
            line += f" percent={e['percent']}"
        if "checked" in e:
            line += f" checked={json.dumps(e['checked'])}"
        if e.get("clickable") is False:
            line += " not-clickable"
        lines.append(line)
    return "\n".join(lines)


def _render_desktop(snap: dict[str, Any]) -> str:
    lines = []
    for m in snap.get("monitors", []):
        g = m.get("geometry") or [None] * 4
        line = (f"monitor {_safe(m.get('name'))} at {g[0]},{g[1]} {g[2]}x{g[3]} "
                f"scale {m.get('scale')} ws {m.get('active_workspace')}")
        if m.get("focused"):
            line += " focused"
        if m.get("transform"):
            line += f" transform {m['transform']}"
        lines.append(line)
    for w in snap.get("workspaces", []):
        line = (f"ws {w.get('id')} {_q(w.get('name', ''))} on {_safe(w.get('monitor'))} "
                f"windows {w.get('windows')}")
        if w.get("visible"):
            line += " visible"
        lines.append(line)
    for c in snap.get("windows", []):
        at, size = c.get("at") or [None, None], c.get("size") or [None, None]
        line = (f"win {c.get('address')} ws {c.get('workspace')} {_safe(c.get('class', ''))} "
                f"{_q(c.get('title', ''))} at {at[0]},{at[1]} {size[0]}x{size[1]}")
        for flag in ("floating", "fullscreen", "hidden"):
            if c.get(flag):
                line += f" {flag}"
        lines.append(line)
    lines.append(f"active {snap.get('active_window')}")
    cur = snap.get("cursor")
    lines.append(f"cursor {cur[0]},{cur[1]}" if cur else "cursor unknown")
    for s in snap.get("layers", []):
        g = s.get("geometry") or [None] * 4
        lines.append(f"layer {_safe(s.get('level'))} {_safe(s.get('namespace'))} at {g[0]},{g[1]} "
                     f"{g[2]}x{g[3]} kind {_safe(s.get('kind'))} on {_safe(s.get('monitor'))}")
    return "\n".join(lines)


# --- entry ----------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("browser", "browser-host"):
        # the bridge to the hypruse-browser extension; browser-host is what Chrome starts,
        # and Chrome passes it an origin argument no parser here should see
        if argv[0] == "browser-host":
            from hypruse.browser import host

            return host.main(argv[1:])
        from hypruse.browser import install

        return install.main(argv[1:])
    if argv and argv[0] in ("journal", "replay", "skill"):
        # these parse their own flags (argparse's REMAINDER would reject a
        # leading --flag before ever handing it over)
        from hypruse import cli  # looked up at call time: tests swap these in

        if argv[0] == "journal":
            return cli.journal_cmd(argv[1:])
        if argv[0] == "replay":
            return cli.replay(argv[1:])
        from hypruse import skill

        return skill.main(argv[1:])
    parser = build_parser()
    ns = parser.parse_args(argv)
    verb = ALIASES.get(ns.verb, ns.verb)
    if verb == "serve":
        from hypruse.server import main as server_main

        server_main()
        return EXIT_OK
    if verb in OWNER:
        from hypruse import cli

        if verb == "doctor":
            return cli.doctor()
        if verb == "init":
            return cli.init(assume_yes=ns.yes, install_skill=ns.skill)
        return cli.stop()
    try:
        kwargs, opts = normalize(verb, ns)
    except Usage as exc:
        return _fail("usage", str(exc), EXIT_USAGE)
    return run(verb, kwargs, **opts)
