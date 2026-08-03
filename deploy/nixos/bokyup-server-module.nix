# NixOS module: run the BokYup authority server ISOLATED IN A CONTAINER
# (virtualisation.oci-containers) from a Nix-built OCI image. Docker is enabled for you.
#
# You normally get this via the flake:  imports = [ inputs.bokyup.nixosModules.bokyup-server ];
# (the flake injects `services.bokyup-server.imageFile`). Minimal use:
#
#   services.bokyup-server = {
#     enable = true;
#     tokenFile = config.age.secrets.bokyup-token.path;   # a file containing ONLY the token
#     backup.enable = true;
#   };
#
# The container's port is published on 127.0.0.1 only — publish it to your tailnet with
# `tailscale serve --bg 8756` (see docs/server-flake.md). Books live in dataDir (bind-mounted
# to /data in the container); that directory is the one thing to back up.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.bokyup-server;
  backend = config.virtualisation.oci-containers.backend;
  unit = "${backend}-bokyup-server.service";
in
{
  options.services.bokyup-server = {
    enable = lib.mkEnableOption "the BokYup self-hosted authority server (isolated in a container)";

    imageFile = lib.mkOption {
      type = lib.types.package;
      description = "The OCI image to run (normally injected by the flake).";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/bokyup";
      description = "Host dir bind-mounted to /data — registry + encrypted books. THE thing to back up.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Host address the container port is published on. Keep 127.0.0.1; publish via `tailscale serve`.";
    };

    port = lib.mkOption { type = lib.types.port; default = 8756; description = "Published host port."; };

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
        Path to a file containing ONLY the API bearer token (just the token, no KEY=VALUE).
        It is bind-mounted read-only into the container; keep it off the Nix store (agenix/
        sops secret, or a root-only file). Generate one:
          python3 -c "import secrets; print(secrets.token_urlsafe(32))"
      '';
    };

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
    # Container runtime (Docker by default; set oci-containers.backend = "podman" to switch).
    virtualisation.docker.enable = lib.mkDefault true;

    virtualisation.oci-containers = {
      backend = lib.mkDefault "docker";
      containers.bokyup-server = {
        imageFile = cfg.imageFile;
        image = "bokyup-server:latest";
        # Publish on the host loopback only; reach it over Tailscale.
        ports = [ "${cfg.host}:${toString cfg.port}:8756" ];
        volumes = [
          "${cfg.dataDir}:/data"
          "${cfg.tokenFile}:/run/bokyup-token:ro"   # the token, read-only
        ];
        environment = {
          BOKYUP_AUTOLOCK = toString cfg.autolockSeconds;
          BOKYUP_CORS = lib.concatStringsSep "," cfg.corsOrigins;
          BOKYUP_API_TOKEN_FILE = "/run/bokyup-token";
        };
      };
    };

    systemd.tmpfiles.rules = [ "d ${cfg.dataDir} 0700 root root - -" ];

    # Consistent nightly backup: stop the container (SQLite closes cleanly), archive, restart.
    systemd.services.bokyup-backup = lib.mkIf cfg.backup.enable {
      description = "Nightly consistent backup of the BokYup data directory";
      path = [ pkgs.gnutar pkgs.gzip pkgs.coreutils pkgs.findutils pkgs.systemd ];
      serviceConfig.Type = "oneshot";
      script = ''
        set -euo pipefail
        mkdir -p ${lib.escapeShellArg cfg.backup.dir}
        stamp=$(date +%Y%m%d-%H%M%S)
        dest=${lib.escapeShellArg cfg.backup.dir}/bokyup-"$stamp".tgz
        systemctl stop ${unit}
        if tar czf "$dest" -C ${lib.escapeShellArg cfg.dataDir} . ; then
          systemctl start ${unit}
        else
          systemctl start ${unit}
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
