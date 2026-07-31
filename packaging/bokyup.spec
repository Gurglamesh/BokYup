# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the BokYup desktop app.
#
#   pip install pyinstaller
#   pyinstaller packaging/bokyup.spec        # run from the repo root
#
# Produces dist/BokYup (one-folder) — a desktop build that needs no Python install.
# Build on each target OS (PyInstaller does not cross-compile): Windows -> .exe,
# macOS -> .app, Linux -> ELF. pywebview uses the OS web runtime (WebView2 / WKWebView
# / WebKitGTK), so ensure that runtime is present on the target.

from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("backend/api/static", "backend/api/static"),   # the web UI, served at /app
]

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + ["webview", "argon2", "cryptography"]
)

a = Analysis(
    ["packaging/desktop_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="BokYup",
    console=False,            # GUI app: no console window
    icon="packaging/icon/bokyup.ico",   # taskbar / window / exe icon
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="BokYup",
)
