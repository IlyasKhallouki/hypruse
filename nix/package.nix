{
  lib,
  python3Packages,
  versionCheckHook,
  jq,
  libxkbcommon,
  grim,
  wtype,
  wl-clipboard,
  imagemagick,
  systemd,
  hyprland,
  # hyprctl has to match the compositor that is running, so hypruse uses the
  # one your Hyprland session puts on PATH and the package leaves Hyprland out
  # of its closure. Set this to true to append nixpkgs' hyprctl as a last
  # resort for a client that starts hypruse without the session's PATH.
  withHyprctl ? false,
  # the flake passes its own checkout and the version from pyproject.toml
  src,
  version,
}:

python3Packages.buildPythonApplication {
  pname = "hypruse";
  inherit version src;
  pyproject = true;

  build-system = [ python3Packages.hatchling ];

  # 0.13.1 runs on mcp 1 and 2, so nixpkgs' mcp is used as it is
  dependencies = [ python3Packages.mcp ];

  # ctypes.util.find_library finds nothing on NixOS, which would leave the
  # keymap empty and break typing on non-US layouts
  postPatch = ''
    substituteInPlace src/hypruse/keymap.py \
      --replace-fail 'ctypes.util.find_library("xkbcommon")' \
                     '"${lib.getLib libxkbcommon}/lib/libxkbcommon.so.0"'
  '';

  # appended, not prepended: a grim, wtype or busctl the session already has
  # wins, and these only fill the gaps for a client started with a bare PATH
  makeWrapperArgs = [
    "--suffix"
    "PATH"
    ":"
    (lib.makeBinPath (
      [
        grim
        wtype
        wl-clipboard
        imagemagick
        systemd # busctl, for the accessibility tools
      ]
      ++ lib.optional withHyprctl hyprland
    ))
  ];

  # the unit tests need no compositor; pyproject's addopts deselects the
  # e2e and background markers that do
  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    jq # the Waybar module tests
  ];

  pythonImportsCheck = [
    "hypruse"
    "hypruse.server"
  ];

  nativeInstallCheckInputs = [ versionCheckHook ];

  meta = {
    description = "Computer use for Hyprland: semantic desktop state over IPC plus vision and native Wayland input, in one MCP server";
    homepage = "https://github.com/IlyasKhallouki/hypruse";
    changelog = "https://github.com/IlyasKhallouki/hypruse/blob/main/CHANGELOG.md";
    license = lib.licenses.mit;
    mainProgram = "hypruse";
    platforms = lib.platforms.linux;
  };
}
