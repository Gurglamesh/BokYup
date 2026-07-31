# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the small EXTERNAL updater shipped next to the desktop app.
#
#   pip install pyinstaller
#   pyinstaller packaging/updater.spec       # run from the repo root
#
# Produces a single-file console exe: dist/BokYupUpdater(.exe). CI copies it into the
# BokYup one-folder build so `apply_update()` can run a temp copy of it to swap the
# install after the app exits. It has no third-party deps (stdlib only).

a = Analysis(
    ["packaging/updater.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["pytest", "tkinter", "fastapi", "uvicorn", "webview"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="BokYupUpdater",
    console=True,             # a tiny console updater
    onefile=True,
    disable_windowed_traceback=False,
)
