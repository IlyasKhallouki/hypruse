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

1. Bump `version` in `pyproject.toml` and move the CHANGELOG `[Unreleased]`
   entries under the new version.
2. Tag and push:
   ```sh
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```
   The `release` workflow builds, publishes to PyPI via OIDC, and cuts a
   GitHub release with the artifacts.
3. Verify: `uvx hypruse --version`.

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
- The MCP registry.

Test a PKGBUILD locally before pushing: `makepkg -si` in its directory.
