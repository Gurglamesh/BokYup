"""Tests for the in-app update check (backend/updater.py) and the external updater's
pure swap/verify helpers (packaging/updater.py). Network + OS-specific parts are not
exercised here."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

from backend import updater


# ---- version comparison ----------------------------------------------------
@pytest.mark.parametrize("s,expected", [
    ("v1.2.3", (1, 2, 3)), ("0.1.0", (0, 1, 0)), ("2", (2,)), ("", (0,)), ("v1.0-rc1", (1, 0)),
])
def test_parse_version(s, expected):
    assert updater.parse_version(s) == expected


def test_is_newer():
    assert updater.is_newer("v0.2.0", "0.1.0") is True
    assert updater.is_newer("0.1.0", "0.1.0") is False
    assert updater.is_newer("0.1.0", "0.2.0") is False


# ---- release evaluation (pure) ---------------------------------------------
def _release():
    return {"tag_name": "v0.2.0", "body": "Nyheter", "html_url": "http://x/rel",
            "published_at": "2026-07-31T00:00:00Z",
            "assets": [
                {"name": "BokYup-windows-0.2.0.zip", "browser_download_url": "http://z/win.zip"},
                {"name": "bokyup-0.2.0.apk", "browser_download_url": "http://z/app.apk"},
            ]}


def test_evaluate_release_update_available():
    info = updater.evaluate_release(_release(), "0.1.0")
    assert info["update_available"] is True
    assert info["latest"] == "0.2.0"
    assert info["exe_url"].endswith("win.zip")
    assert info["apk_url"].endswith(".apk")
    assert info["html_url"] == "http://x/rel"


def test_evaluate_release_same_version_no_update():
    assert updater.evaluate_release(_release(), "0.2.0")["update_available"] is False


def test_check_for_update_offline_is_soft(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(updater, "fetch_latest_release", boom)
    info = updater.check_for_update(current="0.1.0")
    assert info["update_available"] is False and "error" in info


def test_check_for_update_no_releases_is_distinct_from_offline(monkeypatch):
    import urllib.error
    def not_found(*a, **k):
        raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    monkeypatch.setattr(updater, "fetch_latest_release", not_found)
    info = updater.check_for_update(current="0.1.0")
    # A repo with no published release must NOT look like an offline error.
    assert info["update_available"] is False
    assert info.get("no_releases") is True and "error" not in info


def test_check_for_update_success(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest_release", lambda *a, **k: _release())
    info = updater.check_for_update(current="0.1.0")
    assert info["update_available"] is True and info["latest"] == "0.2.0"
    assert "frozen" in info


def test_apply_update_not_frozen_is_friendly():
    res = updater.apply_update({"exe_url": "http://z/win.zip"})
    assert res["applied"] is False and "desktop" in res["reason"].lower()


# ---- external updater swap/verify helpers ----------------------------------
def _load_pkg_updater():
    spec = importlib.util.spec_from_file_location(
        "pkg_updater", Path(__file__).resolve().parents[1] / "packaging" / "updater.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_verify_sha256(tmp_path):
    pu = _load_pkg_updater()
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    assert pu.verify_sha256(str(f), digest) is True
    assert pu.verify_sha256(str(f), "deadbeef") is False
    assert pu.verify_sha256(str(f), None) is True          # no expected -> skip


def test_swap_zip_replaces_install_and_backs_up(tmp_path):
    pu = _load_pkg_updater()
    install = tmp_path / "BokYup"
    install.mkdir()
    (install / "BokYup.exe").write_text("OLD")
    (install / "keep.txt").write_text("data")
    zpath = tmp_path / "upd.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("BokYup/BokYup.exe", "NEW")            # nested under one top folder
        z.writestr("BokYup/_internal/lib.dat", "x")
    pu.swap_zip(str(zpath), str(install))
    assert (install / "BokYup.exe").read_text() == "NEW"
    assert (install / "_internal" / "lib.dat").exists()
    assert Path(str(install) + ".bak").exists()            # backup of the old install
    assert (Path(str(install) + ".bak") / "BokYup.exe").read_text() == "OLD"


def test_swap_file_keeps_backup(tmp_path):
    pu = _load_pkg_updater()
    target = tmp_path / "BokYup.exe"
    target.write_text("OLD")
    new = tmp_path / "new.exe"
    new.write_text("NEW")
    pu.swap_file(str(new), str(target))
    assert target.read_text() == "NEW"
    assert (tmp_path / "BokYup.exe.bak").read_text() == "OLD"
