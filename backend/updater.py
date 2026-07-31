"""
updater.py — in-app update check against GitHub Releases.

The app (desktop .exe, or Android .apk via Obtainium) is distributed through GitHub
Releases. This module checks whether a newer release exists and, on the frozen desktop
build, hands off to an EXTERNAL updater (packaging/updater.py -> BokYupUpdater.exe) that
swaps the installation once the running app has exited (a running .exe can't overwrite
itself on Windows).

Design:
  * `evaluate_release()` and `is_newer()` are PURE (no network) so they're unit-tested.
  * `fetch_latest_release()` does the actual HTTPS GET (stdlib urllib — no new deps).
  * `apply_update()` only does anything in the frozen desktop build; otherwise it returns
    a friendly "not supported in this mode" so the web/phone UI can just link the release.

User books live OUTSIDE the app bundle (~/.buyn / BUYN_DATA_DIR), so replacing the
program never touches the encrypted 7-year archive. schema.migrate() upgrades a book on
open, so a version bump across an update is handled.
"""

from __future__ import annotations

import os
import sys

# owner/repo the releases are published to (override for forks / testing).
GITHUB_REPO = os.environ.get("BOKYUP_UPDATE_REPO", "gurglamesh/bokyup")


def current_version() -> str:
    from backend import __version__
    return __version__


def parse_version(s: str) -> tuple:
    """Lenient numeric version tuple: 'v1.2.3' -> (1, 2, 3); missing/garbage -> (0,)."""
    s = (s or "").strip().lstrip("vV")
    out = []
    for part in s.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) if out else (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def evaluate_release(release_json: dict, current: str) -> dict:
    """Turn a GitHub 'releases/latest' payload into an update-info dict (pure)."""
    tag = (release_json.get("tag_name") or release_json.get("name") or "")
    assets = release_json.get("assets") or []

    def asset(*suffixes):
        for a in assets:
            name = (a.get("name") or "").lower()
            if name.endswith(suffixes):
                return a.get("browser_download_url"), a.get("name")
        return None, None

    exe_url, exe_name = asset(".zip", ".exe")   # Windows one-folder zip (preferred) / exe
    apk_url, apk_name = asset(".apk")
    return {
        "current": current,
        "latest": tag.lstrip("vV"),
        "update_available": is_newer(tag, current),
        "notes": release_json.get("body") or "",
        "html_url": release_json.get("html_url"),
        "published_at": release_json.get("published_at"),
        "exe_url": exe_url, "exe_name": exe_name,
        "apk_url": apk_url, "apk_name": apk_name,
    }


def fetch_latest_release(repo: str = GITHUB_REPO, timeout: float = 8.0) -> dict:
    import json
    import urllib.request
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "BokYup-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_for_update(repo: str = GITHUB_REPO, current: str | None = None) -> dict:
    """Fetch the latest release and evaluate it. Never raises — network errors come back
    as {'error': ...} so the UI can show a soft message."""
    cur = current or current_version()
    try:
        release = fetch_latest_release(repo)
    except Exception as exc:                       # offline / rate-limited / no releases
        return {"current": cur, "update_available": False, "error": str(exc)}
    info = evaluate_release(release, cur)
    info["frozen"] = is_frozen()                   # can we self-update in-app?
    return info


def is_frozen() -> bool:
    """True inside the PyInstaller desktop build (where in-app self-update is possible)."""
    return bool(getattr(sys, "frozen", False))


def apply_update(info: dict) -> dict:
    """Desktop only: download the Windows build and launch the external updater, then the
    app must exit so the updater can swap the files. Returns a status dict."""
    import subprocess
    import tempfile
    import urllib.request

    if not is_frozen():
        return {"applied": False,
                "reason": "In-app-uppdatering stöds bara i den installerade desktop-appen. "
                          "Ladda ner den senaste versionen från releasesidan."}
    url = info.get("exe_url")
    if not url:
        return {"applied": False, "reason": "Ingen Windows-nedladdning i releasen."}

    target_exe = sys.executable                                    # running BokYup.exe
    install_dir = os.path.dirname(target_exe)
    bundled_updater = os.path.join(install_dir, _updater_name())
    if not os.path.exists(bundled_updater):
        return {"applied": False, "reason": "Uppdateraren saknas i installationen."}

    # Download the new build (zip of the one-folder install, or a single exe).
    suffix = ".zip" if url.lower().endswith(".zip") else ".exe"
    tmp_source = os.path.join(tempfile.gettempdir(), "BokYup-update" + suffix)
    urllib.request.urlretrieve(url, tmp_source)

    # Run a COPY of the updater from temp so extraction can overwrite the install dir
    # (the in-place updater.exe would be file-locked while running).
    import shutil
    tmp_updater = os.path.join(tempfile.gettempdir(), _updater_name())
    shutil.copy2(bundled_updater, tmp_updater)

    args = [tmp_updater, "--pid", str(os.getpid()), "--source", tmp_source,
            "--dir", install_dir, "--exe", target_exe]
    kwargs = {"close_fds": True}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so it outlives us.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    subprocess.Popen(args, **kwargs)
    return {"applied": True, "restarting": True}


def _updater_name() -> str:
    return "BokYupUpdater.exe" if os.name == "nt" else "BokYupUpdater"
