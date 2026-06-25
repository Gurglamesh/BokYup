# BokYup phone app (Capacitor)

The phone app is the **same web frontend** as the desktop, plus the **same Python
backend running in-process as WebAssembly** (Pyodide). It is **fully local** — no
server, no network — and a book exported on a PC (`.buyn`) imports here and vice-versa
(proven in `tools/wasm-smoke/`). This directory wraps that web bundle with Capacitor
for Android/iOS.

> Building a device app needs the platform SDKs (Android Studio / Xcode) and cannot be
> done in a headless CI container. Everything here is ready to build on a dev machine.

## How it fits together

```
backend/api/static/*        the web UI (shared with desktop)
  + pyodide-boot.js         boots Pyodide, loads the backend, exposes the in-process API
backend/api/phone.py        the Python side the boot script calls (PhoneApp.call)
phone/www/vendor/           Pyodide runtime + crypto wasm wheels + backend_src.zip
```

`app.js` auto-detects the in-process backend (`window.__BOKYUP_NATIVE__`) and uses it
instead of `fetch`; on desktop that global is absent and it talks to the FastAPI server.

## Build steps

```bash
# 1. Assemble the WebView bundle (copies the UI, injects the Pyodide boot scripts,
#    builds backend_src.zip):
python phone/build_www.py

# 2. Add the prebuilt Pyodide assets into phone/www/vendor/ (large binaries, not in git):
#      pyodide.js, pyodide.asm.js, pyodide.asm.wasm, python_stdlib.zip, pyodide-lock.json
#    and the matching cp3xx wasm32 wheels:
#      cryptography, argon2-cffi, argon2-cffi-bindings, cffi, pycparser, six
#    These come from the Pyodide release bundle (pyodide-<ver>.tar.bz2) — the SAME
#    versions tools/build_phone_assets.py reports. CRITICAL: the crypto must run with
#    Argon2 parallelism=1 (already pinned in crypto.py) or WASM throws "Threading
#    failure" and PC books won't open here.

# 3. Capacitor:
cd phone
npm install
npx cap add android      # and/or: npx cap add ios
npx cap sync
npx cap open android     # build/run in Android Studio (or `cap open ios` in Xcode)
```

## Native integration status

- **Camera receipt capture** — already works: the receipt picker uses `getUserMedia`
  and `<input type=file capture>`, which a WebView supports. `@capacitor/camera` is
  listed if you prefer the native picker.
- **Backup/restore file in/out** — implemented in `native-bridge.js` (included only in
  the phone build, injected by `build_www.py`). It provides `window.__BOKYUP_FILES__`:
  export shares the `.buyn` bytes Pyodide wrote via `@capacitor/filesystem` +
  `@capacitor/share`; restore reads a picked file into the Pyodide FS, then the shared
  UI calls `/books/import`. The backend/API are identical across platforms — only this
  byte-bridge is phone-specific. **Exercise on a device** to confirm the plugin calls
  (the desktop filesystem-path path is already tested).

## Persistence

Books (encrypted SQLite + photos) live in the WebView's IndexedDB (Pyodide IDBFS),
synced after every write. Auto-lock and DEK-in-memory behave exactly as on desktop.
