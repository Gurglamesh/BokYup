"""
generate.py — regenerate every icon asset from the two source SVGs.

Sources (edit these, then re-run):
    packaging/icon/bokyup-icon.svg          the full app icon (rounded bg + ledger + tick)
    packaging/icon/bokyup-foreground.svg    ledger + tick only, padded for the Android
                                            adaptive-icon safe zone (transparent bg)

Outputs (committed, so builds don't rasterize in CI):
    packaging/icon/bokyup.ico               Windows exe icon (multi-size)
    backend/api/static/icons/*              web favicon / PWA icons
    phone/assets/icon-only|foreground|background.png   @capacitor/assets inputs (Android)

Rasterises with Playwright/Chromium (already used by the test tooling). Run from the repo
root:  python packaging/icon/generate.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ICON = ROOT / "packaging" / "icon"
WEB = ROOT / "backend" / "api" / "static" / "icons"
PHONE = ROOT / "phone" / "assets"

FULL_SVG = ICON / "bokyup-icon.svg"
FG_SVG = ICON / "bokyup-foreground.svg"
BG_RGBA = (169, 211, 183, 255)                      # #A9D3B7 adaptive background


def _chromium_path() -> str | None:
    import glob
    for p in glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"):
        return p
    return None


def _render(browser, svg_text: str, size: int, out: Path) -> None:
    sized = re.sub(r'width="512" height="512"', f'width="{size}" height="{size}"', svg_text, count=1)
    pg = browser.new_page(viewport={"width": size, "height": size})
    pg.set_content(f'<body style="margin:0;padding:0">{sized}</body>')
    pg.wait_for_timeout(80)
    pg.screenshot(path=str(out), omit_background=True,
                  clip={"x": 0, "y": 0, "width": size, "height": size})
    pg.close()


def main() -> None:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    for d in (WEB, PHONE, ICON):
        d.mkdir(parents=True, exist_ok=True)
    full = FULL_SVG.read_text()
    fg = FG_SVG.read_text()
    tmp = ICON / "_out"
    tmp.mkdir(exist_ok=True)
    exe = _chromium_path()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        for s in (16, 32, 48, 64, 128, 180, 192, 256, 512, 1024):
            _render(browser, full, s, tmp / f"icon-{s}.png")
        _render(browser, fg, 1024, tmp / "icon-foreground.png")
        browser.close()

    # Windows .ico (multi-size)
    Image.open(tmp / "icon-256.png").save(
        ICON / "bokyup.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    # Web assets
    (WEB / "favicon.svg").write_text(full)
    for src, dst in [("icon-32.png", "favicon-32.png"), ("icon-180.png", "apple-touch-icon.png"),
                     ("icon-192.png", "icon-192.png"), ("icon-512.png", "icon-512.png")]:
        (WEB / dst).write_bytes((tmp / src).read_bytes())

    # Android (@capacitor/assets inputs)
    (PHONE / "icon-only.png").write_bytes((tmp / "icon-1024.png").read_bytes())
    (PHONE / "icon-foreground.png").write_bytes((tmp / "icon-foreground.png").read_bytes())
    Image.new("RGBA", (1024, 1024), BG_RGBA).save(PHONE / "icon-background.png")

    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print("Icon assets regenerated from the SVG sources.")


if __name__ == "__main__":
    main()
