# servertools.nix — host tooling for running a BokYup authority server on NixOS.
#
# This is a SYSTEM module (Tailscale + Docker are system services), so it is imported
# from your configuration.nix / flake — nothing here touches hjem (which manages your
# user's files, not system services):
#
#     imports = [ ./servertools.nix ];   # or ./modules/servertools.nix
#
# Then:  sudo nixos-rebuild switch
#
# It sets up ONLY the tools (Tailscale + Docker). The BokYup server itself is then built
# and run from deploy/ with docker compose — see docs/server-nixos.md. (A fully declarative
# oci-containers service is an optional follow-up.)
{ config, pkgs, ... }:

{
  # --- Tailscale: the only path in (tailnet-only; no public ports) ---
  services.tailscale.enable = true;

  networking.firewall = {
    # Trust the tailnet interface so `tailscale serve` can reach local services.
    trustedInterfaces = [ "tailscale0" ];
    # Helps Tailscale establish direct (non-relayed) connections.
    allowedUDPPorts = [ config.services.tailscale.port ];   # 41641
    # NOTE: we deliberately do NOT open 8756 — Docker binds it to 127.0.0.1 and
    # `tailscale serve` proxies it inside the tailnet.
  };

  # --- Docker: to build + run the BokYup server container ---
  virtualisation.docker = {
    enable = true;
    autoPrune.enable = true;          # weekly tidy of dangling images/layers
  };

  # Let gurglamesh use docker without sudo (needs a re-login to take effect).
  users.users.gurglamesh.extraGroups = [ "docker" ];

  # git for cloning the repo; docker-compose = Compose v2 (invoke as `docker compose`
  # or `docker-compose`). The tailscale CLI comes from services.tailscale above.
  environment.systemPackages = with pkgs; [
    git
    docker-compose
  ];
}
