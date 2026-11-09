# Releasing

The launch and every release after it. Steps marked **[you]** need your
accounts/credentials; the rest is automated.

## One-time setup

1. **[you] Make the repo public** on GitHub (Settings → General → Danger
   Zone). The awesome-list PRs and AUR source tarballs depend on this.
2. **[you] PyPI Trusted Publisher:** on PyPI, add a *pending* publisher so
   the first `release` workflow run can publish without a token
   (https://pypi.org/manage/account/publishing/):
   - Project: `hypruse`
   - Owner: `IlyasKhallouki`  · Repo: `hypruse`
   - Workflow: `release.yml`  · Environment: `pypi`
3. **[you] AUR SSH key:** add your public key at
   https://aur.archlinux.org/account, if not already done.

## Cutting a release

1. Bump the version in ALL of `pyproject.toml` (`version`, feeds the
   PyPI/tag build), `src/hypruse/__init__.py` (`__version__`, feeds
   `hypruse --version`), `server.json` (`version` *and*
   `packages[0].version`, the MCP registry entry), `skills/hypruse/SKILL.md`
   (`metadata.version`) and `.claude-plugin/plugin.json` (`version`, the
   Claude Code plugin: users get a new copy only when it changes, and the
   `hypruse==` pin in `mcpServers.hypruse.args`, the release the plugin's
   server runs); tests guard that they agree. Move the CHANGELOG
   `[Unreleased]` entries under the new version.
2. Tag and push the tag, not `main` yet:
   ```sh
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```
   The `release` workflow builds, publishes to PyPI via OIDC, publishes
   `server.json` to the MCP registry (also OIDC, after waiting for the
   release to land on PyPI), and cuts a GitHub release with the artifacts.
3. Verify: `uvx hypruse==0.1.0 --version`, and the registry entry:
   ```sh
   curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.IlyasKhallouki/hypruse"
   ```
4. Push `main` once the release is on PyPI. The plugin's server runs the
   exact release its `hypruse==` pin names, and both the plugin's own
   marketplace and Anthropic's directory read `main`, so a `main` pushed
   before PyPI has the release hands out a pin that cannot install yet.
   ```sh
   git push origin main
   ```
   Anthropic's directory then scans the new commit and holds it for a
   reviewer (a pinned uvx package is always held), so check the plugin's
   page at https://claude.ai/directory/manage after a release.

## AUR

Stable (`packaging/aur/hypruse/`), after the tag exists:

```sh
cd packaging/aur/hypruse
updpkgsums                     # fills the real sha256 for the tag tarball
makepkg --printsrcinfo > .SRCINFO
# push to the AUR remote:
#   git clone ssh://aur@aur.archlinux.org/hypruse.git
#   copy PKGBUILD + .SRCINFO in, commit, push
```

`hypruse-git` never needs `updpkgsums` (VCS source); regenerate its
`.SRCINFO` the same way and push to `ssh://aur@aur.archlinux.org/hypruse-git.git`.

## Listings (after public + first release)

- `awesome-mcp-servers` and `awesome-hyprland`: PR one entry each.
- The MCP registry is automated (see above), first publish included.

Test a PKGBUILD locally before pushing: `makepkg -si` in its directory.

## MCP registry

`server.json` is the registry entry; `mcp-publisher validate` checks it
against the live schema before a tag goes out.

Two things are easy to break and both cost a whole release to fix, because
the registry validates ownership against the README PyPI serves as the
package description, and a published version's description is immutable:

- The `<!-- mcp-name: … -->` comment at the top of `README.md` must match
  `server.json`'s `name` exactly, trailing boundary included.
- The name's namespace must match the GitHub account the OIDC token comes
  from, case for case: `io.github.IlyasKhallouki/*`. The registry builds
  the publish permission straight from the account name and compares by
  prefix, so a lowercased namespace is a 403.

A test guards the first. To publish by hand (a re-run after a failed
workflow, say), authenticate as yourself instead of via CI:

```sh
mcp-publisher login github     # device flow
mcp-publisher publish
```
