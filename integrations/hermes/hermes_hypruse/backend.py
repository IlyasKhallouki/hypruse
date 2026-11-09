"""Hermes' ``computer_use`` backend on top of the hypruse CLI.

One coordinate space for the model: the pixels of the last capture, origin
top-left, which is what the Hermes schema promises. hypruse works in
Hyprland global logical pixels, and every capture's metadata gives the
map: ``global = geometry[:2] + pixel / scale``. Captures of a window are
anchored to the window, so a window that moved since the capture still
gets the click where the model saw it, and one that was resized is
refused.

Delivery: Hermes' default (background) maps to ``--background``, so hypruse
either does the action without the human's cursor, focus or workspace or
refuses and says what works instead. ``delivery_mode='foreground'`` (its
own approval in Hermes) maps to ``--foreground``. The pointer never runs in
the background: a coordinate action needs foreground and the target window
must be the one the human is in.

Refusals (hypruse exit 3) come back as ``ok=False`` with hypruse's own
message. Nothing here retries, re-routes or downgrades a refused call.
"""

from __future__ import annotations

import base64
import contextlib
import os
import sys
from typing import Any

from tools.computer_use.backend import ActionResult, CaptureResult, ComputerUseBackend, UIElement

from .hypruse_cli import (
    EXIT_NORESULT,
    EXIT_REFUSED,
    EXIT_USAGE,
    MIN_VERSION,
    HypruseCLI,
    Outcome,
    capture_parts,
    command_found,
    marks_record,
    resolve_command,
    runtime_dir,
)

_MODES = ("som", "vision", "ax")
_FULL_SCREEN = frozenset({"screen", "fullscreen", "full screen", "all", "desktop"})
_ACTIVE = frozenset({"active", "frontmost", "focused"})
_DEFAULT_MAX_DIM = 1456  # Hermes' computer_use.max_image_dimension default

_NO_TARGET = "No target window: call capture(app=...) or focus_app first."
_NO_ELEMENTS = ("element {n} is not in the last capture; capture with mode='som' or 'ax' "
                "(element numbers come from that capture)")
_FULL_SCREEN_NOTE = ("full-screen capture of the focused monitor has no elements; capture(app=...) "
                     "a window to get its numbered controls")
_HIDDEN_NOTE = ("{addr} is on workspace {ws}, not on the human's screen: press its controls by "
                "element and use type and key, which all run in the background; the pointer "
                "(coordinate clicks, drag, scroll) cannot reach it")
_NOT_DRAWN_NOTE = ("the compositor is not drawing this window right now, so the image may show "
                   "how it last looked")
_NO_MAGICK_NOTE = ("hypruse could not draw the numbers on the image (ImageMagick is not "
                   "installed); the element list is exact, click by element")

_KEY_NAMES = {"option": "alt", "opt": "alt", "command": "cmd", "windows": "super",
              "escape": "esc", "-": "minus", "+": "plus"}
_UNSUPPORTED_KEYS = frozenset({"fn"})


# --- options ------------------------------------------------------------------------


def load_options() -> dict[str, Any]:
    """``computer_use.hypruse`` from the active profile's config.yaml, plus Hermes'
    shared ``computer_use.max_image_dimension``. Unreadable config means defaults."""
    cu: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        from hermes_cli.config import load_config_readonly

        cu = (load_config_readonly() or {}).get("computer_use") or {}
    own = cu.get("hypruse") if isinstance(cu.get("hypruse"), dict) else {}
    delivery = str(own.get("delivery") or "background").strip().lower()
    try:
        timeout = max(5.0, float(own.get("timeout") or 60))
    except (TypeError, ValueError):
        timeout = 60.0
    try:
        max_dim = int(cu.get("max_image_dimension", _DEFAULT_MAX_DIM))
    except (TypeError, ValueError):
        max_dim = _DEFAULT_MAX_DIM
    return {
        "command": own.get("command"),
        "delivery": delivery if delivery in ("background", "auto") else "background",
        "timeout": timeout,
        "max_image_dimension": max_dim if max_dim > 0 else 0,
    }


def session_reachable() -> bool:
    """A Hyprland session this process can reach, without asking it anything."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return True
    hypr = runtime_dir() / "hypr"
    with contextlib.suppress(OSError):
        return any((d / ".socket.sock").exists() for d in hypr.iterdir())
    return False


# --- small helpers -----------------------------------------------------------------


def _window_id(address: str) -> int | None:
    with contextlib.suppress(TypeError, ValueError):
        return int(str(address), 16)
    return None


def _address(window_id: int) -> str:
    return hex(int(window_id))


def _real_id(value: Any) -> int | None:
    """Models zero-fill optional integers; 0 or a negative is no target at all."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _why(out: Outcome) -> str:
    if out.code == EXIT_REFUSED:
        return f"hypruse refused: {out.message}"
    if out.code == EXIT_NORESULT:
        return out.text()
    return f"hypruse {out.kind or 'error'}: {out.message or out.text()}"


