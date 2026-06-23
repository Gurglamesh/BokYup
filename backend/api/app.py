"""
app.py — FastAPI application (Layer 7).

Exposes the whole backend over HTTP so the same logic serves the desktop web UI
today and phone clients later (CLAUDE.md > Architecture).

Security model (CLAUDE.md): there is NO app-level password. Each database is
unlocked individually with its own passphrase; the DEK then lives only in server
memory inside the DatabaseManager session. Locking wipes it. Auto-lock wipes idle
sessions after a configurable inactivity timeout (default 15 min).

This is a localhost, single-user desktop backend: db/bundle paths in requests are
trusted local filesystem paths. Do not expose this server on a network as-is.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from backend import __version__ as APP_VERSION
from backend.api import schemas as sc
from backend.core.crypto import BadPassphrase, CryptoError
from backend.db import bundle
from backend.db.manager import BookSession, DatabaseManager
from backend.db.operations import BookOps, InvalidState, PeriodLocked
from backend.models import schema as S
from backend.reports import result as result_report
from backend.reports import sie as sie_report
from backend.reports import vat as vat_report

DEFAULT_AUTOLOCK_SECONDS = 15 * 60
DEFAULT_SWEEP_INTERVAL = 60


# ===========================================================================
# Dependencies
# ===========================================================================

def _manager(request: Request) -> DatabaseManager:
    return request.app.state.manager


def _session(book_id: str, request: Request) -> BookSession:
    """Resolve an UNLOCKED session, enforcing auto-lock-on-access (423 if locked)."""
    mgr: DatabaseManager = request.app.state.manager
    activity: dict = request.app.state.last_activity
    autolock: int = request.app.state.autolock_seconds

    session = mgr.get_session(book_id)
    if session is None:
        raise HTTPException(status_code=423, detail="Book is locked")

    now = time.monotonic()
    last = activity.get(book_id)
    if autolock and last is not None and now - last > autolock:
        mgr.lock_book(book_id)
        activity.pop(book_id, None)
        raise HTTPException(status_code=423, detail="Book auto-locked due to inactivity")

    activity[book_id] = now
    return session


def _ops(session: BookSession = Depends(_session)) -> BookOps:
    return BookOps(session)


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

    app = FastAPI(title="Bokföring API", version=APP_VERSION, lifespan=lifespan)
    app.state.manager = DatabaseManager(Path(app_dir)) if app_dir else DatabaseManager()
    app.state.autolock_seconds = autolock_seconds
    app.state.sweep_interval = sweep_interval
    app.state.last_activity = {}

    _register_exception_handlers(app)
    app.include_router(_build_router())

    # Serve the web frontend (Layer 8) at /app from this same server.
    if serve_ui:
        from fastapi.staticfiles import StaticFiles
        static_dir = Path(__file__).parent / "static"
        if static_dir.is_dir():
            app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="ui")
    return app


async def _autolock_sweeper(app: FastAPI) -> None:
    """Actively wipe idle sessions' DEKs (best-effort, in addition to on-access check)."""
    while True:
        await asyncio.sleep(app.state.sweep_interval)
        now = time.monotonic()
        autolock = app.state.autolock_seconds
        if not autolock:
            continue
        for rec in app.state.manager.list_books():
            last = app.state.last_activity.get(rec.id)
            if last is not None and now - last > autolock:
                app.state.manager.lock_book(rec.id)
                app.state.last_activity.pop(rec.id, None)


# ===========================================================================
# Exception handling — map domain errors to HTTP status codes
# ===========================================================================

