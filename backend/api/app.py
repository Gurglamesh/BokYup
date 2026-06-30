"""
app.py — FastAPI application (Layer 7 / desktop transport).

Exposes the whole backend over HTTP for the desktop web UI. The actual request→
operations logic lives in `backend/api/facade.py` (AppFacade); these routes only
validate the request body with Pydantic and delegate to the facade's handlers, so
the same logic also runs in-process on the phone (Pyodide) without duplication.

Security model (CLAUDE.md): there is NO app-level password. Each database is
unlocked individually with its own passphrase; the DEK then lives only in server
memory inside the DatabaseManager session. Locking wipes it. Auto-lock wipes idle
sessions after a configurable inactivity timeout (default 15 min).

This is a localhost, single-user desktop backend: db/bundle paths in requests are
trusted local filesystem paths. Do not expose this server on a network as-is.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from backend import __version__ as APP_VERSION
from backend.api import schemas as sc
from backend.api.facade import AppFacade, BookLocked, RawResult, DEFAULT_AUTOLOCK_SECONDS
from backend.core.crypto import BadPassphrase, CryptoError
from backend.db import bundle
from backend.db.manager import DatabaseManager
from backend.db.operations import InvalidState, PeriodLocked

DEFAULT_SWEEP_INTERVAL = 60


# ===========================================================================
# Application factory
# ===========================================================================

def create_app(app_dir: str | Path | None = None,
               autolock_seconds: int = DEFAULT_AUTOLOCK_SECONDS,
               sweep_interval: int = DEFAULT_SWEEP_INTERVAL,
               serve_ui: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_autolock_sweeper(app))
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="BokYup API", version=APP_VERSION, lifespan=lifespan)
    manager = DatabaseManager(Path(app_dir)) if app_dir else DatabaseManager()
    facade = AppFacade(manager, autolock_seconds)
    app.state.facade = facade
    app.state.manager = manager                  # kept for direct access / tests
    app.state.last_activity = facade.last_activity
    app.state.sweep_interval = sweep_interval

    _register_exception_handlers(app)
    app.include_router(_build_router())

    # Serve the web frontend at /app from this same server.
    if serve_ui:
        from fastapi.staticfiles import StaticFiles
        static_dir = Path(__file__).parent / "static"
        if static_dir.is_dir():
            app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="ui")
    return app


async def _autolock_sweeper(app: FastAPI) -> None:
    """Actively wipe idle sessions' DEKs (best-effort, alongside the on-access check)."""
    while True:
        await asyncio.sleep(app.state.sweep_interval)
        app.state.facade.sweep()


# ===========================================================================
# Exception handling — map domain errors to HTTP status codes
# ===========================================================================

