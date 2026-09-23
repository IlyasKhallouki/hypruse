"""`hypruse browser install | uninstall | status`: tell Chromium browsers where the host is.

Chrome finds a native host through a manifest named after it in the browser's
`NativeMessagingHosts` directory. The manifest's `path` must be an absolute executable and
cannot carry arguments, while Chrome passes the extension's origin as the first argument, so
it points at a two-line launcher that runs `hypruse browser-host` with the Python that did the
installing. `allowed_origins` names the extension by its ID, which the extension pins with a
public key in its own manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from hypruse.browser.client import BrowserClient, BrowserUnavailable, socket_path

HOST_NAME = "dev.hypruse.browser"
EXTENSION_ID = "pcfjlemgcbbpppclachnobhnnnahpgoj"
# where each browser keeps its per-user configuration, relative to the home directory
BROWSERS: dict[str, str] = {
    "chrome": ".config/google-chrome",
    "chromium": ".config/chromium",
    "brave": ".config/BraveSoftware/Brave-Browser",
}
LAUNCHER = ".local/share/hypruse/browser-host"


class InstallError(Exception):
    """Nothing to install into, or a name that is not a browser this knows."""


@dataclass(frozen=True)
class Installed:
    launcher: Path
    manifests: tuple[Path, ...]


@dataclass(frozen=True)
class Status:
    installed: tuple[str, ...]
    launcher: Path | None
    extension: str = ""
    browser: str = ""


def _manifest_path(home: Path, browser: str) -> Path:
    return home / BROWSERS[browser] / "NativeMessagingHosts" / f"{HOST_NAME}.json"


def install(
    *,
    home: Path | None = None,
    browsers: tuple[str, ...] = (),
    python: str | None = None,
    extension_ids: tuple[str, ...] = (EXTENSION_ID,),
) -> Installed:
    """Write the launcher and one manifest per browser.

    Named browsers are installed whether or not they have run yet. With none named, every
    browser whose configuration directory exists gets one.
    """
    home = home or Path.home()
    unknown = [b for b in browsers if b not in BROWSERS]
    if unknown:
        raise InstallError(f"unknown browser {unknown[0]!r}; known: {', '.join(BROWSERS)}")
    targets = browsers or tuple(b for b, rel in BROWSERS.items() if (home / rel).is_dir())
    if not targets:
        raise InstallError(
            "no Chromium browser found in your home directory; name one with --browser"
        )
    launcher = home / LAUNCHER
    launcher.parent.mkdir(parents=True, exist_ok=True)
    interpreter = python or sys.executable
    launcher.write_text(f'#!/bin/sh\nexec "{interpreter}" -m hypruse browser-host "$@"\n')
    launcher.chmod(0o755)
    manifest = {
        "name": HOST_NAME,
        "description": "hypruse: lets local programs drive this browser through hypruse-browser",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ident}/" for ident in extension_ids],
    }
    written: list[Path] = []
    for browser in targets:
        path = _manifest_path(home, browser)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        path.chmod(0o644)
        written.append(path)
    return Installed(launcher, tuple(written))


def uninstall(*, home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    removed: list[Path] = []
    for browser in BROWSERS:
        path = _manifest_path(home, browser)
        if path.exists():
            path.unlink()
            removed.append(path)
    launcher = home / LAUNCHER
    if launcher.exists():
        launcher.unlink()
        removed.append(launcher)
    return removed


def status(*, home: Path | None = None, socket: Path | None = None) -> Status:
    home = home or Path.home()
    installed = tuple(b for b in BROWSERS if _manifest_path(home, b).exists())
    launcher = home / LAUNCHER
    extension = browser = ""
    try:
        with BrowserClient(socket or socket_path(), timeout=1.0) as client:
            extension = str(client.hello.get("extension", ""))
            browser = str(client.hello.get("browser", ""))
    except BrowserUnavailable:
        pass
    return Status(installed, launcher if launcher.exists() else None, extension, browser)


def describe(found: Status) -> tuple[bool, str]:
    """One line for `hypruse doctor`. The bridge is optional, so it never fails the report."""
    if not found.installed:
        return True, "not installed (optional): hypruse browser install"
    where = ", ".join(found.installed)
    if found.extension:
        return True, f"connected: hypruse-browser {found.extension} in {found.browser} ({where})"
    return True, f"installed for {where}; no extension connected (is the browser running?)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hypruse browser")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("install", help="register the native host with Chromium browsers")
    add.add_argument("--browser", action="append", choices=sorted(BROWSERS), default=[])
    add.add_argument(
        "--extension-id",
        action="append",
        default=[],
        help="allow another extension ID as well (a development build)",
    )
    sub.add_parser("uninstall", help="remove the native host registration")
    sub.add_parser("status", help="where it is installed, and whether a browser is connected")
    args = parser.parse_args(argv)
    if args.action == "install":
        try:
            done = install(
                browsers=tuple(args.browser),
                extension_ids=(EXTENSION_ID, *args.extension_id),
            )
        except InstallError as exc:
            print(f"hypruse browser: {exc}", file=sys.stderr)
            return 1
        print(f"launcher: {done.launcher}")
        for path in done.manifests:
            print(f"manifest: {path}")
        print("Load the hypruse-browser extension, then check with: hypruse browser status")
        return 0
    if args.action == "uninstall":
        for path in uninstall():
            print(f"removed: {path}")
        return 0
    ok, line = describe(status())
    print(line)
    return 0 if ok else 1
