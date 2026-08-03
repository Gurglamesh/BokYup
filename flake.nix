{
  description = "BokYup — self-hosted authority server (flake outputs; the app itself is unaffected)";

  # The server is the SAME backend as the app (single-sourced — a legal book must not run
  # divergent schema/verifikationsnummer logic), packaged here as an opt-in flake output so
  # it only matters when you deploy a server. Consume it from your system flake:
  #
  #   inputs.bokyup.url = "github:gurglamesh/bokyup";     # or a path / ref
  #   # in your nixosConfiguration modules:
  #   imports = [ inputs.bokyup.nixosModules.bokyup-server ];
  #   services.bokyup-server = { enable = true; tokenFile = ...; backup.enable = true; };
  #
  # Tip: add `inputs.bokyup.inputs.nixpkgs.follows = "nixpkgs";` to build against your nixpkgs.

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      # Runtime Python env for the headless server. These are the ONLY runtime deps
      # (httpx2/pytest/pywebview are test/desktop-only and not needed here).
      pythonEnvFor = pkgs: pkgs.python3.withPackages (ps: with ps; [
        fastapi
        uvicorn
        pydantic
        argon2-cffi
        cryptography
        pillow
        fpdf2
      ]);

      serverPkgFor = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonEnv = pythonEnvFor pkgs;
        in
        pkgs.writeShellApplication {
          name = "bokyup-server";
          runtimeInputs = [ pythonEnv ];
          text = ''
            # The backend source is this flake's tree; put it on PYTHONPATH and run the
            # headless server module. Config comes from BOKYUP_* env vars (see backend/server.py).
            export PYTHONPATH="${self}:''${PYTHONPATH:-}"
            exec python -m backend.server "$@"
          '';
        };
    in
    {
      packages = forAllSystems (system: rec {
        bokyup-server = serverPkgFor system;
        default = bokyup-server;
      });

      apps = forAllSystems (system: rec {
        bokyup-server = {
          type = "app";
          program = "${serverPkgFor system}/bin/bokyup-server";
        };
        default = bokyup-server;
      });

      # The NixOS service module, with the package pre-wired to this flake's build.
      nixosModules.bokyup-server = { pkgs, lib, ... }: {
        imports = [ ./deploy/nixos/bokyup-server-module.nix ];
        services.bokyup-server.package = lib.mkDefault self.packages.${pkgs.system}.bokyup-server;
      };
      nixosModules.default = self.nixosModules.bokyup-server;
    };
}