def _register_exception_handlers(app: FastAPI) -> None:
    def handler(status: int):
        async def _h(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return _h

    app.add_exception_handler(BadPassphrase, handler(401))
    app.add_exception_handler(BookLocked, handler(423))       # locked / auto-locked
    app.add_exception_handler(CryptoError, handler(423))      # locked session
    app.add_exception_handler(PeriodLocked, handler(409))
    app.add_exception_handler(InvalidState, handler(409))
    app.add_exception_handler(FileExistsError, handler(409))
    app.add_exception_handler(FileNotFoundError, handler(404))
    app.add_exception_handler(KeyError, handler(404))
    app.add_exception_handler(ValueError, handler(400))
    app.add_exception_handler(bundle.BundleError, handler(400))


# ===========================================================================
# Routes — thin: validate body, then delegate to the facade handler.
# ===========================================================================

def _build_router():
    from fastapi import APIRouter

    r = APIRouter()

    def fac(request: Request) -> AppFacade:
        return request.app.state.facade

    # ---- meta ----
    @r.get("/")
    def root(request: Request):
        return fac(request).h_root({}, {}, {})

    # ---- books / registry ----
    @r.get("/books")
    def list_books(request: Request):
        return fac(request).h_list_books({}, {}, {})

    @r.post("/books", status_code=201)
    def create_book(body: sc.CreateBookReq, request: Request):
        return fac(request).h_create_book({}, body.model_dump(), {})

    @r.post("/books/import", status_code=201)
    def import_book(body: sc.ImportReq, request: Request):
        return fac(request).h_import_book({}, body.model_dump(), {})

    @r.post("/books/{book_id}/unlock")
    def unlock(book_id: str, body: sc.UnlockReq, request: Request):
        return fac(request).h_unlock({"book_id": book_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/unlock-recovery")
    def unlock_recovery(book_id: str, body: sc.RecoveryUnlockReq, request: Request):
        return fac(request).h_unlock_recovery({"book_id": book_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/lock")
    def lock(book_id: str, request: Request):
        return fac(request).h_lock({"book_id": book_id}, {}, {})

    @r.patch("/books/{book_id}")
    def rename(book_id: str, body: sc.RenameReq, request: Request):
        return fac(request).h_rename({"book_id": book_id}, body.model_dump(), {})

    @r.delete("/books/{book_id}")
    def remove(book_id: str, request: Request, delete_files: bool = False):
        return fac(request).h_remove(
            {"book_id": book_id}, {}, {"delete_files": delete_files})

    @r.post("/books/{book_id}/export")
    def export_book(book_id: str, body: sc.ExportReq, request: Request):
        return fac(request).h_export_book({"book_id": book_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/change-passphrase")
    def change_passphrase(book_id: str, body: sc.ChangePassphraseReq, request: Request):
        return fac(request).h_change_passphrase({"book_id": book_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/recovery-key")
    def recovery_key_status(book_id: str, request: Request):
        return fac(request).h_recovery_key_status({"book_id": book_id}, {}, {})

    @r.post("/books/{book_id}/recovery-key", status_code=201)
    def add_recovery_key(book_id: str, body: sc.RecoveryKeyReq, request: Request):
        return fac(request).h_add_recovery_key({"book_id": book_id}, body.model_dump(), {})

    # ---- reference: categories ----
    @r.get("/books/{book_id}/categories")
    def list_categories(book_id: str, request: Request):
        return fac(request).h_list_categories({"book_id": book_id}, {}, {})

    @r.get("/books/{book_id}/accounts")
    def list_accounts(book_id: str, request: Request):
        return fac(request).h_list_accounts({"book_id": book_id}, {}, {})

    # ---- article catalog ----
    @r.get("/books/{book_id}/articles")
    def list_articles(book_id: str, request: Request):
        return fac(request).h_list_articles({"book_id": book_id}, {}, {})

    @r.post("/books/{book_id}/articles", status_code=201)
    def create_article(book_id: str, body: sc.ArticleReq, request: Request):
        return fac(request).h_create_article({"book_id": book_id}, body.model_dump(), {})

    @r.patch("/books/{book_id}/articles/{article_id}")
    def update_article(book_id: str, article_id: int, body: sc.ArticleUpdateReq, request: Request):
        return fac(request).h_update_article(
            {"book_id": book_id, "article_id": article_id}, body.model_dump(), {})

    @r.delete("/books/{book_id}/articles/{article_id}")
    def delete_article(book_id: str, article_id: int, request: Request):
        return fac(request).h_delete_article({"book_id": book_id, "article_id": article_id}, {}, {})

    @r.post("/books/{book_id}/categories", status_code=201)
    def create_category(book_id: str, body: sc.CategoryReq, request: Request):
        return fac(request).h_create_category({"book_id": book_id}, body.model_dump(), {})

    @r.patch("/books/{book_id}/categories/{category_id}")
    def update_category(book_id: str, category_id: int, body: sc.CategoryUpdateReq, request: Request):
        return fac(request).h_update_category(
            {"book_id": book_id, "category_id": category_id}, body.model_dump(), {})

    @r.delete("/books/{book_id}/categories/{category_id}")
    def delete_category(book_id: str, category_id: int, request: Request):
        return fac(request).h_delete_category(
            {"book_id": book_id, "category_id": category_id}, {}, {})

    # ---- reference: customers ----
    @r.get("/books/{book_id}/customers")
    def list_customers(book_id: str, request: Request):
        return fac(request).h_list_customers({"book_id": book_id}, {}, {})

    @r.get("/books/{book_id}/customers/{kundnummer}")
    def get_customer(book_id: str, kundnummer: int, request: Request):
        return fac(request).h_get_customer({"book_id": book_id, "kundnummer": kundnummer}, {}, {})

    @r.post("/books/{book_id}/customers", status_code=201)
    def create_customer(book_id: str, body: sc.CustomerReq, request: Request):
        return fac(request).h_create_customer({"book_id": book_id}, body.model_dump(), {})

    @r.patch("/books/{book_id}/customers/{kundnummer}")
    def update_customer(book_id: str, kundnummer: int, body: sc.CustomerUpdateReq, request: Request):
        return fac(request).h_update_customer(
            {"book_id": book_id, "kundnummer": kundnummer}, body.model_dump(), {})

    # ---- customer household relations (RUT/ROT) ----
    @r.get("/books/{book_id}/customers/{kundnummer}/relations")
    def list_customer_relations(book_id: str, kundnummer: int, request: Request):
        return fac(request).h_list_customer_relations(
            {"book_id": book_id, "kundnummer": kundnummer}, {}, {})

    @r.post("/books/{book_id}/customers/{kundnummer}/relations", status_code=201)
    def link_customer(book_id: str, kundnummer: int, body: sc.CustomerRelationReq, request: Request):
        return fac(request).h_link_customer(
            {"book_id": book_id, "kundnummer": kundnummer}, body.model_dump(), {})

    @r.delete("/books/{book_id}/customers/{kundnummer}/relations/{other_kundnummer}")
    def unlink_customer(book_id: str, kundnummer: int, other_kundnummer: int, request: Request):
        return fac(request).h_unlink_customer(
            {"book_id": book_id, "kundnummer": kundnummer, "other_kundnummer": other_kundnummer}, {}, {})

    @r.get("/books/{book_id}/reduction-config")
    def reduction_config(book_id: str, request: Request):
        return fac(request).h_reduction_config({"book_id": book_id}, {}, {})

    @r.get("/books/{book_id}/customers/{kundnummer}/husavdrag-cap/{year}")
    def husavdrag_cap(book_id: str, kundnummer: int, year: int, request: Request):
        return fac(request).h_husavdrag_cap(
            {"book_id": book_id, "kundnummer": kundnummer, "year": year}, {}, {})

    # ---- reference: suppliers ----
    @r.get("/books/{book_id}/suppliers")
    def list_suppliers(book_id: str, request: Request):
        return fac(request).h_list_suppliers({"book_id": book_id}, {}, {})

    @r.post("/books/{book_id}/suppliers", status_code=201)
    def create_supplier(book_id: str, body: sc.SupplierReq, request: Request):
        return fac(request).h_create_supplier({"book_id": book_id}, body.model_dump(), {})

    @r.patch("/books/{book_id}/suppliers/{supplier_id}")
    def update_supplier(book_id: str, supplier_id: int, body: sc.SupplierUpdateReq, request: Request):
        return fac(request).h_update_supplier(
            {"book_id": book_id, "supplier_id": supplier_id}, body.model_dump(), {})

    # ---- bookkeeping ----
    @r.post("/books/{book_id}/expenses", status_code=201)
    def record_expense(book_id: str, body: sc.RecordExpenseReq, request: Request):
        return fac(request).h_record_expense({"book_id": book_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/incomes", status_code=201)
    def record_income(book_id: str, body: sc.RecordIncomeReq, request: Request):
        return fac(request).h_record_income({"book_id": book_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/transaktioner/{transaktion_id}/pay")
    def register_payment(book_id: str, transaktion_id: int, body: sc.PaymentReq, request: Request):
        return fac(request).h_register_payment(
            {"book_id": book_id, "transaktion_id": transaktion_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/rut/{rut_claim_id}/skatteverket-payment")
    def rut_skatteverket_payment(book_id: str, rut_claim_id: int, body: sc.PaymentReq, request: Request):
        return fac(request).h_rut_skatteverket_payment(
            {"book_id": book_id, "rut_claim_id": rut_claim_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/customers/{kundnummer}/rut-cap/{year}")
    def rut_cap(book_id: str, kundnummer: int, year: int, request: Request):
        return fac(request).h_rut_cap(
            {"book_id": book_id, "kundnummer": kundnummer, "year": year}, {}, {})

    @r.get("/books/{book_id}/rut-claims")
    def list_rut_claims(book_id: str, request: Request):
        return fac(request).h_list_rut_claims({"book_id": book_id}, {}, {})

    @r.post("/books/{book_id}/verifikationer/{verifikation_id}/reverse", status_code=201)
    def reverse_verifikation(book_id: str, verifikation_id: int, body: sc.ReverseReq, request: Request):
        return fac(request).h_reverse_verifikation(
            {"book_id": book_id, "verifikation_id": verifikation_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/period-locks", status_code=201)
    def lock_period(book_id: str, body: sc.PeriodLockReq, request: Request):
        return fac(request).h_lock_period({"book_id": book_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/year-end-accruals", status_code=201)
    def year_end_accruals(book_id: str, body: sc.YearEndAccrualReq, request: Request):
        return fac(request).h_year_end_accruals({"book_id": book_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/verifikationer")
    def list_verifikationer(book_id: str, request: Request):
        return fac(request).h_list_verifikationer({"book_id": book_id}, {}, {})

    @r.get("/books/{book_id}/transaktioner")
    def list_transaktioner(book_id: str, request: Request, include_synthetic: bool = False):
        return fac(request).h_list_transaktioner(
            {"book_id": book_id}, {}, {"include_synthetic": "1" if include_synthetic else "0"})

    # ---- receipts (encrypted photos) ----
    @r.post("/books/{book_id}/transaktioner/{transaktion_id}/receipts", status_code=201)
    def upload_receipt(book_id: str, transaktion_id: int, body: sc.ReceiptUploadReq, request: Request):
        return fac(request).h_upload_receipt(
            {"book_id": book_id, "transaktion_id": transaktion_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/transaktioner/{transaktion_id}/receipts")
    def list_receipts(book_id: str, transaktion_id: int, request: Request):
        return fac(request).h_list_receipts(
            {"book_id": book_id, "transaktion_id": transaktion_id}, {}, {})

    @r.get("/books/{book_id}/receipts/{receipt_id}")
    def get_receipt(book_id: str, receipt_id: int, request: Request):
        res = fac(request).h_get_receipt({"book_id": book_id, "receipt_id": receipt_id}, {}, {})
        return Response(content=res.content, media_type=res.media_type)

    @r.delete("/books/{book_id}/receipts/{receipt_id}")
    def delete_receipt(book_id: str, receipt_id: int, request: Request):
        return fac(request).h_delete_receipt({"book_id": book_id, "receipt_id": receipt_id}, {}, {})

    # ---- reports ----
    @r.get("/books/{book_id}/reports/momsdeklaration")
    def report_moms(book_id: str, start: str, end: str, request: Request):
        return fac(request).h_report_moms({"book_id": book_id}, {}, {"start": start, "end": end})

    @r.get("/books/{book_id}/reports/result")
    def report_result(book_id: str, start: str, end: str, request: Request):
        return fac(request).h_report_result({"book_id": book_id}, {}, {"start": start, "end": end})

    @r.get("/books/{book_id}/reports/sie", response_class=PlainTextResponse)
    def report_sie(book_id: str, request: Request, company_name: str = "", org_nr: str = "",
                   fiscal_year_start: str = "", fiscal_year_end: str = ""):
        res = fac(request).h_report_sie({"book_id": book_id}, {}, {
            "company_name": company_name, "org_nr": org_nr,
            "fiscal_year_start": fiscal_year_start, "fiscal_year_end": fiscal_year_end})
        return PlainTextResponse(res.content)

    # ---- accounting method (per book) ----
    @r.get("/books/{book_id}/accounting-method")
    def get_accounting_method(book_id: str, request: Request):
        return fac(request).h_get_accounting_method({"book_id": book_id}, {}, {})

    @r.put("/books/{book_id}/accounting-method")
    def set_accounting_method(book_id: str, body: sc.AccountingMethodReq, request: Request):
        return fac(request).h_set_accounting_method({"book_id": book_id}, body.model_dump(), {})

    # ---- company profile (seller) ----
    @r.get("/books/{book_id}/company")
    def get_company(book_id: str, request: Request):
        return fac(request).h_get_company({"book_id": book_id}, {}, {})

    @r.put("/books/{book_id}/company")
    def set_company(book_id: str, body: sc.CompanyReq, request: Request):
        return fac(request).h_set_company({"book_id": book_id}, body.model_dump(), {})

    # ---- logo (used on every document) ----
    @r.get("/books/{book_id}/logo")
    def get_logo(book_id: str, request: Request):
        res = fac(request).h_get_logo({"book_id": book_id}, {}, {})
        return Response(content=res.content, media_type=res.media_type)

    @r.put("/books/{book_id}/logo")
    def set_logo(book_id: str, body: sc.LogoReq, request: Request):
        return fac(request).h_set_logo({"book_id": book_id}, body.model_dump(), {})

    @r.delete("/books/{book_id}/logo")
    def delete_logo(book_id: str, request: Request):
        return fac(request).h_delete_logo({"book_id": book_id}, {}, {})

    # ---- payment methods ----
    @r.get("/books/{book_id}/payment-methods")
    def list_payment_methods(book_id: str, request: Request):
        return fac(request).h_list_payment_methods({"book_id": book_id}, {}, {})

    @r.post("/books/{book_id}/payment-methods", status_code=201)
    def create_payment_method(book_id: str, body: sc.PaymentMethodReq, request: Request):
        return fac(request).h_create_payment_method({"book_id": book_id}, body.model_dump(), {})

    @r.patch("/books/{book_id}/payment-methods/{payment_method_id}")
    def update_payment_method(book_id: str, payment_method_id: int,
                              body: sc.PaymentMethodUpdateReq, request: Request):
        return fac(request).h_update_payment_method(
            {"book_id": book_id, "payment_method_id": payment_method_id}, body.model_dump(), {})

    # ---- invoices (faktura) ----
    @r.post("/books/{book_id}/invoices", status_code=201)
    def create_invoice(book_id: str, body: sc.CreateInvoiceReq, request: Request):
        return fac(request).h_create_invoice({"book_id": book_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/invoices")
    def list_invoices(book_id: str, request: Request):
        return fac(request).h_list_invoices({"book_id": book_id}, {}, {})

    # ---- invoice drafts ----
    @r.get("/books/{book_id}/invoice-drafts")
    def list_drafts(book_id: str, request: Request):
        return fac(request).h_list_drafts({"book_id": book_id}, {}, {})

    @r.post("/books/{book_id}/invoice-drafts", status_code=201)
    def create_draft(book_id: str, body: sc.InvoiceDraftReq, request: Request):
        return fac(request).h_create_draft({"book_id": book_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/invoice-drafts/{draft_id}")
    def get_draft(book_id: str, draft_id: int, request: Request):
        return fac(request).h_get_draft({"book_id": book_id, "draft_id": draft_id}, {}, {})

    @r.put("/books/{book_id}/invoice-drafts/{draft_id}")
    def update_draft(book_id: str, draft_id: int, body: sc.InvoiceDraftReq, request: Request):
        return fac(request).h_update_draft(
            {"book_id": book_id, "draft_id": draft_id}, body.model_dump(), {})

    @r.delete("/books/{book_id}/invoice-drafts/{draft_id}")
    def delete_draft(book_id: str, draft_id: int, request: Request):
        return fac(request).h_delete_draft({"book_id": book_id, "draft_id": draft_id}, {}, {})

    @r.get("/books/{book_id}/invoices/{invoice_id}")
    def get_invoice(book_id: str, invoice_id: int, request: Request):
        return fac(request).h_get_invoice({"book_id": book_id, "invoice_id": invoice_id}, {}, {})

    @r.post("/books/{book_id}/invoices/{invoice_id}/cancel", status_code=201)
    def cancel_invoice(book_id: str, invoice_id: int, request: Request):
        return fac(request).h_cancel_invoice({"book_id": book_id, "invoice_id": invoice_id}, {}, {})

    @r.post("/books/{book_id}/invoices/{invoice_id}/pay", status_code=201)
    def pay_invoice(book_id: str, invoice_id: int, body: sc.PayInvoiceReq, request: Request):
        return fac(request).h_pay_invoice(
            {"book_id": book_id, "invoice_id": invoice_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/invoices/{invoice_id}/refund", status_code=201)
    def refund_invoice(book_id: str, invoice_id: int, body: sc.RefundInvoiceReq, request: Request):
        return fac(request).h_refund_invoice(
            {"book_id": book_id, "invoice_id": invoice_id}, body.model_dump(), {})

    @r.post("/books/{book_id}/invoices/{invoice_id}/credit", status_code=201)
    def credit_invoice(book_id: str, invoice_id: int, body: sc.CreditInvoiceReq, request: Request):
        return fac(request).h_credit_invoice(
            {"book_id": book_id, "invoice_id": invoice_id}, body.model_dump(), {})

    @r.get("/books/{book_id}/invoices/{invoice_id}/pdf")
    def invoice_pdf(book_id: str, invoice_id: int, request: Request):
        res = fac(request).h_invoice_pdf({"book_id": book_id, "invoice_id": invoice_id}, {}, {})
        return Response(content=res.content, media_type=res.media_type)

    @r.get("/books/{book_id}/invoices/{invoice_id}/credit-notes/{event_id}/pdf")
    def credit_note_pdf(book_id: str, invoice_id: int, event_id: int, request: Request):
        res = fac(request).h_credit_note_pdf(
            {"book_id": book_id, "invoice_id": invoice_id, "event_id": event_id}, {}, {})
        return Response(content=res.content, media_type=res.media_type)

    return r


# Convenience for `python -m backend.api.app`
def run(host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    run()
