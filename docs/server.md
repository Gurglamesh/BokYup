# BokYup self-hosted server (Phase 1)

Run BokYup's backend as a small server on your own Linux box and reach it from your
devices. **The server is the single writer/authority** (so the verifikationsnummer stays
unbroken); clients talk to it over a secure channel. Books live in one server directory;
clients supply a *name*, never a server path. Start behind **Tailscale** — no ports on the
internet — and add TLS/public exposure later, carefully.

> This does not change local/offline use. The app still runs fully local by default; the
> server is an opt-in mode you choose per launch (and per book, later).

## 1. Run it (Docker, Linux)

```bash
cd deploy
cp .env.example .env
# set a strong token:
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into .env as BOKYUP_API_TOKEN
docker compose up -d --build
```

- Books + registry live in `deploy/bokyup-data/` (the `/data` volume) — **back this up.**
- The port is published on **127.0.0.1:8756 only**, never `0.0.0.0`, so it is not on your
  LAN or the internet.

## 2. Reach it from other devices — Tailscale

Install Tailscale on the server and each device (same tailnet). Then, on the server,
publish the local port to your tailnet:

```bash
tailscale serve --bg 8756
tailscale serve status          # shows the https://<machine>.<tailnet>.ts.net URL
```

That URL (HTTPS, only reachable inside your tailnet) is what you give the app.

## 3. Connect the app

Open BokYup on any device → choose **Anslut till server** → enter:
- **Server-URL**: your `https://<machine>.<tailnet>.ts.net` (or `http://127.0.0.1:8756` on
  the server box itself)
- **API-token**: the `BOKYUP_API_TOKEN` value

Then create or import a book — it lives on the server. Each book is still unlocked with its
own passphrase.

## Security notes (read before exposing anything)

- **Token gate:** every API call needs the bearer token; the server refuses to start
  without one. Keep it secret; rotate by changing `.env` and restarting.
- **DEK in server RAM:** unlocking a book sends its passphrase to the server (over the
  Tailscale/TLS channel) and the DEK lives in server memory until auto-lock. The trust
  boundary is your server box — treat it like your own laptop. The database is still
  encrypted at rest, so backups/disk theft stay protected.
- **Tailscale first.** Do **not** publish on `0.0.0.0` or forward a router port yet. Public
  exposure (real TLS, rate-limiting, lockout) is a later, deliberate phase.
- **Backups:** the `bokyup-data/` volume is the authoritative copy for now — back it up.
  Phase 2 adds automatic live replicas on every device so a server loss is survivable.

## Updating the server

```bash
cd deploy && git pull && docker compose up -d --build
```

Book data lives in the `/data` volume, outside the image, so rebuilds never touch it.
`schema.migrate()` upgrades books on open, so a schema bump across an update is handled.
