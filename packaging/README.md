# Packaging the BokYup desktop app

Builds a standalone desktop app (no Python install needed by the end user) from the
local FastAPI backend + pywebview window — the same thing `python -m backend.desktop`
runs today.

> Must be built on each target OS — PyInstaller does not cross-compile, and pywebview
> relies on the OS web runtime. Cannot be produced in a headless CI container.

## Build

```bash
pip install -r requirements.txt pyinstaller
pyinstaller packaging/bokyup.spec        # from the repo root
# -> dist/BokYup/  (run dist/BokYup/BokYup)
```

- **Windows**: produces `BokYup.exe`. Needs the Microsoft **WebView2** runtime
  (preinstalled on current Windows; otherwise ship the Evergreen bootstrapper).
- **macOS**: produces a `.app`; uses **WKWebView** (built in). Code-sign/notarize for
  distribution.
- **Linux**: needs **WebKitGTK** (`gir1.2-webkit2-4.0` or distro equivalent).

## Notes

- The web UI is bundled as data (`backend/api/static`); the backend serves it at `/app`
  on a random localhost port, opened in the native window.
- Books live in `~/.buyn` by default (override with `BUYN_DATA_DIR`) — they are NOT
  inside the app bundle, so updating/reinstalling the app never touches user data.
- An installer (MSI / DMG / AppImage) is a further step on top of the PyInstaller
  output (e.g. Inno Setup, create-dmg, appimagetool).
- Alternative: **Briefcase** (BeeWare) can produce native installers directly from the
  same entry point if preferred over PyInstaller + an installer tool.
