#!/usr/bin/env python3
"""
browser_smoke.py — prove the PHONE bundle (phone/www) runs in a browser engine with
the Python backend as WASM and NO server: the exact runtime the Android/iOS WebView
provides. Boots Pyodide in-page, creates an encrypted book, and persists a customer
(encrypted personnummer) + category (SQLite), failing on any uncaught page error.

Needs: Playwright + a Chromium build, and phone/www assembled with the Pyodide
runtime + wheels in phone/www/vendor (see serve.py / phone/README.md).

  pip install playwright && playwright install chromium
  python tools/phone-preview/browser_smoke.py
  # or reuse a browser:  BOKYUP_CHROME=/path/to/chrome python .../browser_smoke.py
"""
from __future__ import annotations

import functools
import http.server
import os
import socketserver
import threading
import time
from pathlib import Path

WWW = str(Path(__file__).resolve().parents[2] / "phone/www")
PORT = int(os.environ.get("BOKYUP_PREVIEW_PORT", "8794"))
CHROME = os.environ.get("BOKYUP_CHROME")


class _H(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".wasm": "application/wasm", ".mjs": "text/javascript",
                      ".whl": "application/zip", ".json": "application/json",
                      ".js": "text/javascript"}

    def log_message(self, *a):
        pass


def main() -> None:
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(_H, directory=WWW))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    from playwright.sync_api import sync_playwright

    def fill_modal(page, values):
        page.wait_for_selector("#modal-backdrop:not(.hidden)")
        for el, val in zip(page.query_selector_all("#modal-body input, #modal-body select"), values):
            el.select_option(val) if el.evaluate("e=>e.tagName") == "SELECT" else el.fill(val)
        page.click("#modal-ok")

    with sync_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        if CHROME:
            launch["executable_path"] = CHROME
        browser = p.chromium.launch(**launch)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{PORT}/index.html")

        t0 = time.time()
        page.wait_for_function("() => !!window.__BOKYUP_NATIVE__", timeout=120000)
        boot = round(time.time() - t0, 1)

        page.click("text=+ Ny bok")
        fill_modal(page, ["Telefon AB", "/bokyup-data/telefon.db", "telefon-pass"])
        page.wait_for_selector("text=Bokför", timeout=30000)

        page.click(".nav >> text=Kunder")
        page.click("text=+ Ny kund")
        fill_modal(page, ["private", "Anna", "Svensson", "811218-9876", "", "", "anna@x.se"])
        page.wait_for_selector("text=Svensson", timeout=15000)

        page.click(".nav >> text=Kategorier")
        page.click("text=+ Ny kategori")
        fill_modal(page, ["Konsult", "income", "3001"])
        page.wait_for_selector("text=Konsult", timeout=15000)
        browser.close()

    httpd.shutdown()
    assert not errors, f"JS page errors: {errors}"
    print(f"PHONE BUNDLE OK in-browser (WASM backend booted in {boot}s, no server): "
          "book + encrypted customer + category persisted")


if __name__ == "__main__":
    main()
