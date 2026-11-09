"""``hermes-hypruse``: put the provider where Hermes looks for it.

Hermes discovers computer-use providers as directories under
``$HERMES_HOME/plugins/<name>/`` and has no entry point for them, so a pip
or ``uv tool`` install of this package does nothing on its own. This
command links the installed ``hermes_hypruse`` package there as
``hypruse`` (or copies it with ``--copy``). It lives outside the package
because the package itself imports Hermes, which this environment may not
have.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from pathlib import Path

NAME = "hypruse"


def package_dir() -> Path:
    spec = importlib.util.find_spec("hermes_hypruse")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("hermes-hypruse: the hermes_hypruse package is not installed")
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def hermes_home(given: str | None) -> Path:
    raw = given or os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(raw).expanduser()


def install(home: Path, copy: bool) -> int:
    source, target = package_dir(), home / "plugins" / NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        print(f"hermes-hypruse: {target} already exists; run `hermes-hypruse uninstall` first",
              file=sys.stderr)
        return 1
    if copy:
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"copied {source} to {target}")
    else:
        target.symlink_to(source, target_is_directory=True)
        print(f"linked {target} -> {source}")
    print("select it with: hermes config set computer_use.backend hypruse")
    return 0


def uninstall(home: Path) -> int:
    target = home / "plugins" / NAME
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        if not (target / "plugin.yaml").exists() or "name: hypruse" not in (
                target / "plugin.yaml").read_text(encoding="utf-8"):
            print(f"hermes-hypruse: {target} is not this provider; leaving it", file=sys.stderr)
            return 1
        shutil.rmtree(target)
    else:
        print(f"hermes-hypruse: nothing at {target}")
        return 0
    print(f"removed {target}; set computer_use.backend back to cua (or another provider)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-hypruse", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--hermes-home", metavar="DIR", help="default: $HERMES_HOME or ~/.hermes")
        if name == "install":
            p.add_argument("--copy", action="store_true", help="copy instead of linking")
    sub.add_parser("path", help="print the provider directory")
    args = parser.parse_args(argv)
    if args.cmd == "path":
        print(package_dir())
        return 0
    home = hermes_home(args.hermes_home)
    return install(home, args.copy) if args.cmd == "install" else uninstall(home)


if __name__ == "__main__":
    raise SystemExit(main())
