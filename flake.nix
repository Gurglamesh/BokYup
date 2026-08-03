{
  description = "BokYup — self-hosted authority server (flake outputs; the app itself is unaffected)";

  # The server is the SAME backend as the app (single-sourced — a legal book must not run
  # divergent schema/verifikationsnummer logic), packaged here as an opt-in flake output.
  # It runs ISOLATED IN A CONTAINER via a Nix-built OCI image + virtualisation.oci-containers.
  # Consume it from your system flake:
  #
  #   inputs.bokyup.url = "github:gurglamesh/bokyup";
  #   inputs.bokyup.inputs.nixpkgs.follows = "nixpkgs";
  #   # in your host modules:
  #   imports = [ inputs.bokyup.nixosModules.bokyup-server ];
  #   services.bokyup-server = { enable = true; tokenFile = ...; backup.enable = true; };

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      # Runtime Python env for the headless server (the ONLY runtime deps;
      # httpx2/pytest/pywebview are test/desktop-only).
      pythonEnvFor = pkgs: pkgs.python3.withPackages (ps: with ps; [
        fastapi uvicorn pydantic argon2-cffi cryptography pillow fpdf2
      ]);

      # The launcher: put this flake's backend source on PYTHONPATH and run the headless
      # server module. Used both for `nix run` and as the container's entrypoint.
      serverPkgFor = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonEnv = pythonEnvFor pkgs;
        in
        pkgs.writeShellApplication {
          name = "bokyup-server";
          runtimeInputs = [ pythonEnv ];
          text = ''
            export PYTHONPATH="${self}:''${PYTHONPATH:-}"
            exec python -m backend.server "$@"
          '';
        };

      # A reproducible OCI image (no Dockerfile). Binds 0.0.0.0 INSIDE the container; the
      # NixOS module maps it to 127.0.0.1 on the host and publishes via Tailscale.
      containerFor = system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in pkgs.dockerTools.buildLayeredImage {
          name = "bokyup-server";
          tag = "latest";
          contents = [ (serverPkgFor system) ];
          config = {
            Cmd = [ "/bin/bokyup-server" ];
            Env = [ "BOKYUP_DATA_DIR=/data" "BOKYUP_HOST=0.0.0.0" "BOKYUP_PORT=8756" ];
            ExposedPorts = { "8756/tcp" = { }; };
          };
        };
    in
    {
      packages = forAllSystems (system: rec {
        bokyup-server = serverPkgFor system;   # the launcher (nix run / container entrypoint)
        container = containerFor system;       # the OCI image the NixOS module loads + runs
        default = bokyup-server;
      });

      apps = forAllSystems (system: rec {
        bokyup-server = { type = "app"; program = "${serverPkgFor system}/bin/bokyup-server"; };
        default = bokyup-server;
      });

      # The NixOS service module, with the container image pre-wired to this flake's build.
      nixosModules.bokyup-server = { pkgs, lib, ... }: {
        imports = [ ./deploy/nixos/bokyup-server-module.nix ];
        services.bokyup-server.imageFile =
          lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.container;
      };
      nixosModules.default = self.nixosModules.bokyup-server;

      # `nix flake check` builds these: the launcher + the OCI image (validates the Python
      # env, the entrypoint, and the image assembly).
      checks = forAllSystems (system: {
        bokyup-server = self.packages.${system}.bokyup-server;
        container = self.packages.${system}.container;
      });
    };
}
