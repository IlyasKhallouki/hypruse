{
  description = "Computer use for Hyprland: semantic desktop state over IPC plus vision and native Wayland input, in one MCP server";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});

      # one version, read from pyproject.toml, so a release does not need a
      # second edit here
      version = (lib.importTOML ./pyproject.toml).project.version;

      # only what the wheel and the test suite read, so an edit to the docs
      # or the AUR files does not rebuild the package
      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./README.md # hatchling reads it for the package metadata
          ./LICENSE
          ./src
          ./skills
          ./tests
          ./server.json # the version and registry-name tests read it
          ./.claude-plugin # and the plugin version tests read its manifests
          ./waybar/hypruse-status.sh # and the Waybar module's tests run it
        ];
      };

      hypruseFor = pkgs: pkgs.callPackage ./nix/package.nix { inherit src version; };
    in
    {
      packages = forAllSystems (
        _: pkgs:
        let
          hypruse = hypruseFor pkgs;
        in
        {
          inherit hypruse;
          default = hypruse;
        }
      );

      apps = forAllSystems (
        system: _: {
          hypruse = {
            type = "app";
            program = lib.getExe self.packages.${system}.hypruse;
            meta.description = "Run the hypruse MCP server, or a verb such as doctor";
          };
          default = self.apps.${system}.hypruse;
        }
      );

      overlays.default = final: _prev: { hypruse = hypruseFor final; };

      homeModules.default = import ./nix/home-manager.nix self;
    };
}
