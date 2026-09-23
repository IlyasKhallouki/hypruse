"""hypruse command line.

`hypruse` with no arguments runs the MCP stdio server (what clients
spawn). The human-facing subcommands wrap the first-run experience and
the record an agent leaves behind:

    hypruse doctor   diagnose the environment, exit 0 only if all green
    hypruse init     register hypruse in detected MCP clients (asks per
                     client, backs up configs), then run doctor
    hypruse stop     emergency stop, releasing anything a drag holds
    hypruse journal  read back what an agent did (HYPRUSE_JOURNAL)
    hypruse replay   re-issue a journal's actions, dry by default
    hypruse skill    install the Agent Skill that teaches the verbs

The tools themselves are verbs too (`hypruse desktop`, `hypruse click_ui
Save`), for agents that run shell commands instead of MCP: see verbs.py.

init never overwrites an existing hypruse entry: if a client already has
one, whatever its shape, it is reported and left alone.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

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
    # which config manager is running decides the whole IPC dialect, so it
    # is the first thing to know when window ops misbehave
    return True, (
        f"instance {sig[:12]}..., {len(monitors)} monitor(s), "
        f"{hyprctl.provider()} config"
    )


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


def _skill_note() -> tuple[bool, str]:
    """Informational: the skill is for agents that run shell commands, and
    an MCP-only setup is complete without it."""
    from hypruse import skill

    canonical = Path(skill.CANONICAL).expanduser() / skill.NAME
    if (canonical / "SKILL.md").is_file():
        return True, f"agent skill installed at {canonical}"
    return True, "agent skill not installed (hypruse skill install, for shell-driven agents)"


def _browser_note() -> tuple[bool, str]:
    """The bridge to the hypruse-browser extension. Optional, so it never fails the report."""
    from hypruse.browser import install

    return install.describe(install.status())


CHECKS = (
    ("dependencies", _check_deps),
    ("session", _check_session),
    ("events", _check_events),
    ("pointer", _check_pointer),
    ("screenshot", _check_screenshot),
    ("mode", _mode_note),
    ("skill", _skill_note),
    ("browser", _browser_note),
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


def _init_skill(assume_yes: bool, wanted: bool) -> None:
    """The skill is what agents that run shell commands (Pi, Codex, Hermes,
    OpenClaw, or Claude Code without the MCP server) read to learn the
    verbs. Offered, never forced: an MCP-only setup does not need it."""
    from hypruse import skill

    if wanted:
        skill.install([], copy=False)
        return
    # --yes answers the client questions; it does not write into other
    # agents' directories unasked, that is what --skill is for
    if not assume_yes and _ask("- Install the agent skill into your agents' skill directories?",
                               assume_yes=False):
        skill.install([], copy=False)
    else:
        print("- agent skill: skipped (hypruse init --skill, or hypruse skill install)")


def init(assume_yes: bool, install_skill: bool = False) -> int:
    print("hypruse init: registering with detected MCP clients\n")
    _init_claude_code(assume_yes)
    _init_claude_desktop(assume_yes)
    _init_skill(assume_yes, install_skill)
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

    stopped = 0
    # a shell verb acting beside a live server never touches the server's
    # beacon, so it is only findable through the lock it holds while acting
    verb_pid = _running_verb_pid()
    if verb_pid and verb_pid != os.getpid():
        try:
            os.kill(verb_pid, signal.SIGTERM)
            print(f"stopped a running hypruse verb (pid {verb_pid})")
            stopped += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"not permitted to signal pid {verb_pid}")
            return 1
    path = safety.state_path()
    if not path.exists():
        if not stopped:
            print("no active hypruse session (no beacon found)")
        return 0
    try:
        pid = int(json.loads(path.read_text())["pid"])
    except (json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError):
        print(f"beacon at {path} is unreadable; run: pkill -f hypruse")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        with contextlib.suppress(OSError):
            path.unlink()
        print(f"hypruse pid {pid} was already gone; cleared the stale beacon")
        return 0
    except PermissionError:
        print(f"not permitted to signal pid {pid}")
        return 1
    print(f"stopped hypruse (pid {pid})")
    return 0


def _running_verb_pid() -> int | None:
    """The pid of a shell verb currently acting, read from the lock it
    holds (see verbs._lock), or None when the lock is free or absent."""
    import fcntl

    from hypruse import verbs

    lock = verbs.lock_path()
    try:
        with open(lock) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # held: a verb is acting right now
                raw = fh.read().strip()
                return int(raw) if raw.isdigit() else None
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return None
    return None


# --- journal ----------------------------------------------------------------


def _resolve_journal(given: str) -> Path | None:
    """The file to read: an explicit path, else whatever HYPRUSE_JOURNAL
    points at, else the default location (which may simply not exist yet,
    if recording was never turned on)."""
    if given:
        return Path(given).expanduser()
    from hypruse import journal

    return journal.path() or journal.default_path()


def _clock(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "--:--:--"


# Everything printed from a journal was written by the party being
# audited. An agent chooses `launch(command=)`, `hypr(workspace=)`,
# `use_bind(combo=)` and the rest verbatim, and the replay plan on screen
# is the only review a human gives before --execute, so a value carrying
# ESC sequences could erase the lines above it and show a plan that is
# not the one that runs. Same treatment as safety._ACTION_JUNK, for the
# same reason: whitelist before re-embedding in output we build.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_VALUE_MAX = 120


def _safe(value: Any) -> str:
    text = _CONTROL.sub(".", str(value))
    return text[:_VALUE_MAX] + "..." if len(text) > _VALUE_MAX else text


def _skip(value: Any) -> bool:
    """Empty, absent, or off. Written out rather than `value in ("", None,
    False)`, which also swallowed 0 and 0.0: a click at (0, 0) printed
    with no coordinates at all."""
    return value is None or value is False or value == ""


def _fmt_args(args: Any) -> str:
    """Recorded arguments on one line, defaults and redacted text folded
    down so the interesting part of a call is what shows."""
    if not isinstance(args, dict):  # a hand-edited or truncated journal
        return _safe(args)
    parts = []
    for key, value in args.items():
        if _skip(value):
            continue
        if isinstance(value, dict) and value.get("redacted"):
            parts.append(f"{_safe(key)}=<{_safe(value.get('chars', '?'))} chars>")
        elif isinstance(value, list):
            parts.append(f"{_safe(key)}=[{len(value)}]")
        else:
            parts.append(f"{_safe(key)}={_safe(value)}")
    return " ".join(parts)


_INDENT = " " * 18


def _fmt_entry(entry: dict[str, Any], verbose: bool = False) -> str:
    """One line per call, because the arguments already say what happened.
    A refusal or an error earns a second line, since that is what someone
    reading a journal came for; results need --verbose."""
    seq, when = _safe(entry.get("seq", "?")), _clock(entry.get("ts", ""))
    if entry.get("kind") == "session":
        mode = entry.get("mode") if isinstance(entry.get("mode"), dict) else {}
        flags = " ".join(f"{_safe(k)}={_safe(v)}" for k, v in mode.items() if v)
        tail = ""
        if entry.get("event") == "start":
            tail = f"{_safe(entry.get('version', ''))} {flags or 'no trust flags set'}".strip()
        event, pid = _safe(entry.get("event", "")), _safe(entry.get("pid"))
        if entry.get("source"):
            event += f" ({_safe(entry['source'])})"
        return f"{seq:>5}  {when}  session {event} pid {pid} {tail}"
    kind = "act " if entry.get("kind") == "act" else "look"
    tool, detail = _safe(entry.get("tool", "?")), _fmt_args(entry.get("args"))
    line = f"{seq:>5}  {when}  {kind} {tool:<10} {detail}"
    if entry.get("dry"):
        line += "  (dry)"
    if entry.get("source"):
        line += f"  ({_safe(entry['source'])})"
    if entry.get("by"):
        line += f"  (by {_safe(entry['by'])})"
    outcome = _safe(entry.get("outcome", "?"))
    if outcome != "ok":
        line += f"\n{_INDENT}{outcome.upper()}  {_safe(entry.get('error', ''))}".rstrip()
    elif verbose and entry.get("result"):
        line += f"\n{_INDENT}{_safe(entry['result'])}"
    return line


def journal_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hypruse journal", description="Read back what an agent did."
    )
    parser.add_argument("path", nargs="?", default="", help="journal file (default: env)")
    parser.add_argument("-n", "--tail", type=int, default=40, help="last N entries (0 = all)")
    parser.add_argument("--acts", action="store_true", help="only actions, not observations")
    parser.add_argument("--refused", action="store_true", help="only what a trust layer refused")
    parser.add_argument("-v", "--verbose", action="store_true", help="also show each result")
    args = parser.parse_args(argv)

    from hypruse import journal

    source = _resolve_journal(args.path)
    if source is None or not source.exists():
        print(f"no journal at {source}; set HYPRUSE_JOURNAL=1 in the server env to record one")
        return 1
    entries = list(journal.read(source))
    shown = entries
    if args.acts:
        shown = [e for e in shown if e.get("kind") in ("act", "session")]
    if args.refused:
        shown = [e for e in shown if e.get("outcome") == "refused"]
    if args.tail > 0:
        shown = shown[-args.tail :]

    print(f"{source}  ({len(entries)} entries)\n")
    for entry in shown:
        print(_fmt_entry(entry, args.verbose))
    acts = sum(1 for e in entries if e.get("kind") == "act")
    looks = sum(1 for e in entries if e.get("kind") == "observe")
    refused = sum(1 for e in entries if e.get("outcome") == "refused")
    errors = sum(1 for e in entries if e.get("outcome") == "error")
    dry = sum(1 for e in entries if e.get("dry"))
    print(
        f"\n{acts} actions ({dry} dry), {looks} observations, "
        f"{refused} refused by a trust layer, {errors} errors"
    )
    return 0


# --- replay -----------------------------------------------------------------

_ADDRESS_ARGS = ("window", "target")


def _args_of(entry: dict[str, Any]) -> dict[str, Any]:
    """A journal is a file, and a file can be hand-edited or truncated.
    Anything that is not an object is treated as no arguments at all,
    which the pre-flight then rejects rather than crashing on."""
    args = entry.get("args")
    return args if isinstance(args, dict) else {}


def _addresses(entry: dict[str, Any]) -> list[str]:
    args = _args_of(entry)
    return [str(args[k]) for k in _ADDRESS_ARGS if str(args.get(k, "")).startswith("0x")]


def _stale(entries: list[dict[str, Any]]) -> list[str]:
    """Recorded window addresses that no longer exist. Hyprland addresses
    are per-window and not stable across a restart of the app, let alone a
    reboot, so a journal from yesterday mostly names windows that are gone.
    An address CAN also be reused by a different window later, which no
    check here can see: replay is for re-running a flow on a desktop that
    still looks like the one recorded, not for reviving an old session."""
    from hypruse import hyprctl

    wanted = {a for e in entries for a in _addresses(e)}
    if not wanted:
        return []
    try:
        live = {c.get("address") for c in hyprctl.query("clients")}
    except Exception as exc:
        raise SystemExit(
            f"cannot read the window list to check the journal's targets: {exc}"
        ) from exc
    return sorted(wanted - live)


def _replay_args(entry: dict[str, Any]) -> dict[str, Any]:
    """The recorded call, minus the `then=` observation (replay reports
    its own progress and a snapshot per step would bury it)."""
    return {k: v for k, v in _args_of(entry).items() if k != "then"}


def _redacted_steps(entries: list[dict[str, Any]]) -> list[int]:
    from hypruse import journal

    return [
        e.get("seq", -1) for e in entries if journal.redacted_text(_args_of(e).get("text"))
    ]


def _unreplayable(entries: list[dict[str, Any]]) -> list[str]:
    """Recorded actions this version cannot re-issue at all, with the
    reason. Checked BEFORE the seat is taken: a refusal that lands halfway
    through leaves the desktop part-way through someone else's plan."""
    reasons = []
    for entry in entries:
        tool, args, seq = entry.get("tool"), _args_of(entry), entry.get("seq", "?")
        if tool not in journal_module().REPLAYABLE:
            reasons.append(f"seq {seq}: {_safe(tool)} (recorded by a newer hypruse)")
        elif not args:
            reasons.append(f"seq {seq}: {_safe(tool)} has no recorded arguments")
        elif tool == "click_ui" and args.get("mark"):
            # marks live in the server process that drew them, so a fresh
            # replay has no numbering to resolve `mark` against
            mark = _safe(args["mark"])
            reasons.append(f"seq {seq}: click_ui(mark={mark}) needs a live marks capture")
        elif tool == "clipboard" and not _flag_on("HYPRUSE_CLIPBOARD"):
            # the clipboard tool is opt-in on the SERVER; replay must not
            # be the way around that gate
            reasons.append(f"seq {seq}: clipboard is opt-in, set HYPRUSE_CLIPBOARD=1 to replay it")
    return reasons


