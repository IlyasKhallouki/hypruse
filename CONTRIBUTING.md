# Contributing

Contributions are welcome, especially the roadmap items marked
`help wanted` (sway/niri support is the big one: `wire.py` already speaks
the wlr protocols; what's missing is an IPC layer equivalent to
`hyprctl.py` for those compositors).

## Setup

```sh
git clone https://github.com/IlyasKhallouki/hypruse
cd hypruse
uv sync --group dev
```

## Before you push

```sh
uv run ruff check .
uv run pytest          # must stay green without a compositor
```

If your change touches live behaviour, also run `uv run pytest -m e2e
--override-ini addopts=` inside a Hyprland session, and
`scripts/e2e_input.py` if it touches input (supervised, it takes the
seat for ~10 seconds).

## Ground rules

- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`).
- Unit tests accompany features; anything pure gets tested pure.
- Never make input tests automatic. The supervised script stays supervised.
- Keep the coordinate contract (see ARCHITECTURE.md) intact.
- No new runtime dependencies without an issue discussion first: the
  zero-daemon, near-zero-dep footprint is a feature.
