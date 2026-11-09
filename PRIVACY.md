# Privacy

hypruse runs on your computer. It has no account, no server of its own and no telemetry, and it makes no network connections.

## What it reads

When your agent calls a tool, hypruse reads what that tool needs from your desktop: the window and workspace list, screenshots, the accessibility tree of an app (control names and their values), your keybinds and, only if you set `HYPRUSE_CLIPBOARD=1`, the clipboard. It hands the result to the agent that called it and to nothing else. If that agent is Claude, the data travels to Anthropic as part of your conversation, under Anthropic's terms.

## What it keeps

- In `$XDG_RUNTIME_DIR/hypruse/`, which lives in memory and is emptied when you log out: the newest 20 screenshots, the activity status file the Waybar module reads, and the small state the shell verbs share between calls (window addresses and mark numbers).
- A screenshot the agent was asked to save somewhere else, at the path it named.
- Only if you set `HYPRUSE_JOURNAL`: a log of tool calls in `$XDG_STATE_HOME/hypruse/journal.ndjson`, rotated to one old generation. It records that the agent looked, never what it saw. Typed and copied text is stored as a length and a digest unless you also set `HYPRUSE_JOURNAL_TEXT`.

The setup commands you run yourself, `hypruse init` and `hypruse skill install`, write the MCP client configs and skill links they describe. hypruse writes nothing else and sends nothing anywhere.

## Installing it

`uvx`, the Claude Code plugin and `uv tool install` download hypruse and its Python dependencies from PyPI. The AUR and Nix packages come from those services. Each host has its own policy for those downloads.

## Questions

Open an issue at https://github.com/IlyasKhallouki/hypruse/issues. For anything sensitive, use the private vulnerability report on the repository's Security tab.
