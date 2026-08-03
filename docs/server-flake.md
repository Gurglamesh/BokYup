# BokYup server on NixOS with flakes (native systemd, no Docker)

The recommended way to run the authority server on NixOS. The server is packaged as a
**flake output** of this repo (same backend code as the app — never a separate/forked
copy, so the schema + verifikationsnummer logic can't drift). You run it as a **native
systemd service** with a Nix-built Python env — **no Docker needed**.

> Prefer Docker (e.g. non-NixOS host)? See `docs/server-nixos.md`. Both hit the same server.

## Flake outputs

- `packages.<system>.bokyup-server` — the headless server (run ad-hoc with `nix run`).
- `nixosModules.bokyup-server` — the declarative systemd service (+ optional nightly backup).

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
        ./servertools.nix                              # Tailscale (Docker no longer required)
        bokyup.nixosModules.bokyup-server              # the server service
        ./bokyup.nix                                   # your settings (below)
      ];
    };
  };
}
```

Since you're not using Docker for this, you can trim `servertools.nix` down to just
`services.tailscale.enable = true;` + the firewall bits (drop the `virtualisation.docker`
lines).

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
systemctl status bokyup-server
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
sudo systemctl stop bokyup-server
sudo rm -rf /var/lib/bokyup/* && sudo tar xzf /var/backup/bokyup/bokyup-YYYYMMDD-HHMMSS.tgz -C /var/lib/bokyup
sudo chown -R bokyup:bokyup /var/lib/bokyup
sudo systemctl start bokyup-server
```

## Ad-hoc run (no service)

```bash
BOKYUP_API_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
BOKYUP_DATA_DIR=./bokyup-data \
nix run github:gurglamesh/bokyup#bokyup-server
```
