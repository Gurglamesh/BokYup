"""Layer 7 — HTTP/transport layer over the backend.

`create_app` (the FastAPI desktop server) is imported lazily so that importing
`backend.api.facade` on the phone — where FastAPI/uvicorn are NOT available
(the backend runs as WebAssembly via Pyodide) — does not pull FastAPI in.
"""

__all__ = ["create_app"]


def __getattr__(name):  # PEP 562: lazy attribute import
    if name == "create_app":
        from backend.api.app import create_app
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