def journal_module():
    from hypruse import journal

    return journal


def _flag_on(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _take_beacon() -> bool:
    """Raise the activity beacon for this replay, unless a live hypruse
    already owns it. Overwriting a running server's beacon would point
    `hypruse stop` and the Waybar indicator at the replay's pid, and
    clearing it on the way out would leave the still-running server
    invisible and un-stoppable."""
    from hypruse import safety

    path = safety.state_path()
    if path.exists():
        with contextlib.suppress(Exception):
            os.kill(int(json.loads(path.read_text())["pid"]), 0)  # liveness only
            return False
    safety.init()
    return True


def _countdown(seconds: int) -> bool:
    """Returns False if the human took the abort the prompt offers. That
    abort must exit cleanly, not with a traceback: it is the documented
    way out, not a crash."""
    print(f"taking the cursor and keyboard in {seconds}s, Ctrl+C to abort", flush=True)
    try:
        for left in range(seconds, 0, -1):
            print(f"  {left}...", end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\naborted, nothing was replayed")
        return False
    print("  replaying   ")
    return True


def _gap_before(entry: dict[str, Any], previous: dict[str, Any] | None) -> float:
    """Think time between two recorded calls: the wall clock between their
    timestamps, minus the time the later call itself took (timestamps are
    written on completion). Never negative."""
    if previous is None:
        return 0.0
    try:
        a = datetime.fromisoformat(str(previous["ts"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(entry["ts"]).replace("Z", "+00:00"))
        gap = (b - a).total_seconds() - float(entry.get("ms", 0)) / 1000.0
    except (ValueError, TypeError, KeyError, OverflowError):
        return 0.0  # a hand-edited timestamp must not stop a replay in its tracks
    return gap if gap > 0 else 0.0  # NaN compares false, so it lands on 0.0 too


def replay(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="hypruse replay",
        description="Re-issue the actions in a journal. Prints the plan and stops "
        "unless --execute is given.",
    )
    parser.add_argument("path", nargs="?", default="", help="journal file (default: env)")
    parser.add_argument("--execute", action="store_true", help="actually perform the actions")
    parser.add_argument("--from", dest="start", type=int, default=0, help="first seq to replay")
    parser.add_argument("--to", dest="end", type=int, default=0, help="last seq to replay")
    parser.add_argument("--speed", type=float, default=1.0, help="time scale (2 = twice as fast)")
    parser.add_argument(
        "--max-gap", type=float, default=2.0, help="cap on the pause between actions, seconds"
    )
    parser.add_argument(
        "--skip-missing", action="store_true", help="skip actions whose window is gone"
    )
    parser.add_argument("--yes", action="store_true", help="no countdown before taking the seat")
    args = parser.parse_args(argv)
    if args.speed <= 0:
        parser.error("--speed must be greater than 0")

    from hypruse import journal

    source = _resolve_journal(args.path)
    if source is None or not source.exists():
        print(f"no journal at {source}; set HYPRUSE_JOURNAL=1 in the server env to record one")
        return 1
    plan = [e for e in journal.read(source) if journal.replayable(e)]
    if args.start:
        plan = [e for e in plan if e.get("seq", 0) >= args.start]
    if args.end:
        plan = [e for e in plan if e.get("seq", 0) <= args.end]
    if not plan:
        print(f"{source}: nothing to replay in that range")
        return 1

    print(f"{source}: {len(plan)} action(s) to replay\n")
    for entry in plan:
        seq = _safe(entry.get("seq"))
        print(f"  {seq:>5}  {_safe(entry.get('tool'))} {_fmt_args(_replay_args(entry))}")

    blocked = _unreplayable(plan)
    if blocked:
        # Dropping these and running the rest would produce a run that
        # reports success for a different sequence of events than the one
        # recorded, so the whole run stops rather than part of it.
        print("\nthis hypruse cannot replay:")
        for reason in blocked:
            print(f"  {reason}")
        if args.execute:
            return 1

    gone = _stale(plan)
    if gone:
        print(f"\n{len(gone)} recorded window(s) no longer exist: {', '.join(gone)}")
        if args.skip_missing:
            plan = [e for e in plan if not (set(_addresses(e)) & set(gone))]
            print(f"--skip-missing: {len(plan)} action(s) left")
        elif args.execute:
            print("refusing to replay: pass --skip-missing to run the rest anyway")
            return 1
    if not plan:
        print("nothing left to replay")
        return 1

    redacted = _redacted_steps(plan)
    if redacted:
        print(
            f"\nseq {redacted} typed text that was recorded as a digest, not text "
            "(the default). They cannot be replayed; record with "
            "HYPRUSE_JOURNAL_TEXT=1 if you need to."
        )
        if args.execute:
            return 1

    if not args.execute:
        print("\ndry: nothing was replayed. Pass --execute to perform these actions.")
        return 0
    if _flag_on("HYPRUSE_READONLY"):
        print("HYPRUSE_READONLY is set: refusing to replay actions in read-only mode")
        return 1
    if journal.dry_run():
        # every tool would return its "would have" plan, and reporting
        # "replayed N actions" for that is exactly the phantom success
        # this project ranks below an honest failure
        print("HYPRUSE_DRYRUN is set: --execute would deliver nothing. Unset it to replay for "
              "real, or drop --execute to see the plan.")
        return 1

    from hypruse import input as hinput
    from hypruse import safety, server, session

    session.ensure_session_env()
    # a replay's own actions are journaled, but marked, so that replaying
    # this file again does not run both the original actions and these
    journal.set_origin("replay")
    if _take_beacon():
        safety.on_shutdown(hinput.release_held)
    else:
        print("a hypruse server already holds the activity beacon; leaving it alone")
    if not args.yes and not _countdown(3):
        return 1

    previous: dict[str, Any] | None = None
    done = 0
    for entry in plan:
        gap = min(_gap_before(entry, previous), max(args.max_gap, 0.0)) / args.speed
        if gap:
            time.sleep(gap)
        previous = entry
        seq = _safe(entry.get("seq"))
        tool = getattr(server, str(entry.get("tool")))  # the pre-flight above vetted the name
        try:
            result = tool(**_replay_args(entry))
        except KeyboardInterrupt:
            print(f"\naborted after {done}/{len(plan)} action(s); the desktop is part-way "
                  "through the journal")
            return 1
        except Exception as exc:
            print(f"  {seq:>5}  {_safe(entry.get('tool'))}: {type(exc).__name__}: {_safe(exc)}")
            print(f"stopped after {done}/{len(plan)} action(s); the desktop is part-way "
                  "through the journal")
            return 1
        done += 1
        head = _safe(result if isinstance(result, str) else str(result)).splitlines()
        print(f"  {seq:>5}  {_safe(entry.get('tool'))}: {head[0] if head else ''}")
    print(f"\nreplayed {done} action(s)")
    return 0


# --- entry ------------------------------------------------------------------

_USAGE = """\
usage: hypruse [VERB ...]        hypruse VERB --help for a verb's flags

no arguments   run the MCP stdio server (this is what MCP clients spawn)

The tools as shell verbs (the same guards, journal and beacon as the MCP
server; for agents that run commands). Observation, works in read-only mode:
  desktop                        monitors, workspaces, windows, active, cursor, layers
  screenshot [--window ADDR] [--region x,y,WxH] [--scale F] [--stable] [--lossless]
             [--out PATH]
  zoom X Y [--size WxH] [--window ADDR] [--stable] [--lossless] [--out PATH]
  ui [--window ADDR] [--name TEXT] [--all]
  marks [--window ADDR] [--name TEXT] [--out PATH]
  binds
  wait_for EVENT [--match TEXT] [--timeout S]
Acting (refused with exit 3 under HYPRUSE_READONLY; all take --dry-run, which
rehearses; all but launch and clipboard take --then none|desktop|ui|screenshot,
which appends the effect; pointer, keyboard and click_ui take --allow-auth):
  pointer move X Y | click [X Y] [--button B] [--double] | drag X Y TO_X TO_Y [--button B]
          | scroll DY [DX] [--at X Y]
  keyboard type TEXT|- [--window ADDR] | key COMBO [--window ADDR]
  click_ui NAME [--window ADDR] [--index I] [--button B] [--double]   or   click_ui --mark N
  hypr workspace WS | focus_window ADDR | move_window ADDR WS | close_window ADDR
       | fullscreen [ADDR] | toggle_floating [ADDR]
  launch [--workspace WS] [--wait S] COMMAND...   (the app's own flags after --)
  use_bind COMBO
  sequence STEPS|@file|- [--no-stop-on-change]
  clipboard read | write TEXT|-          needs HYPRUSE_CLIPBOARD=1
Every tool verb takes --json (raw result, one line). click-ui, use-bind and
wait-for are accepted spellings. Exit: 0 ok, 1 error, 2 usage, 3 refused,
4 no result. hypruse VERB [ACTION] --help shows the flags.

For the owner:
  doctor         diagnose dependencies, session, protocols; exit 0 if green
  init [--yes] [--skill]   register hypruse in detected MCP clients, then doctor
  stop           emergency stop: signal a running server to shut down safely
                 (bind it: bind = SUPER SHIFT, BackSpace, exec, hypruse stop)
  journal        read back what an agent did (needs HYPRUSE_JOURNAL);
                 --acts, --refused, -n N
  replay         re-issue a journal's actions through the same guards;
                 prints the plan and stops unless --execute is given
  skill          path | install [--agent NAME] [--copy] | uninstall
  browser        install [--browser chrome|chromium|brave] | uninstall | status:
                 the native host for the hypruse-browser extension
  browser-host   the relay itself; Chrome starts it, you never need to
  serve          the MCP stdio server, explicitly
  --version
"""


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        from hypruse.server import main as server_main

        server_main()
        return
    # help and version must never start the server: an agent's shell has a
    # piped stdin, and a server started there waits forever for a client
    if argv[0] in ("-h", "--help"):
        print(_USAGE, end="")
        sys.exit(0)
    if argv[0] == "--version":
        from hypruse import __version__

        print(f"hypruse {__version__}")
        sys.exit(0)
    from hypruse import verbs

    try:
        code = verbs.main(argv)
        if not sys.stdout.closed:  # `serve` hands stdout to the MCP session, which closes it
            sys.stdout.flush()
    except BrokenPipeError:
        # `hypruse desktop | head` closed the pipe early: that is the
        # reader's choice, not a failure, and the interpreter must not
        # trip over it again while flushing at exit
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 0
    sys.exit(code)
