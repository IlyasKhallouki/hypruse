"""hypruse's reach into the browser, through the hypruse-browser extension.

Chrome starts `hypruse browser-host` when the extension connects, over native messaging
(`framing`). The host relays between that pipe and a socket only the owner can reach
(`host`), and any local program talks to the socket with `client.BrowserClient`.
`install` writes the manifest that tells Chrome where the host is.

The extension lives in its own repository, github.com/IlyasKhallouki/hypruse-browser, and
docs/PROTOCOL.md there is the message format this package speaks.
"""

from hypruse.browser.client import (
    BrowserClient,
    BrowserError,
    BrowserUnavailable,
    socket_path,
    socket_paths,
)

__all__ = ["BrowserClient", "BrowserError", "BrowserUnavailable", "socket_path", "socket_paths"]
