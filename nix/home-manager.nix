self:
{
  config,
  lib,
  options,
  pkgs,
  ...
}:
let
  cfg = config.programs.hypruse;
in
{
  options.programs.hypruse = {
    enable = lib.mkEnableOption "hypruse, computer use for Hyprland over MCP";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.hypruse;
      defaultText = lib.literalExpression "hypruse.packages.\${pkgs.stdenv.hostPlatform.system}.hypruse";
      description = "The hypruse package to install.";
    };

    env = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        HYPRUSE_BACKGROUND = "1";
      };
      description = ''
        Environment for the MCP server entry, for example `HYPRUSE_READONLY = "1"`
        or `HYPRUSE_BACKGROUND = "1"`.
      '';
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      { home.packages = [ cfg.package ]; }

      # home-manager's shared MCP registry, which programs.claude-code (with
      # enableMcpIntegration) and the other MCP client modules read. Older
      # home-manager releases have no such option, and there hypruse is only
      # installed.
      (lib.optionalAttrs (options ? programs.mcp.servers) {
        programs.mcp.servers.hypruse = {
          command = lib.getExe cfg.package;
          inherit (cfg) env;
        };
      })
    ]
  );
}
