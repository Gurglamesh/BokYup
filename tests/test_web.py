"""
Tests for Layer 8: the web frontend wiring.

The UI itself is vanilla JS (verified separately with `node --check`); these tests
confirm the FastAPI server serves the static assets at /app and that the desktop
launcher module is importable and wired correctly without requiring a display.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api import create_app


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(app_dir=tmp_path / "app")) as c:
        yield c


class TestStaticUI:
    def test_index_served(self, client):
        resp = client.get("/app/")
        assert resp.status_code == 200
        assert "<title>BokYup</title>" in resp.text

    def test_app_js_served(self, client):
        resp = client.get("/app/app.js")
        assert resp.status_code == 200
        assert "BokYup web frontend" in resp.text

    def test_app_js_has_receipt_capture(self, client):
        # Receipt capture + multi-rate lines editor are wired in the frontend.
        js = client.get("/app/app.js").text
        for marker in ("momsLinesEditor", "receiptPicker", "cameraCaptureModal", "receiptsFlow"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_has_stock_lager(self, client):
        # Inventory (Lager) tab + invoice batch picker + margin are wired in the frontend.
        js = client.get("/app/app.js").text
        for marker in ("addStockBatchFlow", "stockBatchesTable", "refreshBatches",
                       "getMargin", "Lagerbatch"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_has_inkop_items_and_prefix(self, client):
        # Inköp line-items (create articles + batches), category prefix picker and the
        # article category filter are wired in the frontend.
        js = client.get("/app/app.js").text
        for marker in ("purchaseItemsEditor", "next-prefix", "articlesCat",
                       "full_batch_id", "Produktkategori"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_has_inline_category_create(self, client):
        # Inline "create category" (stacking dialog) is wired into the invoice line editor
        # and the article-create form.
        js = client.get("/app/app.js").text
        for marker in ("newCategoryDialog", "wireCategoryCreateSelect",
                       "handleModalCatCreate", "Ny kategori"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_has_subcategories_and_dropdowns(self, client):
        # Subcategory tree + path labels, BAS-konto datalist, receipt accept-all/PDF.
        js = client.get("/app/app.js").text
        for marker in ("orderCategoryTree", "categoryPath", "descendantIdSet",
                       "accountOptions", "+ Underkat.", "Endast bild eller PDF"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_inkop_requires_supplier_and_format(self, client):
        # Inköp requires a supplier and an explicit receipt originalformat.
        js = client.get("/app/app.js").text
        for marker in ("requireFormat", "Välj leverantör",
                       "Välj kvittots originalformat", "getFormat"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_inkop_ores_and_edit(self, client):
        # Inköp öresavrundning toggle + edit-inköp (non-ledger fields) + delete customer.
        js = client.get("/app/app.js").text
        for marker in ("ores_rounding", "öresavrundar", "editPurchaseFlow",
                       "deleteCustomerFlow"):
            assert marker in js, f"missing {marker} in app.js"

    def test_app_js_inkop_drafts(self, client):
        # Inköp drafts: save/continue wired in the frontend.
        js = client.get("/app/app.js").text
        for marker in ("expense-drafts", "Spara utkast", "saveDraft"):
            assert marker in js, f"missing {marker} in app.js"

    def test_styles_served(self, client):
        resp = client.get("/app/styles.css")
        assert resp.status_code == 200
        assert "--brand" in resp.text

    def test_api_still_at_root(self, client):
        # Mounting the UI at /app must not shadow the API routes.
        assert client.get("/").json()["name"] == "BokYup API"
        assert client.get("/books").status_code == 200

    def test_ui_can_be_disabled(self, tmp_path):
        with TestClient(create_app(app_dir=tmp_path / "a2", serve_ui=False)) as c:
            assert c.get("/app/").status_code == 404


class TestDesktopLauncher:
    def test_module_imports_without_pywebview(self):
        # pywebview is imported lazily inside main(), so the module must import fine.
        import backend.desktop as desktop
        assert desktop.APP_PATH == "/app/"

    def test_find_free_port(self):
        import backend.desktop as desktop
        port = desktop._find_free_port()
        assert isinstance(port, int) and port > 0
