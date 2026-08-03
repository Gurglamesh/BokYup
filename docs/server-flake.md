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

BokYup is **runtime-agnostic** — you choose how to run it with `services.bokyup-server.runtime`:

| `runtime` | what it does |
| --------- | ------------ |
| `"docker"` (default) | runs the Nix-built OCI image isolated in a container; **installs + enables Docker for you** if it isn't already |
| `"podman"` | same, on Podman (enabled for you) |
| `"native"` | runs the Python server directly as a systemd service — **no container runtime at all** |

So on the container path you don't declare Docker yourself; `enable = true` is enough. The one
thing you still bring is **Tailscale** (to reach the loopback-bound server). Keep that a
separate concern in your own `server-tools.nix` (see `deploy/nixos/servertools.nix`):

```nix
# server-tools.nix
{ config, pkgs, ... }:
{
  services.tailscale.enable = true;
  networking.firewall = {
    trustedInterfaces = [ "tailscale0" ];
    allowedUDPPorts = [ config.services.tailscale.port ];
  };
  # Already run your own Docker? Declaring virtualisation.docker.enable = true here is fine —
  # it just merges with what runtime = "docker" sets.
}
```

Don't want a container at all? Set `runtime = "native"` — nothing Docker/Podman gets pulled in.

## 2. The API token

By default (`generateToken = true`) the module **creates the token for you** on first
activation: a random 32-byte url-safe token written to `tokenFile` (default
`/var/lib/bokyup-token`), `0600 root:root`, **outside the Nix store**, generated once and
never rotated on its own. Nothing imperative to run — just rebuild. Read it back to
configure a client:

```bash
sudo cat /var/lib/bokyup-token
```

Prefer to manage the secret yourself? Set `generateToken = false` and point `tokenFile` at
an **agenix**/**sops-nix** decrypted path (or any root-only file you provision):

```nix
services.bokyup-server.generateToken = false;
services.bokyup-server.tokenFile = config.age.secrets.bokyup-token.path;
```

## 3. Configure the service — `bokyup.nix`

```nix
{ config, ... }:
{
  services.bokyup-server = {
    enable = true;
    # Token is auto-generated at /var/lib/bokyup-token (read it with `sudo cat`).
    # To manage it yourself instead:
    #   generateToken = false;
    #   tokenFile = config.age.secrets.bokyup-token.path;
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
