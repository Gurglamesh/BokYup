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
      default = "/var/lib/bokyup-token";
      description = ''
        Path to a file containing ONLY the API bearer token (just the token, no KEY=VALUE).
        It is bind-mounted read-only into the container; it is kept off the Nix store.
        With generateToken = true (the default) this file is created for you on first
        activation and left alone thereafter — read it with `sudo cat` to configure a client.
        Set generateToken = false to manage it yourself (agenix/sops) and point this at the
        decrypted secret's path.
      '';
    };

    generateToken = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Declaratively ensure tokenFile exists: on activation, if it is missing (or is the
        empty directory a first bind-mount can leave behind) a fresh random token is written
        there, 0600 root:root, OUTSIDE the Nix store. It is generated ONCE and never rotated
        automatically — delete the file to force a new one. Set false to provision the token
        yourself with agenix/sops-nix and just point tokenFile at it.
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
    # We do NOT enable a container runtime here — declare it yourself (e.g. in a
    # server-tools.nix: `virtualisation.docker.enable = true;`). This module only runs the
    # container on whatever backend is configured, and asserts one is actually enabled.
    assertions = [{
      assertion = (backend == "docker" -> config.virtualisation.docker.enable)
               && (backend == "podman" -> config.virtualisation.podman.enable);
      message = ''
        services.bokyup-server needs a container backend enabled. Declare it yourself,
        e.g. in your server-tools.nix:  virtualisation.docker.enable = true;
        (or set virtualisation.oci-containers.backend = "podman" and enable
        virtualisation.podman.enable = true;).
      '';
    }];

    # Declaratively materialise the API token on the host (never in the Nix store).
    # Runs before the container and heals the empty directory a first bind-mount can leave
    # at tokenFile when the file doesn't exist yet.
    systemd.services.bokyup-token = lib.mkIf cfg.generateToken {
      description = "Ensure the BokYup API token file exists (generate once if missing)";
      path = [ pkgs.coreutils ];
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
      script = ''
        set -euo pipefail
        tok=${lib.escapeShellArg (toString cfg.tokenFile)}
        # A first `docker run` with a missing bind-mount source creates a DIRECTORY there.
        if [ -d "$tok" ]; then rm -rf "$tok"; fi
        if [ ! -s "$tok" ]; then
          umask 077
          # 32 random bytes, url-safe base64, no padding/newlines — same shape as
          # secrets.token_urlsafe(32); only needs coreutils.
          head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' > "$tok"
          chmod 600 "$tok"
        fi
      '';
    };

    # Make the container wait for the token to be in place.
    systemd.services."${backend}-bokyup-server" = lib.mkIf cfg.generateToken {
      after = [ "bokyup-token.service" ];
      requires = [ "bokyup-token.service" ];
    };

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
