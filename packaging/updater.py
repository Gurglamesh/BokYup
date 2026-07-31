"""
updater.py — the EXTERNAL updater, shipped alongside the desktop app as
`BokYupUpdater.exe` (a small console build). The running app can't overwrite its own
files on Windows, so when the user approves an update the app:

  1. downloads the new build (a zip of the one-folder install, or a single .exe),
  2. launches a COPY of this updater from %TEMP% (so the install dir is not file-locked),
  3. exits.

This updater then waits for the app to exit, backs up the current install, swaps in the
new files, and relaunches the app. User books live outside the install dir, so they are
never touched.

    BokYupUpdater --pid <PID> --source <zip|exe> --dir <install_dir> --exe <exe_path>
                  [--sha256 <hex>]

Pure helpers (`verify_sha256`, `swap_zip`, `swap_file`) are unit-tested; the wait +
relaunch are OS-specific and run only as the frozen exe.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time
import zipfile


def verify_sha256(path: str, expected: str | None) -> bool:
    if not expected:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected.strip().lower()


def _backup_dir(install_dir: str) -> str:
    bak = install_dir.rstrip("/\\") + ".bak"
    if os.path.exists(bak):
        shutil.rmtree(bak, ignore_errors=True)
    shutil.copytree(install_dir, bak)
    return bak


def swap_zip(source_zip: str, install_dir: str) -> None:
    """Extract the update zip over the install dir. The zip may contain the files at its
    root or nested under a single top folder (e.g. 'BokYup/…'); both are handled. The
    previous install is copied to <dir>.bak first."""
    _backup_dir(install_dir)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(source_zip) as zf:
            zf.extractall(tmp)
        # If everything sits under one top-level folder, descend into it.
        entries = [e for e in os.listdir(tmp) if not e.startswith(".")]
        root = tmp
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
            root = os.path.join(tmp, entries[0])
        for name in os.listdir(root):
            src = os.path.join(root, name)
            dst = os.path.join(install_dir, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)


def swap_file(source_exe: str, target_exe: str) -> None:
    """Replace a single-file (onefile) exe, keeping the old one as <exe>.bak."""
    bak = target_exe + ".bak"
    if os.path.exists(bak):
        try:
            os.remove(bak)
        except OSError:
            pass
    if os.path.exists(target_exe):
        os.replace(target_exe, bak)
    shutil.copy2(source_exe, target_exe)


def _wait_for_exit(pid: int, timeout: float = 60.0) -> None:
    """Block until the given PID is gone (best-effort, cross-platform)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.4)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(h)
        return exit_code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _relaunch(exe_path: str) -> None:
    import subprocess
    kwargs = {"close_fds": True, "cwd": os.path.dirname(exe_path)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
    subprocess.Popen([exe_path], **kwargs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="BokYupUpdater")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--source", required=True, help="new build (.zip one-folder, or .exe)")
    ap.add_argument("--dir", dest="install_dir", help="install directory (for a zip)")
    ap.add_argument("--exe", dest="exe_path", required=True, help="exe to replace/relaunch")
    ap.add_argument("--sha256", default=None)
    args = ap.parse_args(argv)

    _wait_for_exit(args.pid)
    if not verify_sha256(args.source, args.sha256):
        print("Checksum mismatch — aborting update.", file=sys.stderr)
        return 2
    try:
        if args.source.lower().endswith(".zip"):
            if not args.install_dir:
                print("--dir required for a zip update.", file=sys.stderr)
                return 2
            swap_zip(args.source, args.install_dir)
        else:
            swap_file(args.source, args.exe_path)
    except Exception as exc:                                   # noqa: BLE001
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    _relaunch(args.exe_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
