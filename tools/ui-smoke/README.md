# UI smoke test

Drives the real vanilla-JS frontend in a headless browser against a live FastAPI
server, exercising the flows that have no other automated coverage and failing on any
uncaught JS page error:

- create a book (modal)
- add **and edit** a category (reference-editing flow)
- generate a recovery key (key-management flow)
- export an encrypted `.buyn` backup

Not part of `pytest` (needs Playwright + a Chromium build).

```bash
pip install playwright
playwright install chromium            # or reuse an existing build:
#   BOKYUP_CHROME=/path/to/chrome python tools/ui-smoke/smoke.py
python tools/ui-smoke/smoke.py
```

Env vars: `BOKYUP_CHROME` (explicit chrome binary), `BOKYUP_SMOKE_PORT` (default 8791).
