# App icon

The BokYup app icon — a green ledger page with an accountant's tick. This is the
**app/brand** icon (Windows exe, Android launcher, web favicon), separate from the
per-book **company logo** shown on invoices (`company.logo_enc`).

## Source

- `bokyup-icon.svg` — the full icon (rounded green background + ledger + tick).
- `bokyup-foreground.svg` — ledger + tick only, padded for the Android adaptive-icon
  safe zone (transparent background).

## Regenerate everything

Edit the SVG(s), then from the repo root:

```bash
python packaging/icon/generate.py
```

This rasterises (Playwright/Chromium) and rewrites the committed assets:

| Target | Files |
|---|---|
| Windows exe | `packaging/icon/bokyup.ico` (wired in `packaging/bokyup.spec`) |
| Web / PWA | `backend/api/static/icons/*` + `…/manifest.webmanifest` (linked in `index.html`) |
| Android | `phone/assets/icon-only\|foreground\|background.png` → `@capacitor/assets generate --android` in CI |

The generated files are committed so the release build never has to rasterise.
