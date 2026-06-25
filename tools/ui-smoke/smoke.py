#!/usr/bin/env python3
"""
Headless-browser smoke of the real web frontend against a live FastAPI server.

The frontend is vanilla JS with no other automated coverage, so this drives the
actual DOM (modals, section rendering, the new Phase 4 + backup flows) in Chromium
and fails on any uncaught page error. Not part of pytest (needs Playwright + a
browser).

  pip install playwright
  # a browser is needed; either `playwright install chromium` or point at an
  # existing build:  BOKYUP_CHROME=/path/to/chrome  python tools/ui-smoke/smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import uvicorn  # noqa: E402
from backend.api import create_app  # noqa: E402

PORT = int(os.environ.get("BOKYUP_SMOKE_PORT", "8791"))
CHROME = os.environ.get("BOKYUP_CHROME")  # optional explicit chrome binary
DBPATH = os.path.join(tempfile.mkdtemp(), "smoke.db")
BUYN = os.path.join(tempfile.mkdtemp(), "backup.buyn")


def _fill_modal(page, values):
    page.wait_for_selector("#modal-backdrop:not(.hidden)")
    fields = page.query_selector_all("#modal-body input, #modal-body select")
    for el, val in zip(fields, values):
        if el.evaluate("e => e.tagName") == "SELECT":
            el.select_option(val)
        else:
            el.fill(val)
    page.click("#modal-ok")


def main() -> None:
    app = create_app(app_dir=tempfile.mkdtemp(), autolock_seconds=9000)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.5)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        if CHROME:
            launch["executable_path"] = CHROME
        browser = p.chromium.launch(**launch)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{PORT}/app/")
        page.wait_for_selector("#book-tabs")

        page.click("text=+ Ny bok")
        _fill_modal(page, ["Smoke AB", DBPATH, "smoke-pass"])
        page.wait_for_selector("text=Bokför")

        page.click(".nav >> text=Kategorier")
        page.click("text=+ Ny kategori")
        _fill_modal(page, ["Konsult", "income", "3001"])
        page.wait_for_selector("text=Konsult")
        page.click("table >> text=Ändra")
        _fill_modal(page, ["Konsultarvode", "3001"])
        page.wait_for_selector("text=Konsultarvode")

        page.click(".nav >> text=Inställningar")
        page.click("text=Skapa/ersätt återställningsnyckel")
        _fill_modal(page, ["smoke-pass"])
        page.wait_for_selector(".box .v")
        rk = page.inner_text(".box .v")

        page.click("text=Exportera säkerhetskopia")
        _fill_modal(page, [BUYN])
        page.wait_for_selector("#toast:not(.hidden)")
        time.sleep(0.3)
        browser.close()

    assert rk and "-" in rk, "recovery key not shown"
    assert os.path.exists(BUYN), ".buyn backup not written"
    assert not errors, f"JS page errors: {errors}"
    print("UI SMOKE PASSED (book create, category add+edit, recovery key, backup export)")


if __name__ == "__main__":
    main()
