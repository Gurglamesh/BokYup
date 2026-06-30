"""
phone.py — in-process JSON boundary for the phone (Pyodide) transport.

On the phone there is no HTTP server: the web frontend runs in a WebView and the
Python backend runs as WebAssembly (Pyodide) in the same page. This module is the
single bridge the JavaScript shim calls. `PhoneApp.call(...)` takes the same
(method, path, body, query) the REST API uses — as JSON strings, the only thing
that crosses the JS<->Python boundary cleanly — runs it through the shared
AppFacade, and returns a JSON string:

    {"ok": true,  "status": 200, "result": <json>}
    {"ok": false, "status": 423, "detail": "Book is locked"}

Binary results (a receipt photo) and text results (the SIE file) come back as
{"raw": true, "base64"/"text": ..., "media_type": ...} so the frontend can build
a Blob exactly like it does from an HTTP response. Domain exceptions are mapped to
the same status codes the FastAPI desktop server uses, so the frontend's existing
error handling is unchanged across PC and phone.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from backend.api.facade import AppFacade, BookLocked, RawResult, DEFAULT_AUTOLOCK_SECONDS
from backend.core.crypto import BadPassphrase, CryptoError
from backend.db import bundle
from backend.db.operations import InvalidState, PeriodLocked

# Order matters: most specific first (BadPassphrase is a CryptoError).
_ERROR_STATUS: list[tuple[type, int]] = [
    (BadPassphrase, 401),
    (BookLocked, 423),
    (CryptoError, 423),
    (PeriodLocked, 409),
    (InvalidState, 409),
    (FileExistsError, 409),
    (bundle.BundleError, 400),
    (FileNotFoundError, 404),
    (KeyError, 404),
    (ValueError, 400),
]


def _status_for(exc: BaseException) -> int:
    for kind, status in _ERROR_STATUS:
        if isinstance(exc, kind):
            return status
    return 500


class PhoneApp:
    """The whole backend, in-process, behind one JSON call. Mirrors create_app()."""

    def __init__(self, data_dir: str | Path,
                 autolock_seconds: int = DEFAULT_AUTOLOCK_SECONDS):
        from backend.db.manager import DatabaseManager
        self.facade = AppFacade(DatabaseManager(Path(data_dir)), autolock_seconds)

    def call(self, method: str, path: str,
             body_json: str | None = None, query_json: str | None = None) -> str:
        body = json.loads(body_json) if body_json else None
        query = json.loads(query_json) if query_json else None
        try:
            status, result = self.facade.dispatch(method, path, body, query)
        except Exception as exc:  # noqa: BLE001 - boundary: turn every error into a response
            return json.dumps({"ok": False, "status": _status_for(exc), "detail": str(exc)})

        if isinstance(result, RawResult):
            if isinstance(result.content, bytes):
                result = {"raw": True, "media_type": result.media_type,
                          "base64": base64.b64encode(result.content).decode("ascii")}
            else:
                result = {"raw": True, "media_type": result.media_type,
                          "text": result.content}
        return json.dumps({"ok": True, "status": status, "result": result})
