# NixOS module: run the BokYup authority server as a native systemd service (no Docker).
#
# You normally get this via the flake:  imports = [ inputs.bokyup.nixosModules.bokyup-server ];
# (the flake injects `services.bokyup-server.package`). Then, minimally:
#
#   services.bokyup-server = {
#     enable = true;
#     tokenFile = config.age.secrets.bokyup-token.path;   # a file containing ONLY the token
#     backup.enable = true;
#   };
#
# The server binds 127.0.0.1 by default — publish it to your tailnet with
# `tailscale serve --bg 8756` (see docs/server-nixos.md). Books live in dataDir; that
# directory is the one thing to back up (this module can do it nightly).
{ config, lib, pkgs, ... }:

let
  cfg = config.services.bokyup-server;
in
{
  options.services.bokyup-server = {
    enable = lib.mkEnableOption "the BokYup self-hosted authority server";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The bokyup-server package (normally injected by the flake).";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/bokyup";
      description = "Where the registry + encrypted books live. THE thing to back up.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Bind address. Keep 127.0.0.1 and publish via `tailscale serve`.";
    };

    port = lib.mkOption { type = lib.types.port; default = 8756; description = "TCP port."; };

    autolockSeconds = lib.mkOption {
      type = lib.types.int;
      default = 900;
      description = "Idle seconds after which a book's DEK is wiped from server RAM.";
    };

    corsOrigins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "*" ];
      description = "Allowed CORS origins for app clients (token-gated, so may be broad).";
    };

    tokenFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to a file containing ONLY the API bearer token (no KEY=VALUE, just the token).
        Kept off the Nix store and out of the environment via systemd LoadCredential — use
        an agenix/sops secret, or a root-only file you create by hand. Generate a token:
          python3 -c "import secrets; print(secrets.token_urlsafe(32))"
      '';
    };

    user = lib.mkOption { type = lib.types.str; default = "bokyup"; description = "Service user."; };
    group = lib.mkOption { type = lib.types.str; default = "bokyup"; description = "Service group."; };

    backup = {
      enable = lib.mkEnableOption "a consistent nightly local backup of dataDir";
      dir = lib.mkOption {
        type = lib.types.path;
        default = "/var/backup/bokyup";
        description = "Where nightly archives land (put on a separate disk if you can).";
      };
      keep = lib.mkOption { type = lib.types.int; default = 14; description = "Archives to keep."; };
      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 04:00:00";
        description = "systemd OnCalendar schedule.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    users.users = lib.mkIf (cfg.user == "bokyup") {
      bokyup = { isSystemUser = true; group = cfg.group; home = cfg.dataDir; };
    };
    users.groups = lib.mkIf (cfg.group == "bokyup") { bokyup = { }; };

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0700 ${cfg.user} ${cfg.group} - -"
    ];

    systemd.services.bokyup-server = {
      description = "BokYup self-hosted authority server";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        BOKYUP_DATA_DIR = cfg.dataDir;
        BOKYUP_HOST = cfg.host;
        BOKYUP_PORT = toString cfg.port;
        BOKYUP_AUTOLOCK = toString cfg.autolockSeconds;
        BOKYUP_CORS = lib.concatStringsSep "," cfg.corsOrigins;
      };
      serviceConfig = {
        # The token is exposed to the process only via $CREDENTIALS_DIRECTORY/token (tmpfs,
        # 0400) — never in the store or the environment.
        LoadCredential = [ "token:${cfg.tokenFile}" ];
        ExecStart = pkgs.writeShellScript "bokyup-server-start" ''
          export BOKYUP_API_TOKEN_FILE="$CREDENTIALS_DIRECTORY/token"
          exec ${cfg.package}/bin/bokyup-server
        '';
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";
        RestartSec = 5;
        # Hardening.
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        ReadWritePaths = [ cfg.dataDir ];
      };
    };

    # Consistent nightly backup: stop the service (SQLite closes cleanly), archive, restart.
    systemd.services.bokyup-backup = lib.mkIf cfg.backup.enable {
      description = "Nightly consistent backup of the BokYup data directory";
      path = [ pkgs.gnutar pkgs.gzip pkgs.coreutils pkgs.findutils pkgs.systemd ];
      serviceConfig.Type = "oneshot";
      script = ''
        set -euo pipefail
        mkdir -p ${lib.escapeShellArg cfg.backup.dir}
        stamp=$(date +%Y%m%d-%H%M%S)
        dest=${lib.escapeShellArg cfg.backup.dir}/bokyup-"$stamp".tgz
        systemctl stop bokyup-server
        if tar czf "$dest" -C ${lib.escapeShellArg cfg.dataDir} . ; then
          systemctl start bokyup-server
        else
          systemctl start bokyup-server
          echo "backup FAILED" >&2; exit 1
        fi
        ls -1t ${lib.escapeShellArg cfg.backup.dir}/bokyup-*.tgz \
          | tail -n +${toString (cfg.backup.keep + 1)} | xargs -r rm -f
        echo "backup written: $dest"
      '';
    };

    systemd.timers.bokyup-backup = lib.mkIf cfg.backup.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.backup.onCalendar;
        Persistent = true;
        RandomizedDelaySec = "10m";
      };
    };
  };
}
