# BokYup server on NixOS (Tailscale + Docker)

A concrete, copy-pasteable walkthrough for hosting the **authority server** on a NixOS box
and reaching it from your other devices over Tailscale. Read `docs/server.md` first for the
model (the server is the single writer/authority; the app connects to it).

Everything below is tailnet-only — **no ports are opened to the internet.**

---

## 1. Enable Tailscale + Docker (NixOS module)

A ready-to-import system module lives at **`deploy/nixos/servertools.nix`** (Tailscale +
Docker + compose + the `docker` group for user `gurglamesh`). These are system services, so
it's a NixOS module imported from `configuration.nix` — independent of hjem, which manages
your user's files, not system services.

Copy it into your config tree and import it:

```nix
# configuration.nix (or your modules aggregator)
imports = [ ./servertools.nix ];   # adjust the path
```

Change the username in `users.users.gurglamesh.extraGroups` if needed, then apply:

```bash
sudo nixos-rebuild switch
```

> Prefer inlining over a separate module? The three blocks inside `servertools.nix`
> (`services.tailscale.enable`, the `networking.firewall` tailnet bits, and
> `virtualisation.docker.enable` + the `docker` group) can go straight into
> `configuration.nix`.

Then bring this machine onto your tailnet (opens a browser link to authenticate):

```bash
sudo tailscale up
tailscale ip -4          # your 100.x.y.z address
tailscale status         # confirm you're connected
```

In the **Tailscale admin console → DNS**: enable **MagicDNS** and **HTTPS Certificates**
(needed so `tailscale serve` can give the server an `https://…ts.net` name with a real cert).

---

## 2. Build + run the BokYup server (Docker)

```bash
git clone <your BokYup repo> BokYup
cd BokYup
git checkout claude/bokyup-server     # the server lives on this branch (not on main)

cd deploy
cp .env.example .env

# generate a strong token and put it in .env as BOKYUP_API_TOKEN:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
$EDITOR .env                          # paste it into BOKYUP_API_TOKEN=...

docker compose up -d --build
docker compose logs -f                # should print: BokYup server: http://0.0.0.0:8756
```

- The port is published on **`127.0.0.1:8756` only** — not on your LAN, not the internet.
- Books + registry live in `deploy/bokyup-data/` (the container's `/data` volume). **Back
  this directory up** — it is the authoritative copy.

---

## 3. Publish it to your tailnet (HTTPS, tailnet-only)

```bash
sudo tailscale serve --bg 8756
tailscale serve status        # prints https://<hostname>.<tailnet>.ts.net
```

That HTTPS URL is reachable **only by devices on your tailnet**. It proxies to the
container's `127.0.0.1:8756`. (To stop sharing later: `sudo tailscale serve --https=443 off`.)

---

## 4. Connect the BokYup app (any device on the tailnet)

Open BokYup (desktop, phone, or just the server's own UI at
`https://<hostname>.<tailnet>.ts.net/app` in a browser) →

1. Choose **"Anslut till server"**.
2. **Server-URL**: `https://<hostname>.<tailnet>.ts.net`
   (on the server box itself you can use `http://127.0.0.1:8756`).
3. **API-token**: the `BOKYUP_API_TOKEN` from `deploy/.env`.

Then **create a new book** (you only give it a name + passphrase — the server assigns the
file under its data dir) or import a `.buyn`. Each book is still unlocked with its own
passphrase; the DEK lives in the server's RAM only while the book is open (auto-locks when
idle).

Switch back to local any time via **"Byt anslutning"** on the home screen.

---

## Handy commands

```bash
docker compose logs -f              # server logs
docker compose restart              # after changing .env (e.g. rotating the token)
cd deploy && git pull && docker compose up -d --build   # update the server
tailscale serve status              # what's published to the tailnet
```

## Notes / gotchas

- **`docker` group needs a re-login** to take effect (or run compose with `sudo`).
- If `tailscale serve` can't get a cert, re-check MagicDNS + HTTPS are enabled in the admin
  console, and that the machine has a MagicDNS name (`tailscale status`).
- **Do not** change the compose `ports:` to `0.0.0.0:8756` — that would expose it on your
  LAN. Keep it `127.0.0.1` and reach it via `tailscale serve`.
- Rotating the token: edit `deploy/.env`, `docker compose up -d`, then reconnect the app
  with the new token.
- Backups: `deploy/bokyup-data/` is authoritative for now. Also keep taking manual `.buyn`
  exports. (Phase 2 will add automatic per-device encrypted replicas so a server loss is
  survivable.)
