# Phone preview — try the phone app without an APK

The phone app is the BokYup web UI + the Python backend as WebAssembly (Pyodide),
running **in-page, fully local, no server**. That bundle (`phone/www`) is what
Capacitor wraps into an APK/IPA — but you can run the *identical* thing in any modern
browser, including your **phone's browser**, to try the real app immediately.

```bash
# 1. assemble phone/www and add the Pyodide runtime + wheels to phone/www/vendor
#    (one-time; see phone/README.md / tools/build_phone_assets.py)
# 2. serve it:
python tools/phone-preview/serve.py
#    -> open the printed http://<lan-ip>:8794/ on your phone's browser
```

The Android WebView is the same engine, so if it works here it works in the app.

## Automated check

```bash
pip install playwright && playwright install chromium
python tools/phone-preview/browser_smoke.py
```

Boots the WASM backend in headless Chromium and creates an encrypted book + customer
(encrypted personnummer) + category, failing on any uncaught JS error. Verified:
backend boots in ~3 s, no server, no page errors.

## Why there's no APK in this repo

Building the Android APK needs the Android SDK + the Android Gradle Plugin + AndroidX,
all served only from Google's repositories (`dl.google.com`, `maven.google.com`). In
some sandboxes those hosts are blocked by egress policy, so the APK must be built on a
machine with normal network access:

```bash
cd phone && npm install && npx cap add android && npx cap sync && npx cap open android
```

Until then, the browser preview above is the way to use the app on a phone.
