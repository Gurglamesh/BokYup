# NixOS module: run the BokYup authority server. BokYup itself is runtime-agnostic — you
# choose HOW to run it with `runtime`:
#   "native" — the Python server directly as a systemd service (no container runtime).
#   "docker" — the Nix-built OCI image, isolated in a container. Docker is INSTALLED/ENABLED
#              for you automatically if it isn't already.
#   "podman" — same, on Podman (enabled for you).
#
# You normally get this via the flake:  imports = [ inputs.bokyup.nixosModules.bokyup-server ];
# (the flake injects both `package` (native) and `imageFile` (container)). Minimal use:
#
#   services.bokyup-server = {
#     enable = true;
#     # runtime = "docker";   # the default; set "native" for no container
#     backup.enable = true;
#   };
#
# The server binds to 127.0.0.1 only — publish it to your tailnet with
# `tailscale serve --bg 8756` (see docs/server-flake.md). Books live in dataDir; that
# directory is the one thing to back up.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.bokyup-server;
  useContainer = cfg.runtime != "native";
  containerBackend = if cfg.runtime == "podman" then "podman" else "docker";
  # The systemd unit that actually runs the server, whichever way it runs.
  unit = if useContainer then "${containerBackend}-bokyup-server.service" else "bokyup-server.service";
in
{
  options.services.bokyup-server = {
    enable = lib.mkEnableOption "the BokYup self-hosted authority server";

    runtime = lib.mkOption {
      type = lib.types.enum [ "native" "docker" "podman" ];
      default = "docker";
      description = ''
        How to run the server — BokYup works the same either way, pick what fits your host:
          "native" — run the Python server directly as a systemd service. No container
                     runtime required; lightest footprint.
          "docker" — run the Nix-built OCI image isolated in a container. Docker is
                     enabled for you (virtualisation.docker + autoPrune) if it isn't
                     already, so `enable = true` works on a host without Docker.
          "podman" — same, on Podman (virtualisation.podman enabled for you).
        All three share the same token, data directory, and backup handling. (Already run
        your own Docker/Podman? Selecting it here just merges with your existing enable.)
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      description = "The server launcher to run in `runtime = \"native\"` (injected by the flake).";
    };

    imageFile = lib.mkOption {
      type = lib.types.package;
      description = "The OCI image to run in `runtime = \"docker\"/\"podman\"` (injected by the flake).";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/bokyup";
      description = "Registry + encrypted books (bind-mounted to /data in a container). THE thing to back up.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Host address the server is published on. Keep 127.0.0.1; publish via `tailscale serve`.";
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
        Path to a file containing ONLY the API bearer token (just the token, no KEY=VALUE),
        kept off the Nix store. With generateToken = true (the default) it is created for you
        on first activation and left alone thereafter — read it with `sudo cat` to configure a
        client. Set generateToken = false to manage it yourself (agenix/sops) and point this
        at the decrypted secret's path. In container runtimes it is bind-mounted read-only.
      '';
    };

    generateToken = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Declaratively ensure tokenFile exists: on activation, if it is missing (or is the
        empty directory a first container bind-mount can leave behind) a fresh random token is
        written there, 0600 root:root, OUTSIDE the Nix store. Generated ONCE and never rotated
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
    # The "docker"/"podman" runtime installs + enables the container backend for you (mkDefault,
    # so it just merges if you already run it). "native" pulls in no container runtime at all.
    virtualisation.docker = lib.mkIf (cfg.runtime == "docker") {
      enable = lib.mkDefault true;
      autoPrune.enable = lib.mkDefault true;
    };
    virtualisation.podman = lib.mkIf (cfg.runtime == "podman") {
      enable = lib.mkDefault true;
    };

    # Declaratively materialise the API token on the host (never in the Nix store). Runs before
    # the server and heals the empty directory a first container bind-mount can leave at tokenFile.
    systemd.services.bokyup-token = lib.mkIf cfg.generateToken {
      description = "Ensure the BokYup API token file exists (generate once if missing)";
      path = [ pkgs.coreutils ];
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
      script = ''
        set -euo pipefail
        tok=${lib.escapeShellArg (toString cfg.tokenFile)}
        # A first container run with a missing bind-mount source creates a DIRECTORY there.
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

    # --- native runtime: run the launcher directly as a systemd service ---
    systemd.services.bokyup-server = lib.mkIf (cfg.runtime == "native") {
      description = "BokYup self-hosted authority server (native)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ] ++ lib.optional cfg.generateToken "bokyup-token.service";
      requires = lib.optional cfg.generateToken "bokyup-token.service";
      environment = {
        BOKYUP_DATA_DIR = toString cfg.dataDir;
        BOKYUP_HOST = cfg.host;
        BOKYUP_PORT = toString cfg.port;
        BOKYUP_AUTOLOCK = toString cfg.autolockSeconds;
        BOKYUP_CORS = lib.concatStringsSep "," cfg.corsOrigins;
        BOKYUP_API_TOKEN_FILE = toString cfg.tokenFile;
      };
      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        Restart = "on-failure";
        RestartSec = 2;
      };
    };

    # --- container runtime (docker/podman): run the Nix-built OCI image ---
    systemd.services."${containerBackend}-bokyup-server" = lib.mkIf (useContainer && cfg.generateToken) {
      after = [ "bokyup-token.service" ];
      requires = [ "bokyup-token.service" ];
    };

    virtualisation.oci-containers = lib.mkIf useContainer {
      backend = lib.mkDefault containerBackend;
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

    # Consistent nightly backup: stop the server (SQLite closes cleanly), archive, restart.
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