def _register_exception_handlers(app: FastAPI) -> None:
    from fastapi.responses import JSONResponse

    def handler(status: int):
        async def _h(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return _h

    app.add_exception_handler(BadPassphrase, handler(401))
    app.add_exception_handler(CryptoError, handler(423))      # locked session
    app.add_exception_handler(PeriodLocked, handler(409))
    app.add_exception_handler(InvalidState, handler(409))
    app.add_exception_handler(FileExistsError, handler(409))
    app.add_exception_handler(FileNotFoundError, handler(404))
    app.add_exception_handler(KeyError, handler(404))
    app.add_exception_handler(ValueError, handler(400))
    app.add_exception_handler(bundle.BundleError, handler(400))


# ===========================================================================
# Routes
# ===========================================================================

def _build_router():
    from fastapi import APIRouter

    r = APIRouter()

    # ---- meta ----
    @r.get("/")
    def root():
        return {"name": "Bokföring API", "version": APP_VERSION}

    # ---- books / registry ----
    @r.get("/books")
    def list_books(request: Request):
        return [b.to_dict() for b in _manager(request).list_books()]

    @r.post("/books", status_code=201)
    def create_book(body: sc.CreateBookReq, request: Request):
        mgr = _manager(request)
        record, session = mgr.create_book(body.display_name, body.db_path, body.passphrase)
        S.initialize_schema(session.connection())
        request.app.state.last_activity[record.id] = time.monotonic()
        return record.to_dict()

    @r.post("/books/{book_id}/unlock")
    def unlock(book_id: str, body: sc.UnlockReq, request: Request):
        _manager(request).open_book(book_id, body.passphrase)
        request.app.state.last_activity[book_id] = time.monotonic()
        return {"book_id": book_id, "unlocked": True}

    @r.post("/books/{book_id}/unlock-recovery")
    def unlock_recovery(book_id: str, body: sc.RecoveryUnlockReq, request: Request):
        _manager(request).open_book_with_recovery(book_id, body.recovery_key)
        request.app.state.last_activity[book_id] = time.monotonic()
        return {"book_id": book_id, "unlocked": True}

    @r.post("/books/{book_id}/lock")
    def lock(book_id: str, request: Request):
        _manager(request).lock_book(book_id)
        request.app.state.last_activity.pop(book_id, None)
        return {"book_id": book_id, "locked": True}

    @r.patch("/books/{book_id}")
    def rename(book_id: str, body: sc.RenameReq, request: Request):
        _manager(request).rename_book(book_id, body.display_name)
        return {"book_id": book_id, "display_name": body.display_name}

    @r.delete("/books/{book_id}")
    def remove(book_id: str, request: Request):
        _manager(request).remove_from_registry(book_id)
        return {"book_id": book_id, "removed": True}

    @r.post("/books/{book_id}/export")
    def export_book(book_id: str, body: sc.ExportReq, request: Request):
        out = _manager(request).export_book(book_id, body.out_path)
        return {"out_path": str(out)}

    @r.post("/books/import", status_code=201)
    def import_book(body: sc.ImportReq, request: Request):
        rec = _manager(request).import_book(
            body.bundle_path, body.dest_db_path,
            display_name=body.display_name, overwrite=body.overwrite,
        )
        return rec.to_dict()

    # ---- reference: categories ----
    @r.get("/books/{book_id}/categories")
    def list_categories(ops: BookOps = Depends(_ops)):
        return _rows(ops.conn, "SELECT id, name, kind, bas_konto, active FROM category ORDER BY bas_konto")

    @r.post("/books/{book_id}/categories", status_code=201)
    def create_category(body: sc.CategoryReq, ops: BookOps = Depends(_ops)):
        cid = ops.create_category(body.name, body.kind, body.bas_konto, body.account_name)
        return {"id": cid}

    @r.patch("/books/{book_id}/categories/{category_id}")
    def update_category(category_id: int, body: sc.CategoryUpdateReq, ops: BookOps = Depends(_ops)):
        ops.update_category(category_id, **body.model_dump(exclude_none=True))
        return {"id": category_id}

    # ---- reference: customers ----
    @r.get("/books/{book_id}/customers")
    def list_customers(ops: BookOps = Depends(_ops)):
        return _rows(ops.conn,
                     "SELECT kundnummer, type, first_name, last_name, company_name, "
                     "org_nr, email, phone, active FROM customer ORDER BY kundnummer")

    @r.get("/books/{book_id}/customers/{kundnummer}")
    def get_customer(kundnummer: int, ops: BookOps = Depends(_ops)):
        return ops.get_customer(kundnummer)

    @r.post("/books/{book_id}/customers", status_code=201)
    def create_customer(body: sc.CustomerReq, ops: BookOps = Depends(_ops)):
        data = body.model_dump(exclude_none=True)
        ctype = data.pop("type")
        return {"kundnummer": ops.create_customer(ctype, **data)}

    @r.patch("/books/{book_id}/customers/{kundnummer}")
    def update_customer(kundnummer: int, body: sc.CustomerUpdateReq, ops: BookOps = Depends(_ops)):
        ops.update_customer(kundnummer, **body.model_dump(exclude_none=True))
        return {"kundnummer": kundnummer}

    # ---- reference: suppliers ----
    @r.get("/books/{book_id}/suppliers")
    def list_suppliers(ops: BookOps = Depends(_ops)):
        return _rows(ops.conn, "SELECT id, name, default_moms_rate, org_nr, address, active "
                               "FROM supplier ORDER BY name")

    @r.post("/books/{book_id}/suppliers", status_code=201)
    def create_supplier(body: sc.SupplierReq, ops: BookOps = Depends(_ops)):
        return {"id": ops.create_supplier(body.name, body.default_moms_rate, body.org_nr, body.address)}

    @r.patch("/books/{book_id}/suppliers/{supplier_id}")
    def update_supplier(supplier_id: int, body: sc.SupplierUpdateReq, ops: BookOps = Depends(_ops)):
        ops.update_supplier(supplier_id, **body.model_dump(exclude_none=True))
        return {"id": supplier_id}

    # ---- bookkeeping ----
    @r.post("/books/{book_id}/expenses", status_code=201)
    def record_expense(body: sc.RecordExpenseReq, ops: BookOps = Depends(_ops)):
        return ops.record_expense(
            body.supplier_id, body.category_id, [l.model_dump() for l in body.lines],
            body.trans_date, note=body.note,
            receipt_original_format=body.receipt_original_format, paid_date=body.paid_date,
        )

    @r.post("/books/{book_id}/incomes", status_code=201)
    def record_income(body: sc.RecordIncomeReq, ops: BookOps = Depends(_ops)):
        return ops.record_income(
            body.customer_id, body.category_id, [l.model_dump() for l in body.lines],
            body.trans_date, rut_amount_ore=body.rut_amount_ore,
            note=body.note, paid_date=body.paid_date,
        )

    @r.post("/books/{book_id}/transaktioner/{transaktion_id}/pay")
    def register_payment(transaktion_id: int, body: sc.PaymentReq, ops: BookOps = Depends(_ops)):
        return ops.register_payment(transaktion_id, body.payment_date)

    @r.post("/books/{book_id}/rut/{rut_claim_id}/skatteverket-payment")
    def rut_skatteverket_payment(rut_claim_id: int, body: sc.PaymentReq, ops: BookOps = Depends(_ops)):
        return ops.register_rut_skatteverket_payment(rut_claim_id, body.payment_date)

    @r.get("/books/{book_id}/customers/{kundnummer}/rut-cap/{year}")
    def rut_cap(kundnummer: int, year: int, ops: BookOps = Depends(_ops)):
        return ops.rut_cap_status(kundnummer, year)

    @r.post("/books/{book_id}/verifikationer/{verifikation_id}/reverse", status_code=201)
    def reverse_verifikation(verifikation_id: int, body: sc.ReverseReq, ops: BookOps = Depends(_ops)):
        return ops.reverse_verifikation(verifikation_id, body.reason, body.reg_date)

    @r.post("/books/{book_id}/period-locks", status_code=201)
    def lock_period(body: sc.PeriodLockReq, ops: BookOps = Depends(_ops)):
        return {"id": ops.lock_period(body.period_start, body.period_end, body.kind)}

    @r.get("/books/{book_id}/verifikationer")
    def list_verifikationer(ops: BookOps = Depends(_ops)):
        return _rows(ops.conn,
                     "SELECT id, series, ver_number, ver_date, text, posted, rattelse_of "
                     "FROM verifikation ORDER BY ver_number")

    @r.get("/books/{book_id}/transaktioner")
    def list_transaktioner(ops: BookOps = Depends(_ops)):
        return _rows(ops.conn,
                     "SELECT id, direction, status, trans_date, payment_date, category_id, "
                     "customer_id, supplier_id, verifikation_id FROM transaktion ORDER BY id")

    # ---- reports ----
    @r.get("/books/{book_id}/reports/momsdeklaration")
    def report_moms(start: str, end: str, ops: BookOps = Depends(_ops)):
        return vat_report.momsdeklaration(ops.conn, start, end)

    @r.get("/books/{book_id}/reports/result")
    def report_result(start: str, end: str, ops: BookOps = Depends(_ops)):
        return result_report.result_report(ops.conn, start, end)

    @r.get("/books/{book_id}/reports/sie", response_class=PlainTextResponse)
    def report_sie(ops: BookOps = Depends(_ops), company_name: str = "", org_nr: str = ""):
        return sie_report.export_sie(ops.conn, company_name=company_name, org_nr=org_nr)

    return r


# ===========================================================================
# Helpers
# ===========================================================================

def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# Convenience for `python -m backend.api.app`
def run(host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    run()
