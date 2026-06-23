"""Layer 7 — FastAPI HTTP layer over the backend (see backend.api.app.create_app)."""

from backend.api.app import create_app

__all__ = ["create_app"]