def _ws_refs(ws: dict[str, Any]) -> set[Any]:
    return {ws.get("id"), ws.get("name"), str(ws.get("id")), str(ws.get("name"))} - {None, "None"}


def _on_screen(snap: dict[str, Any], window: dict[str, Any]) -> bool:
    if window.get("hidden"):
        return False
    shown: set[Any] = set()
    for ws in snap.get("workspaces") or []:
        if isinstance(ws, dict) and ws.get("visible"):
            shown |= _ws_refs(ws)
    return window.get("workspace") in shown or str(window.get("workspace")) in shown


def _monitor_scale(snap: dict[str, Any], window: dict[str, Any] | None) -> float:
    monitors = [m for m in snap.get("monitors") or [] if isinstance(m, dict)]
    name = None
    if window is not None:
        for ws in snap.get("workspaces") or []:
            if isinstance(ws, dict) and (window.get("workspace") in _ws_refs(ws)):
                name = ws.get("monitor")
                break
    pick = next((m for m in monitors if name and m.get("name") == name), None)
    pick = pick or next((m for m in monitors if m.get("focused")), None)
    try:
        return float((pick or {}).get("scale") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _label(item: dict[str, Any]) -> str:
    label = str(item.get("name") or "")
    if "value" in item and item["value"] not in (None, ""):
        label += f" = {str(item['value'])!r}"
    if "checked" in item:
        label += " [checked]" if item["checked"] else " [unchecked]"
    if "percent" in item:
        label += f" {item['percent']}%"
    return label.strip()


def _normalize_combo(keys: str) -> tuple[str | None, str]:
    """Hermes key combos ('cmd+s', 'ctrl-alt-t', 'return', 'option+left') in
    hypruse's spelling ('+'-joined, alt for option). (None, why) when hypruse
    has no such key."""
    raw = (keys or "").strip()
    if not raw:
        return None, "key needs keys, e.g. 'ctrl+s' or 'enter'"
    if raw in ("-", "+"):
        return _KEY_NAMES[raw], ""
    if "+" in raw:
        parts = raw.split("+")
        if raw.endswith("+"):  # 'ctrl++' is ctrl and the plus key
            parts = [*[p for p in parts if p], "+"]
    elif "-" in raw.strip("-"):
        parts = raw.split("-")
        if raw.endswith("-"):
            parts = [*[p for p in parts if p], "-"]
    else:
        parts = [raw]
    out = []
    for part in (p.strip() for p in parts if p.strip()):
        lower = part.lower()
        if lower in _UNSUPPORTED_KEYS:
            return None, f"hypruse has no {part!r} key (it never reaches applications)"
        out.append(_KEY_NAMES.get(lower, part))
    return "+".join(out), ""


# what a hypruse result line says about its effect
_CONFIRMED = ("; now: ", "the field now holds the typed text")
_NOOP = ("no change seen",)
_UNSURE_CHANGE = ("the field changed, but not by exactly",)


def _effect(text: str) -> tuple[str | None, bool | None]:
    if text.startswith("DRY RUN"):
        return None, None
    if any(m in text for m in _CONFIRMED):
        return "confirmed", True
    if any(m in text for m in _UNSURE_CHANGE):
        return "unverifiable", False
    if any(m in text for m in _NOOP):
        return "suspected_noop", False
    return "unverifiable", None


def _path(text: str) -> str | None:
    if "accessibility action" in text or "accessibility interface" in text:
        return "accessibility"
    if "with sendshortcut" in text:
        return "sendshortcut"
    if text.startswith(("clicked ", "click ok", "drag ok", "scroll ok", "move ok")):
        return "pointer"
    if text.startswith(("typed ", "pressed ")):
        return "keyboard"
    return None


def _refusal(action: str, out: Outcome, delivery_mode: str | None) -> ActionResult:
    text = out.message or out.text()
    if "cannot be done in the background" in text:
        return ActionResult(
            ok=False, action=action, code="background_unavailable", delivery_mode=delivery_mode,
            message=f"hypruse refused this in the background (exit 3): {text}",
            meta={"hypruse_exit": EXIT_REFUSED, "refused": True})
    return ActionResult(
        ok=False, action=action, code="refused", delivery_mode=delivery_mode,
        message=(f"hypruse refused this (exit 3, a trust layer the owner set): {text}. This is "
                 "a policy decision, not a failed delivery: do not retry it, change "
                 "delivery_mode or route around it; tell the user what was refused and why."),
        meta={"hypruse_exit": EXIT_REFUSED, "refused": True})


def _result(action: str, out: Outcome, delivery_mode: str | None) -> ActionResult:
    if out.code == EXIT_REFUSED:
        return _refusal(action, out, delivery_mode)
    if not out.ok:
        code = {EXIT_NORESULT: "no_result", EXIT_USAGE: "bad_request"}.get(out.code)
        if out.kind == "timeout":
            code = "timeout"
        return ActionResult(ok=False, action=action, code=code, delivery_mode=delivery_mode,
                            message=_why(out), meta={"hypruse_exit": out.code})
    text = out.text()
    effect, verified = _effect(text)
    meta: dict[str, Any] = {"hypruse": text}
    if "; WARNING: " in text:
        meta["warning"] = text.split("; WARNING: ", 1)[1]
    if text.startswith("DRY RUN"):
        meta["dry_run"] = True
    return ActionResult(ok=True, action=action, message=text, meta=meta, effect=effect,
                        verified=verified, path=_path(text), delivery_mode=delivery_mode)


def _unsupported(action: str, why: str, delivery_mode: str | None = None) -> ActionResult:
    return ActionResult(ok=False, action=action, code="unsupported_action", message=why,
                        delivery_mode=delivery_mode)


def _refuse(action: str, why: str, code: str | None = None,
            delivery_mode: str | None = None) -> ActionResult:
    return ActionResult(ok=False, action=action, message=why, code=code, delivery_mode=delivery_mode)


# --- the backend ------------------------------------------------------------------------


class HypruseBackend(ComputerUseBackend):
    """Sticky state, as Hermes' tool expects of a backend: the target window
    (``_last_app``, ``_last_target`` with ``pid`` and ``window_id``), the
    frame of the last capture and its numbered elements."""

    def __init__(self, permission_mode: str = "standard", *, options: dict[str, Any] | None = None,
                 cli: HypruseCLI | None = None) -> None:
        if permission_mode not in ("standard", "bounded", "unrestricted"):
            raise ValueError(f"unsupported permission mode: {permission_mode}")
        self.permission_mode = permission_mode
        self._options = options if options is not None else load_options()
        self._cli = cli or HypruseCLI(resolve_command(self._options.get("command")),
                                      timeout=float(self._options.get("timeout", 60.0)))
        self.version: tuple[int, int, int] | None = None
        self._clear_target()

    # lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if not command_found(self._cli.command):
            raise RuntimeError(
                f"hypruse is not installed ({self._cli.command[0]!r} not found). Install hypruse "
                f"{'.'.join(map(str, MIN_VERSION))} or newer (AUR `hypruse`, `uv tool install "
                "hypruse` or `pipx install hypruse`), or point HERMES_HYPRUSE_CMD or "
                "computer_use.hypruse.command at it.")
        version = self._cli.version()
        if version is None:
            raise RuntimeError(f"`{' '.join(self._cli.command)} --version` did not print a hypruse "
                               "version; is that command hypruse?")
        if version < MIN_VERSION:
            raise RuntimeError(
                f"hypruse {'.'.join(map(str, version))} is too old for this provider; it needs "
                f"{'.'.join(map(str, MIN_VERSION))} or newer (background mode). Upgrade it, or point "
                "HERMES_HYPRUSE_CMD or computer_use.hypruse.command at a newer one.")
        self.version = version

    def stop(self) -> None:
        self._clear_target()

    def is_available(self) -> bool:
        return (sys.platform == "linux" and command_found(self._cli.command)
                and session_reachable())

    # state ----------------------------------------------------------------------------

    def _clear_view(self) -> None:
        self._frame: dict[str, Any] | None = None
        self._elements: dict[int, dict[str, Any]] = {}
        self._marks: dict[str, Any] | None = None

    def _clear_target(self) -> None:
        self._target: dict[str, Any] | None = None
        self._last_app: str | None = None
        self._last_target: dict[str, Any] | None = None
        self._clear_view()

    def _set_target(self, window: dict[str, Any]) -> None:
        self._target = dict(window)
        self._last_app = str(window.get("class") or "")
        self._last_target = {"pid": window.get("pid"), "window_id": _window_id(window["address"])}

    def _target_address(self) -> str | None:
        return self._target.get("address") if self._target else None

    # discovery ------------------------------------------------------------------------

    def _desktop(self) -> tuple[dict[str, Any] | None, str]:
        out = self._cli.run("desktop")
        if out.ok and isinstance(out.result, dict):
            return out.result, ""
        return None, _why(out)

    @staticmethod
    def _windows(snap: dict[str, Any]) -> list[dict[str, Any]]:
        return [w for w in snap.get("windows") or [] if isinstance(w, dict) and w.get("address")]

    def _pick(self, snap: dict[str, Any], app: str | None, pid: int | None,
              window_id: int | None) -> tuple[dict[str, Any] | None, str]:
        windows, active = self._windows(snap), snap.get("active_window")

        def prefer(found: list[dict[str, Any]]) -> dict[str, Any]:
            for addr in (self._target_address(), active):
                hit = next((w for w in found if w["address"] == addr), None)
                if hit is not None:
                    return hit
            return found[0]

        if window_id is not None:
            addr = _address(window_id)
            hit = next((w for w in windows if w["address"] == addr), None)
            if hit is None:
                return None, f"no window {addr} (window_id {window_id}); call list_windows"
            if pid is not None and hit.get("pid") not in (None, pid):
                return None, f"window {addr} belongs to pid {hit.get('pid')}, not {pid}"
            return hit, ""
        if pid is not None:
            found = [w for w in windows if w.get("pid") == pid]
            return (prefer(found), "") if found else (None, f"no window of pid {pid}")
        if app and app.strip():
            needle = app.strip().lower()
            if needle in _ACTIVE:
                hit = next((w for w in windows if w["address"] == active), None)
                return (hit, "") if hit else (None, "no window is focused")
            cls = lambda w: str(w.get("class") or "").lower()  # noqa: E731
            title = lambda w: str(w.get("title") or "").lower()  # noqa: E731
            tiers = (
                [w for w in windows if w["address"].lower() == needle],
                [w for w in windows if cls(w) == needle],
                [w for w in windows if title(w) == needle],
                [w for w in windows if needle in cls(w)],
                [w for w in windows if needle in title(w)],
            )
            for found in tiers:
                if found:
                    return prefer(found), ""
            names = ", ".join(sorted({str(w.get("class") or "?") for w in windows})) or "none"
            return None, (f"no window matched app={app!r} (window classes: {names}); "
                          "list_windows shows titles and window ids")
        current = self._target_address()
        hit = next((w for w in windows if w["address"] == current
                    and w.get("class") == (self._target or {}).get("class")), None)
        if hit is not None:
            return hit, ""
        hit = next((w for w in windows if w["address"] == active), None)
        if hit is not None:
            return hit, ""
        return None, "no window is focused; pass app= (a class from list_apps)"

    # capture --------------------------------------------------------------------------

    def _fit_scale(self, physical_long_edge: float) -> float:
        cap = int(self._options.get("max_image_dimension") or 0)
        if cap <= 0 or physical_long_edge <= cap:
            return 1.0
        return max(0.1, round(cap / physical_long_edge, 3))

    def _read_image(self, meta: dict[str, Any]) -> tuple[bytes | None, str]:
        path = meta.get("path")
        if not isinstance(path, str):
            return None, "hypruse returned no capture file"
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            return None, f"could not read the capture {path}: {exc}"
        with contextlib.suppress(OSError):
            os.unlink(path)  # Hermes keeps its own copy; leave no screen contents behind
        return raw, ""

    def _shoot(self, address: str | None, long_edge: float) -> tuple[dict[str, Any] | None,
                                                                       bytes | None, str]:
        args = ["screenshot", *(["--window", address] if address else []), "--stable"]
        scale = self._fit_scale(long_edge)
        if scale < 1.0:
            args += ["--scale", f"{scale:g}"]
        out = self._cli.run(*args)
        if not out.ok:
            return None, None, _why(out)
        meta, _, _ = capture_parts(out.result)
        if meta is None:
            return None, None, "hypruse returned no capture metadata"
        raw, why = self._read_image(meta)
        return (meta, raw, "") if raw is not None else (None, None, why)

    def _set_frame(self, meta: dict[str, Any]) -> None:
        """The map from the capture's pixels back to global logical pixels. The
        window it belongs to, as it was then, is the target record."""
        self._frame = {
            "geometry": [float(v) for v in meta["geometry"]],
            "scale": float(meta.get("scale") or 1.0),
            "image": [int(v) for v in meta.get("image") or (0, 0)],
        }

    def _to_image(self, x: float, y: float) -> tuple[int, int]:
        f = self._frame or {}
        gx, gy = (f.get("geometry") or (0, 0))[:2]
        s = f.get("scale") or 1.0
        return round((x - gx) * s), round((y - gy) * s)

    @staticmethod
    def _failed(mode: str, note: str, app: str = "", title: str = "") -> CaptureResult:
        return CaptureResult(mode=mode, width=0, height=0, app=app, window_title=title, note=note)

    @staticmethod
    def _image_result(mode: str, meta: dict[str, Any], raw: bytes | None, app: str, title: str,
                      elements: list[UIElement], notes: list[str]) -> CaptureResult:
        image = meta.get("image") or (0, 0)
        mime = "image/png" if str(meta.get("format", "")).lower() == "png" else "image/jpeg"
        keep = raw is not None and mode != "ax"
        return CaptureResult(
            mode=mode, width=int(image[0]), height=int(image[1]),
            png_b64=base64.b64encode(raw).decode("ascii") if keep else None,
            elements=elements, app=app, window_title=title,
            png_bytes_len=len(raw) if keep else 0, image_mime_type=mime if keep else None,
            note="; ".join(n for n in notes if n))

    def capture(self, mode: str = "som", app: str | None = None, pid: int | None = None,
                window_id: int | None = None) -> CaptureResult:
        mode = mode if mode in _MODES else "som"
        pid, window_id = _real_id(pid), _real_id(window_id)
        snap, why = self._desktop()
        if snap is None:
            self._clear_view()
            return self._failed(mode, why)
        if app and app.strip().lower() in _FULL_SCREEN and pid is None and window_id is None:
            return self._capture_screen(snap)
        window, why = self._pick(snap, app, pid, window_id)
        if window is None:
            self._clear_target()
            return self._failed(mode, why)
        self._set_target(window)
        self._clear_view()
        notes = []
        if not _on_screen(snap, window):
            notes.append(_HIDDEN_NOTE.format(addr=window["address"], ws=window.get("workspace")))
        if mode == "vision":
            return self._capture_plain(snap, window, "vision", notes)
        return self._capture_marks(snap, window, mode, notes)

    def _capture_plain(self, snap: dict[str, Any], window: dict[str, Any], mode: str,
                       notes: list[str]) -> CaptureResult:
        cls, title = str(window.get("class") or ""), str(window.get("title") or "")
        size = window.get("size") or (0, 0)
        meta, raw, why = self._shoot(window["address"], max(size) * _monitor_scale(snap, window))
        if meta is None:
            return self._failed(mode, why, cls, title)
        self._set_frame(meta)
        if meta.get("visible") is False:
            notes.append(_NOT_DRAWN_NOTE)
        return self._image_result(mode, meta, raw, cls, title, [], notes)

    def _capture_marks(self, snap: dict[str, Any], window: dict[str, Any], mode: str,
                       notes: list[str]) -> CaptureResult:
        addr = window["address"]
        cls, title = str(window.get("class") or ""), str(window.get("title") or "")
        out = self._cli.run("marks", "--window", addr)
        if out.code == EXIT_NORESULT:  # no accessibility tree, or nothing actionable in it
            notes.append(out.text())
            if mode == "ax":
                return self._failed(mode, "; ".join(notes), cls, title)
            notes.append("no elements to click by number: use key and type, or a coordinate "
                         "click with delivery_mode='foreground' on the window the human is in")
            return self._capture_plain(snap, window, mode, notes)
        if not out.ok:
            return self._failed(mode, _why(out), cls, title)
        meta, legend, extra = capture_parts(out.result)
        notes.extend(n for n in extra if not n.startswith("ImageMagick not found"))
        raw = None
        if meta is None:  # the legend alone: ImageMagick is missing
            notes.append(_NO_MAGICK_NOTE)
            if mode == "som":
                size = window.get("size") or (0, 0)
                meta, raw, why = self._shoot(addr, max(size) * _monitor_scale(snap, window))
                if meta is None:
                    notes.append(f"and the plain capture failed: {why}")
        else:
            raw, why = self._read_image(meta)
            if raw is None:
                notes.append(why)
        if meta is not None:
            self._set_frame(meta)
            if meta.get("visible") is False:
                notes.append(_NOT_DRAWN_NOTE)
        record = marks_record()
        self._marks = {"window": addr, "ts": record.get("ts") if record else None,
                       "checked": record is not None}
        elements = []
        for item in legend or []:
            mark = item.get("mark")
            if not isinstance(mark, int) or not isinstance(item.get("x"), (int, float)):
                continue
            self._elements[mark] = dict(item)
            ix, iy = self._to_image(float(item["x"]), float(item["y"])) if self._frame else (0, 0)
            attributes = {k: item[k] for k in ("name", "value", "checked", "percent", "action",
                                               "clickable") if k in item}
            attributes["point"] = [item["x"], item["y"]]  # global logical pixels
            elements.append(UIElement(
                index=mark, role=str(item.get("role") or ""), label=_label(item),
                bounds=(ix, iy, 1, 1), app=cls, pid=int(window.get("pid") or 0),
                window_id=_window_id(addr) or 0, attributes=attributes))
        if meta is None:
            return CaptureResult(mode=mode, width=0, height=0, elements=elements, app=cls,
                                 window_title=title, note="; ".join(notes))
        return self._image_result(mode, meta, raw, cls, title, elements, notes)

    def _capture_screen(self, snap: dict[str, Any]) -> CaptureResult:
        self._clear_target()
        monitor = next((m for m in snap.get("monitors") or [] if m.get("focused")), None) or {}
        geometry = monitor.get("geometry") or (0, 0, 0, 0)
        long_edge = max(geometry[2:4] or (0,)) * float(monitor.get("scale") or 1.0)
        meta, raw, why = self._shoot(None, long_edge)
        if meta is None:
            return self._failed("vision", why, "screen")
        self._set_frame(meta)
        return self._image_result("vision", meta, raw, "screen", "Full screen (focused monitor)",
                                  [], [_FULL_SCREEN_NOTE])

    # listings ------------------------------------------------------------------------

    def list_windows(self) -> list[dict[str, Any]]:
        snap, why = self._desktop()
        if snap is None:
            raise RuntimeError(why)
        active = snap.get("active_window")
        out = []
        for w in self._windows(snap):
            at, size = w.get("at") or (0, 0), w.get("size") or (0, 0)
            out.append({
                "app_name": w.get("class") or "", "title": w.get("title") or "",
                "pid": w.get("pid"), "window_id": _window_id(w["address"]),
                "address": w["address"], "workspace": w.get("workspace"),
                "bounds": [at[0], at[1], size[0], size[1]],
                "on_screen": _on_screen(snap, w), "focused": w["address"] == active,
                **({"floating": True} if w.get("floating") else {}),
            })
        return out

    def list_apps(self) -> list[dict[str, Any]]:
        apps: dict[str, dict[str, Any]] = {}
        for w in self.list_windows():
            name = w["app_name"] or "?"
            entry = apps.setdefault(name, {"name": name, "app_name": name, "pid": w["pid"],
                                           "pids": [], "windows": 0, "running": True})
            entry["windows"] += 1
            if w["pid"] is not None and w["pid"] not in entry["pids"]:
                entry["pids"].append(w["pid"])
        return list(apps.values())

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        snap, why = self._desktop()
        if snap is None:
            return _refuse("focus_app", why)
        window, why = self._pick(snap, app, None, None)
        if window is None:
            self._clear_target()
            return _refuse("focus_app", why)
        self._set_target(window)
        self._clear_view()
        addr, cls = window["address"], window.get("class") or ""
        if not raise_window:
            return ActionResult(ok=True, action="focus_app", effect="confirmed", verified=True,
                                message=(f"targeted {cls} ({addr}, workspace "
                                         f"{window.get('workspace')}) without raising it; capture "
                                         "to see it"))
        result = _result("focus_app", self._cli.run("hypr", "focus_window", addr), "foreground")
        result.meta["target_selected"] = True
        return result

    # input ---------------------------------------------------------------------------------

    def _placement(self, action: str, delivery_mode: str | None) -> tuple[list[str], ActionResult | None]:
        if delivery_mode in (None, ""):
            if self._options.get("delivery") == "auto":
                return [], None
            return ["--background"], None
        if delivery_mode == "background":
            return ["--background"], None
        if delivery_mode == "foreground":
            return ["--foreground"], None
        return [], _refuse(action, f"unknown delivery_mode {delivery_mode!r}: use background or "
                                   "foreground", code="bad_delivery_mode", delivery_mode=delivery_mode)

    def _bring_forward(self, action: str, bring_to_front: bool,
                       delivery_mode: str | None) -> ActionResult | None:
        if not bring_to_front:
            return None
        if delivery_mode != "foreground":
            return _refuse(action, "bring_to_front requires delivery_mode='foreground'",
                           code="bring_to_front_requires_foreground", delivery_mode=delivery_mode)
        addr = self._target_address()
        if not addr:
            return _refuse(action, _NO_TARGET, delivery_mode=delivery_mode)
        out = self._cli.run("hypr", "focus_window", addr)
        return None if out.ok else _result(action, out, delivery_mode)

    def _marks_current(self, action: str, delivery_mode: str | None) -> ActionResult | None:
        """None when hypruse's marks numbering is still the one this backend captured."""
        mine = self._marks or {}
        if not mine.get("checked"):
            return None  # hypruse's state is unreadable; its own checks still apply
        now = marks_record()
        if now is None or now.get("window") != mine.get("window") or now.get("ts") != mine.get("ts"):
            return _refuse(action, (
                "the element numbers are stale: another hypruse marks call replaced this "
                "capture's numbering; capture again before clicking by element"),
                code="stale_element", delivery_mode=delivery_mode)
        return None

    def _current_point(self, x: float, y: float) -> tuple[tuple[float, float] | None, str]:
        """A pixel of the last capture in global logical pixels, anchored to the
        captured window where it is now."""
        f = self._frame
        if f is None:
            return None, "no capture to take coordinates from; capture first"
        width, height = f["image"]
        if not (0 <= x < width and 0 <= y < height):
            return None, (f"coordinate ({x:g}, {y:g}) is outside the last capture "
                          f"({width}x{height} pixels)")
        gx, gy = f["geometry"][:2]
        return (gx + x / f["scale"], gy + y / f["scale"]), ""

    def _pointer(self, action: str, args: list[str], points: list[tuple[float, float]],
                 delivery_mode: str | None, bring_to_front: bool) -> ActionResult:
        """A pointer verb. The pointer is the human's own cursor: background is
        refused by hypruse itself, and foreground runs only on the window the human
        is in, with the points inside it and hypruse's automatic placement rule
        (input landing on another window is refused) left in force."""
        placement, refusal = self._placement(action, delivery_mode)
        if refusal is not None:
            return refusal
        if placement == ["--background"]:  # hypruse refuses it and says what works instead
            out = self._cli.run(*self._pointer_argv(args, points), "--background")
            return _result(action, out, delivery_mode or "background")
        refusal = self._bring_forward(action, bring_to_front, delivery_mode)
        if refusal is not None:
            return refusal
        snap, why = self._desktop()
        if snap is None:
            return _refuse(action, why, delivery_mode=delivery_mode)
        captured = self._target  # the window record as it was when captured
        if captured:
            addr = captured["address"]
            window = next((w for w in self._windows(snap) if w["address"] == addr), None)
            if window is None:
                return _refuse(action, f"the captured window {addr} is gone; capture again",
                               code="stale_target", delivery_mode=delivery_mode)
            if snap.get("active_window") != addr:
                return _refuse(action, (
                    f"the captured window {addr} is not the window the human is in, and the "
                    "pointer would land on whatever their screen shows at that point. Use an "
                    "element click, type or key (background), or bring it forward with "
                    "delivery_mode='foreground' and bring_to_front=true"),
                    code="target_not_frontmost", delivery_mode=delivery_mode)
            if list(window.get("size") or []) != list(captured.get("size") or []):
                return _refuse(action, (
                    f"the window was resized since the capture (from {captured.get('size')} to "
                    f"{window.get('size')}), so its contents moved; capture again"),
                    code="stale_target", delivery_mode=delivery_mode)
            at, was = window.get("at") or (0, 0), captured.get("at") or (0, 0)
            dx, dy = at[0] - was[0], at[1] - was[1]
            size = window.get("size") or (0, 0)
            moved = [(px + dx, py + dy) for px, py in points]
            for px, py in moved:
                if not (at[0] <= px < at[0] + size[0] and at[1] <= py < at[1] + size[1]):
                    return _refuse(action, f"({px:g}, {py:g}) is outside the window {addr}",
                                   delivery_mode=delivery_mode)
            points = moved
        # no placement flag: hypruse's automatic rule still refuses input that would
        # land on a window other than the human's, and HYPRUSE_BACKGROUND=1 refuses it
        out = self._cli.run(*self._pointer_argv(args, points))
        return _result(action, out, delivery_mode)

    @staticmethod
    def _pointer_argv(args: list[str], points: list[tuple[float, float]]) -> list[str]:
        """Fill the {x0} {y0} {x1} {y1} slots of a pointer argv."""
        flat = {}
        for i, (px, py) in enumerate(points):
            flat[f"x{i}"], flat[f"y{i}"] = f"{round(px, 2):g}", f"{round(py, 2):g}"
        return [a.format(**flat) if "{" in a else a for a in args]

    def _element_point(self, element: int) -> tuple[float, float] | None:
        """An element's click point in global logical pixels, as captured."""
        item = self._elements.get(element)
        return (float(item["x"]), float(item["y"])) if item else None

    def click(self, *, element: int | None = None, x: int | None = None, y: int | None = None,
              button: str = "left", click_count: int = 1, modifiers: list[str] | None = None,
              delivery_mode: str | None = None, bring_to_front: bool = False) -> ActionResult:
        action = "double_click" if click_count == 2 else "click"
        button = (button or "left").lower()
        if button not in ("left", "right", "middle"):
            return _refuse(action, f"unknown button {button!r}: left, right or middle")
        if click_count not in (1, 2):
            return _unsupported(action, f"hypruse clicks once or twice, not {click_count} times")
        if modifiers:
            return _unsupported(action, "hypruse cannot hold modifier keys during a click; press "
                                        "a key combo with key instead", delivery_mode)
        extra = [] if button == "left" else ["--button", button]
        if click_count == 2:
            extra.append("--double")
        if element is not None:
            if element not in self._elements:
                return _refuse(action, _NO_ELEMENTS.format(n=element), delivery_mode=delivery_mode)
            placement, refusal = self._placement(action, delivery_mode)
            if refusal is not None:
                return refusal
            stale = self._marks_current(action, delivery_mode)
            if stale is not None:
                return stale
            refusal = self._bring_forward(action, bring_to_front, delivery_mode)
            if refusal is not None:
                return refusal
            out = self._cli.run("click_ui", "--mark", str(int(element)), *extra, *placement)
            return _result(action, out, delivery_mode)
        if x is None or y is None:
            return _refuse(action, "click needs element= or coordinate=[x, y]")
        point, why = self._current_point(float(x), float(y))
        if point is None:
            return _refuse(action, why, delivery_mode=delivery_mode)
        return self._pointer(action, ["pointer", "click", "{x0}", "{y0}", *extra], [point],
                             delivery_mode, bring_to_front)

    def drag(self, *, from_element: int | None = None, to_element: int | None = None,
             from_xy: tuple[int, int] | None = None, to_xy: tuple[int, int] | None = None,
             button: str = "left", modifiers: list[str] | None = None,
             delivery_mode: str | None = None, bring_to_front: bool = False) -> ActionResult:
        if modifiers:
            return _unsupported("drag", "hypruse cannot hold modifier keys during a drag",
                                delivery_mode)
        button = (button or "left").lower()
        if button not in ("left", "right", "middle"):
            return _refuse("drag", f"unknown button {button!r}: left, right or middle")
        points = []
        if from_element is not None and to_element is not None:
            for n in (from_element, to_element):
                point = self._element_point(n)
                if point is None:
                    return _refuse("drag", _NO_ELEMENTS.format(n=n), delivery_mode=delivery_mode)
                points.append(point)
        elif from_xy is not None and to_xy is not None:
            for px, py in (from_xy, to_xy):
                point, why = self._current_point(float(px), float(py))
                if point is None:
                    return _refuse("drag", why, delivery_mode=delivery_mode)
                points.append(point)
        else:
            return _refuse("drag", "drag needs from_element/to_element or "
                                   "from_coordinate/to_coordinate")
        extra = [] if button == "left" else ["--button", button]
        return self._pointer("drag", ["pointer", "drag", "{x0}", "{y0}", "{x1}", "{y1}", *extra],
                             points, delivery_mode, bring_to_front)

    def scroll(self, *, direction: str, amount: int = 3, element: int | None = None,
               x: int | None = None, y: int | None = None, modifiers: list[str] | None = None,
               delivery_mode: str | None = None, bring_to_front: bool = False) -> ActionResult:
        if modifiers:
            return _unsupported("scroll", "hypruse cannot hold modifier keys during a scroll",
                                delivery_mode)
        steps = max(1, min(50, int(amount or 3)))
        delta = {"down": (steps, 0), "up": (-steps, 0), "right": (0, steps), "left": (0, -steps)}
        if direction not in delta:
            return _refuse("scroll", f"unknown direction {direction!r}: up, down, left or right")
        dy, dx = delta[direction]
        if element is not None:
            point = self._element_point(element)
            if point is None:
                return _refuse("scroll", _NO_ELEMENTS.format(n=element),
                               delivery_mode=delivery_mode)
        elif x is not None and y is not None:
            point, why = self._current_point(float(x), float(y))
            if point is None:
                return _refuse("scroll", why, delivery_mode=delivery_mode)
        elif self._frame is not None:  # the middle of what was captured
            f = self._frame
            point = (f["geometry"][0] + f["geometry"][2] / 2, f["geometry"][1] + f["geometry"][3] / 2)
        else:
            return _refuse("scroll", "scroll needs a capture, an element or a coordinate")
        return self._pointer("scroll", ["pointer", "scroll", str(dy), str(dx), "--at", "{x0}", "{y0}"],
                             [point], delivery_mode, bring_to_front)

    def _keyboard(self, action: str, verb_args: list[str], stdin: str | None,
                  delivery_mode: str | None, bring_to_front: bool) -> ActionResult:
        addr = self._target_address()
        if not addr:
            return _refuse(action, _NO_TARGET, delivery_mode=delivery_mode)
        placement, refusal = self._placement(action, delivery_mode)
        if refusal is not None:
            return refusal
        refusal = self._bring_forward(action, bring_to_front, delivery_mode)
        if refusal is not None:
            return refusal
        out = self._cli.run("keyboard", *verb_args, "--window", addr, *placement, stdin=stdin)
        return _result(action, out, delivery_mode)

    def type_text(self, text: str, *, delivery_mode: str | None = None,
                  bring_to_front: bool = False) -> ActionResult:
        if not text:
            return _refuse("type", "type needs text")
        # stdin, so the text is in no argv (ps shows argv to every local user)
        return self._keyboard("type", ["type", "-"], text, delivery_mode, bring_to_front)

    def key(self, keys: str, *, delivery_mode: str | None = None,
            bring_to_front: bool = False) -> ActionResult:
        combo, why = _normalize_combo(keys)
        if combo is None:
            return _unsupported("key", why, delivery_mode)
        return self._keyboard("key", ["key", combo], None, delivery_mode, bring_to_front)

    def set_value(self, value: str, element: int | None = None) -> ActionResult:
        return _unsupported("set_value", (
            "hypruse has no set-value verb. For a text field, click it by element, then type; for "
            "a check box or button, click it by element; for a dropdown, click it and then click "
            "the option."))
