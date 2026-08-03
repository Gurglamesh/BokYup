# BokYup server on NixOS with flakes (isolated in a container)

The recommended way to run the authority server on NixOS. The server is packaged as a
**flake output** of this repo (same backend code as the app — never a separate/forked
copy, so the schema + verifikationsnummer logic can't drift). It runs **isolated in a
container** via a **Nix-built OCI image** + `virtualisation.oci-containers` — declarative
and reproducible, no `Dockerfile`/registry, no manual `docker build`. Docker is enabled
for you by the module.

> Plain `docker compose` on a non-NixOS host? See `docs/server-nixos.md`. Same server.

## Flake outputs

- `packages.<system>.bokyup-server` — the headless server launcher (`nix run`).
- `packages.<system>.container` — the OCI image the module loads + runs.
- `nixosModules.bokyup-server` — the declarative container service (+ optional nightly backup).

## 1. Add the input

In your **system flake** (`/etc/nixos/flake.nix` or wherever your config lives):

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    bokyup.url = "github:gurglamesh/bokyup";          # or path:/home/gurglamesh/BokYup
    bokyup.inputs.nixpkgs.follows = "nixpkgs";         # build against your nixpkgs
  };

  outputs = { self, nixpkgs, bokyup, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        ./server-tools.nix                             # YOU declare Docker + Tailscale here
        bokyup.nixosModules.bokyup-server              # the server service
        ./bokyup.nix                                   # your settings (below)
      ];
    };
  };
}
```

The BokYup module does **not** install Docker — it runs the server as an oci-container on
whatever backend you declare (and asserts one is enabled, so you get a clear error if not).
Keep the runtime a separate concern in your own `server-tools.nix` (see
`deploy/nixos/servertools.nix` for a ready one):

```nix
# server-tools.nix
{ config, pkgs, ... }:
{
  virtualisation.docker.enable = true;
  services.tailscale.enable = true;
  networking.firewall = {
    trustedInterfaces = [ "tailscale0" ];
    allowedUDPPorts = [ config.services.tailscale.port ];
  };
}
```

## 2. A token secret

The service reads the token from a file via systemd `LoadCredential`, so it never lands in
the Nix store or the environment. Easiest with **agenix** or **sops-nix**; or a plain
root-only file:

```bash
sudo install -m600 /dev/stdin /var/lib/bokyup-token <<< "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

## 3. Configure the service — `bokyup.nix`

```nix
{ config, ... }:
{
  services.bokyup-server = {
    enable = true;
    tokenFile = "/var/lib/bokyup-token";   # or config.age.secrets.bokyup-token.path
    # host/port default to 127.0.0.1:8756 (publish via tailscale serve, below)
    backup.enable = true;                  # nightly consistent local backup of the data dir
    # backup.dir = "/mnt/backup/bokyup";   # put on another disk if you can
  };
}
```

Rebuild:

```bash
sudo nixos-rebuild switch --flake .#myhost
systemctl status docker-bokyup-server        # the container's systemd unit
docker logs docker-bokyup-server             # server logs
```

## 4. Publish to your tailnet + connect

```bash
sudo tailscale up            # once
sudo tailscale serve --bg 8756
tailscale serve status       # https://<host>.<tailnet>.ts.net
```

Open BokYup → **Anslut till server** → that HTTPS URL + the token → create a book.

## Data + backups

- Books + registry live in **`/var/lib/bokyup`** (`services.bokyup-server.dataDir`). This is
  the one thing to protect.
- `backup.enable = true` writes a consistent nightly archive to `backup.dir` (default
  `/var/backup/bokyup`) — it briefly stops the service so SQLite closes cleanly, tars the
  data dir, restarts, and keeps the newest `backup.keep` (14) archives.
- **Keep a copy OFF the machine.** Point `backup.dir` at a mounted second disk, or add your
  own restic/borg/rsync job over the archives (or over `/var/lib/bokyup` after a
  `systemctl stop bokyup-server`). A Tailscale peer makes a fine off-site target.
- The app's **`.buyn` export** is a complementary portable, per-book encrypted backup.

## Restore

```bash
sudo systemctl stop docker-bokyup-server
sudo rm -rf /var/lib/bokyup/* && sudo tar xzf /var/backup/bokyup/bokyup-YYYYMMDD-HHMMSS.tgz -C /var/lib/bokyup
sudo systemctl start docker-bokyup-server
```

## Ad-hoc run (no service)

```bash
BOKYUP_API_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
BOKYUP_DATA_DIR=./bokyup-data \
nix run github:gurglamesh/bokyup#bokyup-server
```
