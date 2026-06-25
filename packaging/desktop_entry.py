"""
desktop_entry.py — PyInstaller entry point for the BokYup desktop app.

Launches the local FastAPI backend in a background thread and the web UI in a native
window (pywebview), i.e. the same thing as `python -m backend.desktop`, but as a frozen
single-file executable so end users don't need Python installed.
"""

from backend.desktop import main

if __name__ == "__main__":
    main()
