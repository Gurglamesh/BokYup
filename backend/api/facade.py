"""
facade.py — transport-independent application core (Phase 1 of the phone plan).

The whole backend is reachable through ONE in-process entry point, `AppFacade.dispatch`,
keyed by (HTTP method, path) exactly like the REST API. Two transports drive it:

  * Desktop / PC  — `api/app.py` (FastAPI) validates the request body with Pydantic,
    then delegates to `dispatch`. The HTTP server is for the desktop web UI.
  * Phone         — the same web frontend runs inside a WebView with the Python
    backend loaded as WebAssembly (Pyodide). There are no sockets in WASM, so a thin
    JS shim calls `dispatch(method, path, body)` directly, in-process.

Keeping the request→operations logic here (not in the FastAPI handlers) means the
legal/bookkeeping rules are written and verified ONCE and run identically on every
platform — the core principle in CLAUDE.md.

`dispatch` returns `(status_code, result)` and raises the same domain exceptions the
backend already uses; each transport maps those to its own error shape.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from backend.db.manager import DatabaseManager
from backend.db.operations import BookOps
from backend.models import schema as S
from backend.reports import arsbokslut as arsbokslut_report
from backend.reports import result as result_report
from backend.reports import sie as sie_report
from backend.reports import vat as vat_report

DEFAULT_AUTOLOCK_SECONDS = 15 * 60


class BookLocked(Exception):
    """Raised when an operation needs an unlocked book but the session is locked
    (or was just auto-locked for inactivity). Transports map this to HTTP 423."""


@dataclass
class RawResult:
    """A non-JSON result (a raw photo blob, or the plain-text SIE file). Transports
    render it natively: FastAPI as a Response, the phone shim as bytes/base64."""
    content: bytes | str
    media_type: str


# ---------------------------------------------------------------------------
# Routing table plumbing
# ---------------------------------------------------------------------------

_Handler = Callable[..., Any]
_ROUTES: list[tuple[str, re.Pattern, str, int]] = []


def _route(method: str, pattern: str, handler_name: str, status: int = 200):
    regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
    _ROUTES.append((method.upper(), re.compile("^" + regex + "$"), handler_name, status))


class AppFacade:
    """Owns the DatabaseManager + auto-lock state and exposes every operation."""

    def __init__(self, manager: DatabaseManager,
                 autolock_seconds: int = DEFAULT_AUTOLOCK_SECONDS):
        self.manager = manager
        self.autolock_seconds = autolock_seconds
        self.last_activity: dict[str, float] = {}

    # ----- dispatch -------------------------------------------------------
    def dispatch(self, method: str, path: str,
                 body: Optional[dict] = None,
                 query: Optional[dict] = None) -> tuple[int, Any]:
        method = method.upper()
        path = "/" + path.strip("/") if path != "/" else "/"
        for m, regex, handler_name, status in _ROUTES:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                handler = getattr(self, handler_name)
                result = handler(match.groupdict(), body or {}, query or {})
                return status, result
        raise KeyError(f"No route for {method} {path}")

    # ----- auto-lock-aware resolver --------------------------------------
    def _ops(self, book_id: str) -> BookOps:
        session = self.manager.get_session(book_id)
        if session is None:
            raise BookLocked("Book is locked")
        now = time.monotonic()
        last = self.last_activity.get(book_id)
        if self.autolock_seconds and last is not None and now - last > self.autolock_seconds:
            self.manager.lock_book(book_id)
            self.last_activity.pop(book_id, None)
            raise BookLocked("Book auto-locked due to inactivity")
        self.last_activity[book_id] = now
        return BookOps(session)

    def _touch(self, book_id: str) -> None:
        self.last_activity[book_id] = time.monotonic()

    def sweep(self) -> None:
        """Actively wipe idle sessions' DEKs (called by the desktop background task)."""
        if not self.autolock_seconds:
            return
        now = time.monotonic()
        for rec in self.manager.list_books():
            last = self.last_activity.get(rec.id)
            if last is not None and now - last > self.autolock_seconds:
                self.manager.lock_book(rec.id)
                self.last_activity.pop(rec.id, None)

    # =====================================================================
    # Handlers — (params, body, query) -> result. One per REST route.
    # =====================================================================

    # ---- meta ----
    def h_root(self, p, b, q):
        from backend import __version__ as APP_VERSION
        return {"name": "BokYup API", "version": APP_VERSION}

    # ---- books / registry ----
    def h_list_books(self, p, b, q):
        return [rec.to_dict() for rec in self.manager.list_books()]

    def h_create_book(self, p, b, q):
        record, session = self.manager.create_book(b["display_name"], b["db_path"], b["passphrase"])
        S.initialize_schema(session.connection())
        self._touch(record.id)
        return record.to_dict()

    def h_unlock(self, p, b, q):
        self.manager.open_book(p["book_id"], b["passphrase"])
        self._touch(p["book_id"])
        return {"book_id": p["book_id"], "unlocked": True}

    def h_unlock_recovery(self, p, b, q):
        self.manager.open_book_with_recovery(p["book_id"], b["recovery_key"])
        self._touch(p["book_id"])
        return {"book_id": p["book_id"], "unlocked": True}

    def h_lock(self, p, b, q):
        self.manager.lock_book(p["book_id"])
        self.last_activity.pop(p["book_id"], None)
        return {"book_id": p["book_id"], "locked": True}

    def h_rename(self, p, b, q):
        self.manager.rename_book(p["book_id"], b["display_name"])
        return {"book_id": p["book_id"], "display_name": b["display_name"]}

    def h_remove(self, p, b, q):
        # delete_files may arrive as a query param (?delete_files=true) or in the body.
        flag = q.get("delete_files") if q else None
        if flag is None:
            flag = (b or {}).get("delete_files")
        delete_files = str(flag).lower() in ("1", "true", "yes") if flag is not None else False
        res = self.manager.remove_from_registry(p["book_id"], delete_files=delete_files)
        return {"book_id": p["book_id"], "removed": True, **res}

    def h_export_book(self, p, b, q):
        out = self.manager.export_book(p["book_id"], b["out_path"])
        return {"out_path": str(out)}

    def h_import_book(self, p, b, q):
        rec = self.manager.import_book(
            b["bundle_path"], b["dest_db_path"],
            display_name=b.get("display_name"), overwrite=b.get("overwrite", False),
        )
        return rec.to_dict()

    def h_change_passphrase(self, p, b, q):
        self.manager.change_passphrase(p["book_id"], b["old_passphrase"], b["new_passphrase"])
        return {"book_id": p["book_id"], "changed": True}

    def h_add_recovery_key(self, p, b, q):
        key = self.manager.add_recovery_key(p["book_id"], b["passphrase"], b.get("recovery_key"))
        return {"recovery_key": key}

    def h_recovery_key_status(self, p, b, q):
        return {"has_recovery_key": self.manager.has_recovery_key(p["book_id"])}

    # ---- reference: categories ----
    def h_list_categories(self, p, b, q):
        ops = self._ops(p["book_id"])
        # `used` flags categories already touched by the books (cannot be deleted).
        return _rows(ops,
                     "SELECT id, name, kind, bas_konto, default_rate_code, active, "
                     "(EXISTS(SELECT 1 FROM transaktion t WHERE t.category_id=category.id) "
                     " OR EXISTS(SELECT 1 FROM moms_line m WHERE m.category_id=category.id) "
                     " OR EXISTS(SELECT 1 FROM invoice_line il WHERE il.category_id=category.id)) "
                     "AS used FROM category ORDER BY bas_konto")

    def h_list_accounts(self, p, b, q):
        """All BAS-konton, each tagged with whether it is a system account (the
        booking engine's bank/moms/receivable konton) and which categories use it."""
        ops = self._ops(p["book_id"])
        sys_map = ops.system_accounts()             # {bas_konto: label}
        rows = _rows(ops, "SELECT a.bas_konto, a.name, "
                          "(SELECT COUNT(*) FROM category c WHERE c.bas_konto=a.bas_konto) "
                          "AS category_count FROM account a ORDER BY a.bas_konto")
        for r in rows:
            r["system_label"] = sys_map.get(r["bas_konto"])
            r["is_system"] = r["bas_konto"] in sys_map
        return rows

    def h_create_category(self, p, b, q):
        ops = self._ops(p["book_id"])
        cid = ops.create_category(b["name"], b["kind"], b["bas_konto"],
                                  b.get("account_name"), b.get("default_rate_code"))
        return {"id": cid}

    def h_update_category(self, p, b, q):
        ops = self._ops(p["book_id"])
        ops.update_category(int(p["category_id"]), **_clean(b))
        return {"id": int(p["category_id"])}

    def h_delete_category(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.delete_category(int(p["category_id"]))

    # ---- article catalog ----
    def h_list_articles(self, p, b, q):
        return self._ops(p["book_id"]).list_articles()

    def h_create_article(self, p, b, q):
        return self._ops(p["book_id"]).create_article(
            b["description"], b["prefix"], unit_price_ore=b.get("unit_price_ore", 0),
            rate_code=b.get("rate_code", "25"), reduction_type=b.get("reduction_type"),
            category_id=b.get("category_id"), unit=b.get("unit"))

    def h_update_article(self, p, b, q):
        ops = self._ops(p["book_id"])
        ops.update_article(int(p["article_id"]), **_clean(b))
        return {"id": int(p["article_id"])}

    def h_delete_article(self, p, b, q):
        self._ops(p["book_id"]).delete_article(int(p["article_id"]))
        return {"deleted": True}

    # ---- reference: customers ----
    def h_list_customers(self, p, b, q):
        ops = self._ops(p["book_id"])
        return _rows(ops,
                     "SELECT kundnummer, type, first_name, last_name, company_name, "
                     "org_nr, email, phone, active FROM customer ORDER BY kundnummer")

    def h_get_customer(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.get_customer(int(p["kundnummer"]))

    def h_create_customer(self, p, b, q):
        ops = self._ops(p["book_id"])
        data = _clean(b)
        ctype = data.pop("type")
        return {"kundnummer": ops.create_customer(ctype, **data)}

    def h_update_customer(self, p, b, q):
        ops = self._ops(p["book_id"])
        ops.update_customer(int(p["kundnummer"]), **_clean(b))
        return {"kundnummer": int(p["kundnummer"])}

    # ---- customer household relations (for RUT/ROT recipients) ----
    def h_list_customer_relations(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.list_related_customers(int(p["kundnummer"]))

    def h_link_customer(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.link_customers(int(p["kundnummer"]), int(b["other_kundnummer"]))

    def h_unlink_customer(self, p, b, q):
        ops = self._ops(p["book_id"])
        ops.unlink_customers(int(p["kundnummer"]), int(p["other_kundnummer"]))
        return {"unlinked": True}

    def h_reduction_config(self, p, b, q):
        rut, rot = self._ops(p["book_id"]).reduction_pcts()
        return {"rut_pct": rut, "rot_pct": rot}

    def h_husavdrag_cap(self, p, b, q):
        return self._ops(p["book_id"]).husavdrag_cap_status(
            int(p["kundnummer"]), int(p["year"]))

    # ---- reference: suppliers ----
    def h_list_suppliers(self, p, b, q):
        ops = self._ops(p["book_id"])
        return _rows(ops, "SELECT id, name, default_moms_rate, org_nr, address, active "
                          "FROM supplier ORDER BY name")

    def h_create_supplier(self, p, b, q):
        ops = self._ops(p["book_id"])
        return {"id": ops.create_supplier(b["name"], b.get("default_moms_rate", "25"),
                                          b.get("org_nr"), b.get("address"))}

    def h_update_supplier(self, p, b, q):
        ops = self._ops(p["book_id"])
        ops.update_supplier(int(p["supplier_id"]), **_clean(b))
        return {"id": int(p["supplier_id"])}

    # ---- bookkeeping ----
    def h_record_expense(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.record_expense(
            b.get("supplier_id"), b["category_id"], b["lines"], b["trans_date"],
            note=b.get("note"), receipt_original_format=b.get("receipt_original_format"),
            paid_date=b.get("paid_date"),
        )

    def h_record_income(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.record_income(
            b["customer_id"], b["category_id"], b["lines"], b["trans_date"],
            rut_amount_ore=b.get("rut_amount_ore", 0), note=b.get("note"),
            paid_date=b.get("paid_date"),
        )

    def h_register_payment(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.register_payment(int(p["transaktion_id"]), b["payment_date"])

    def h_rut_skatteverket_payment(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.register_rut_skatteverket_payment(
            int(p["rut_claim_id"]), b["payment_date"],
            received_ore=b.get("received_ore"), mode=b.get("mode"),
            relation_note=b.get("relation_note"), reference=b.get("reference"))

    def h_upload_rut_receipt(self, p, b, q):
        import base64
        import binascii
        ops = self._ops(p["book_id"])
        try:
            data = base64.b64decode(b["image_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image_base64 is not valid base64")
        if not data:
            raise ValueError("empty file")
        return ops.attach_rut_receipt(int(p["rut_claim_id"]), data, b.get("mime", "image/jpeg"))

    def h_list_rut_receipts(self, p, b, q):
        return self._ops(p["book_id"]).list_rut_receipts(int(p["rut_claim_id"]))

    def h_skatteverket_preview(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.skatteverket_payment_preview(int(p["rut_claim_id"]), int(b["received_ore"]))

    def h_rut_cap(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.rut_cap_status(int(p["kundnummer"]), int(p["year"]))

    def h_list_rut_claims(self, p, b, q):
        ops = self._ops(p["book_id"])
        return _rows(ops,
                     "SELECT id, transaktion_id, customer_id, rut_amount_ore, state, "
                     "customer_payment_date, skatteverket_payment_date, "
                     "skatteverket_received_ore, shortfall_invoice_id, "
                     "skatteverket_reference, claim_year FROM rut_claim ORDER BY id")

    def h_reverse_verifikation(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.reverse_verifikation(int(p["verifikation_id"]), b["reason"], b.get("reg_date"))

    def h_lock_period(self, p, b, q):
        ops = self._ops(p["book_id"])
        return {"id": ops.lock_period(b["period_start"], b["period_end"], b.get("kind", "moms"))}

    def h_year_end_accruals(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.book_year_end_accruals(b["fiscal_year_end"])

    def h_list_verifikationer(self, p, b, q):
        ops = self._ops(p["book_id"])
        return _rows(ops,
                     "SELECT id, series, ver_number, ver_date, text, posted, rattelse_of "
                     "FROM verifikation ORDER BY ver_number")

    def h_verifikationer_full(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.verifikationer_full(q.get("start"), q.get("end"))

    def h_huvudbok(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.huvudbok(q.get("start"), q.get("end"))

    def h_manual_verifikation(self, p, b, q):
        ops = self._ops(p["book_id"])
        # The UI works in debit/credit columns; the ledger stores signed amounts.
        lines = [{"bas_konto": ln["bas_konto"],
                  "amount_ore": int(ln.get("debit_ore") or 0) - int(ln.get("credit_ore") or 0),
                  "account_name": ln.get("account_name"), "text": ln.get("text")}
                 for ln in b["postings"]]
        return ops.add_manual_verifikation(b["ver_date"], b["text"], lines, b.get("reg_date"))

    def h_list_transaktioner(self, p, b, q):
        ops = self._ops(p["book_id"])
        cols = ("SELECT id, direction, status, trans_date, payment_date, category_id, "
                "customer_id, supplier_id, verifikation_id, note FROM transaktion")
        if q.get("include_synthetic") in ("1", "true", True):
            return _rows(ops, cols + " ORDER BY id")
        from backend.db.operations import SYNTHETIC_TRANSAKTION_NOTES as _SYN
        placeholders = ",".join("?" for _ in _SYN)
        return _rows(ops, f"{cols} WHERE note IS NULL OR note NOT IN ({placeholders}) "
                          "ORDER BY id", tuple(_SYN))

    # ---- receipts (encrypted photos) ----
    def h_upload_receipt(self, p, b, q):
        import base64
        import binascii
        ops = self._ops(p["book_id"])
        try:
            data = base64.b64decode(b["image_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image_base64 is not valid base64")
        if not data:
            raise ValueError("empty image")
        return ops.attach_receipt(int(p["transaktion_id"]), data, b["mime"],
                                  b.get("original_format"))

    def h_list_receipts(self, p, b, q):
        ops = self._ops(p["book_id"])
        return ops.list_receipts(int(p["transaktion_id"]))

    def h_get_receipt(self, p, b, q):
        ops = self._ops(p["book_id"])
        data, mime = ops.get_receipt(int(p["receipt_id"]))
        return RawResult(content=data, media_type=mime)

    def h_delete_receipt(self, p, b, q):
        ops = self._ops(p["book_id"])
        ops.delete_receipt(int(p["receipt_id"]))
        return {"id": int(p["receipt_id"]), "deleted": True}

    # ---- reports ----
    def h_report_moms(self, p, b, q):
        ops = self._ops(p["book_id"])
        return vat_report.momsdeklaration(ops.conn, q["start"], q["end"])

    def h_report_result(self, p, b, q):
        ops = self._ops(p["book_id"])
        return result_report.result_report(ops.conn, q["start"], q["end"])

    def h_report_arsbokslut(self, p, b, q):
        ops = self._ops(p["book_id"])
        return arsbokslut_report.forenklat_arsbokslut(ops.conn, q["start"], q["end"])

    def h_report_sie(self, p, b, q):
        ops = self._ops(p["book_id"])
        text = sie_report.export_sie(
            ops.conn, company_name=q.get("company_name", ""), org_nr=q.get("org_nr", ""),
            fiscal_year_start=q.get("fiscal_year_start") or None,
            fiscal_year_end=q.get("fiscal_year_end") or None,
        )
        return RawResult(content=text, media_type="text/plain; charset=utf-8")

    # ---- accounting method (per book) ----
    def h_get_accounting_method(self, p, b, q):
        return {"method": self._ops(p["book_id"]).get_accounting_method()}

    def h_set_accounting_method(self, p, b, q):
        self._ops(p["book_id"]).set_accounting_method(b["method"])
        return {"method": b["method"]}

    # ---- company profile (seller) ----
    def h_get_company(self, p, b, q):
        return self._ops(p["book_id"]).get_company()

    def h_set_company(self, p, b, q):
        self._ops(p["book_id"]).set_company(**_clean(b))
        return {"saved": True}

    def h_get_logo(self, p, b, q):
        logo = self._ops(p["book_id"]).get_logo()
        if logo is None:
            raise KeyError("No logo set")
        data, mime = logo
        return RawResult(content=data, media_type=mime)

    def h_set_logo(self, p, b, q):
        import base64
        import binascii
        try:
            data = base64.b64decode(b["image_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image_base64 is not valid base64")
        self._ops(p["book_id"]).set_logo(data)
        return {"saved": True}

    def h_delete_logo(self, p, b, q):
        self._ops(p["book_id"]).delete_logo()
        return {"deleted": True}

    # ---- payment methods ----
    def h_list_payment_methods(self, p, b, q):
        return self._ops(p["book_id"]).list_payment_methods()

    def h_create_payment_method(self, p, b, q):
        return {"id": self._ops(p["book_id"]).create_payment_method(
            b["label"], b["value"], b.get("sort_order", 0))}

    def h_update_payment_method(self, p, b, q):
        self._ops(p["book_id"]).update_payment_method(int(p["payment_method_id"]), **_clean(b))
        return {"id": int(p["payment_method_id"])}

    # ---- invoices (faktura) ----
    def h_create_invoice(self, p, b, q):
        return self._ops(p["book_id"]).create_invoice(
            customer_id=b["customer_id"], category_id=b.get("category_id"),
            invoice_date=b["invoice_date"], due_date=b["due_date"], lines=b["lines"],
            recipients=b.get("recipients"), delivery_date=b.get("delivery_date"),
            payment_terms=b.get("payment_terms"), our_reference=b.get("our_reference"),
            your_reference=b.get("your_reference"), note=b.get("note"))

    def h_list_invoices(self, p, b, q):
        return self._ops(p["book_id"]).list_invoices()

    # ---- invoice drafts ----
    def h_list_drafts(self, p, b, q):
        return self._ops(p["book_id"]).list_drafts()

    def h_create_draft(self, p, b, q):
        return self._ops(p["book_id"]).save_draft(b["payload"])

    def h_get_draft(self, p, b, q):
        return self._ops(p["book_id"]).get_draft(int(p["draft_id"]))

    def h_update_draft(self, p, b, q):
        return self._ops(p["book_id"]).save_draft(b["payload"], draft_id=int(p["draft_id"]))

    def h_delete_draft(self, p, b, q):
        self._ops(p["book_id"]).delete_draft(int(p["draft_id"]))
        return {"deleted": True}

    def h_get_invoice(self, p, b, q):
        return self._ops(p["book_id"]).get_invoice(int(p["invoice_id"]))

    def h_cancel_invoice(self, p, b, q):
        return self._ops(p["book_id"]).cancel_invoice(int(p["invoice_id"]))

    def h_pay_invoice(self, p, b, q):
        return self._ops(p["book_id"]).pay_invoice(
            int(p["invoice_id"]), b.get("amount_ore"), b.get("date"))

    def h_refund_invoice(self, p, b, q):
        return self._ops(p["book_id"]).refund_invoice(
            int(p["invoice_id"]), b["amount_ore"], b.get("date"), b.get("note"))

    def h_credit_invoice(self, p, b, q):
        return self._ops(p["book_id"]).credit_invoice(
            int(p["invoice_id"]), amount_ore=b.get("amount_ore"),
            reason=b.get("reason"), date=b.get("date"))

    def h_invoice_pdf(self, p, b, q):
        from backend.invoices.pdf import render_invoice_pdf
        ops = self._ops(p["book_id"])
        logo = ops.get_logo()
        pdf = render_invoice_pdf(ops.get_invoice(int(p["invoice_id"])),
                                 logo_png=logo[0] if logo else None)
        return RawResult(content=pdf, media_type="application/pdf")

    def h_credit_note_pdf(self, p, b, q):
        from backend.invoices.pdf import render_invoice_pdf
        ops = self._ops(p["book_id"])
        logo = ops.get_logo()
        pdf = render_invoice_pdf(
            ops.get_credit_note(int(p["invoice_id"]), int(p["event_id"])),
            logo_png=logo[0] if logo else None)
        return RawResult(content=pdf, media_type="application/pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows(ops: BookOps, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in ops.conn.execute(sql, params).fetchall()]


def _clean(body: dict) -> dict:
    """Drop None values (mirrors Pydantic's exclude_none for partial updates)."""
    return {k: v for k, v in body.items() if v is not None}


# ---------------------------------------------------------------------------
# Route table — the single source of (method, path) -> handler.
# ---------------------------------------------------------------------------

_route("GET", "/", "h_root")
_route("GET", "/books", "h_list_books")
_route("POST", "/books", "h_create_book", 201)
_route("POST", "/books/import", "h_import_book", 201)
_route("POST", "/books/{book_id}/unlock", "h_unlock")
_route("POST", "/books/{book_id}/unlock-recovery", "h_unlock_recovery")
_route("POST", "/books/{book_id}/lock", "h_lock")
_route("PATCH", "/books/{book_id}", "h_rename")
_route("DELETE", "/books/{book_id}", "h_remove")
_route("POST", "/books/{book_id}/export", "h_export_book")
_route("POST", "/books/{book_id}/change-passphrase", "h_change_passphrase")
_route("GET", "/books/{book_id}/recovery-key", "h_recovery_key_status")
_route("POST", "/books/{book_id}/recovery-key", "h_add_recovery_key", 201)

_route("GET", "/books/{book_id}/categories", "h_list_categories")
_route("GET", "/books/{book_id}/accounts", "h_list_accounts")
_route("GET", "/books/{book_id}/articles", "h_list_articles")
_route("POST", "/books/{book_id}/articles", "h_create_article", 201)
_route("PATCH", "/books/{book_id}/articles/{article_id}", "h_update_article")
_route("DELETE", "/books/{book_id}/articles/{article_id}", "h_delete_article")
_route("POST", "/books/{book_id}/categories", "h_create_category", 201)
_route("PATCH", "/books/{book_id}/categories/{category_id}", "h_update_category")
_route("DELETE", "/books/{book_id}/categories/{category_id}", "h_delete_category")

_route("GET", "/books/{book_id}/customers", "h_list_customers")
_route("GET", "/books/{book_id}/customers/{kundnummer}/rut-cap/{year}", "h_rut_cap")
_route("GET", "/books/{book_id}/customers/{kundnummer}/husavdrag-cap/{year}", "h_husavdrag_cap")
_route("GET", "/books/{book_id}/customers/{kundnummer}/relations", "h_list_customer_relations")
_route("POST", "/books/{book_id}/customers/{kundnummer}/relations", "h_link_customer", 201)
_route("DELETE", "/books/{book_id}/customers/{kundnummer}/relations/{other_kundnummer}", "h_unlink_customer")
_route("GET", "/books/{book_id}/customers/{kundnummer}", "h_get_customer")
_route("POST", "/books/{book_id}/customers", "h_create_customer", 201)
_route("PATCH", "/books/{book_id}/customers/{kundnummer}", "h_update_customer")
_route("GET", "/books/{book_id}/reduction-config", "h_reduction_config")

_route("GET", "/books/{book_id}/suppliers", "h_list_suppliers")
_route("POST", "/books/{book_id}/suppliers", "h_create_supplier", 201)
_route("PATCH", "/books/{book_id}/suppliers/{supplier_id}", "h_update_supplier")

_route("POST", "/books/{book_id}/expenses", "h_record_expense", 201)
_route("POST", "/books/{book_id}/incomes", "h_record_income", 201)
_route("POST", "/books/{book_id}/transaktioner/{transaktion_id}/pay", "h_register_payment")
_route("POST", "/books/{book_id}/rut/{rut_claim_id}/skatteverket-payment", "h_rut_skatteverket_payment")
_route("POST", "/books/{book_id}/rut/{rut_claim_id}/skatteverket-preview", "h_skatteverket_preview")
_route("POST", "/books/{book_id}/rut/{rut_claim_id}/receipt", "h_upload_rut_receipt", 201)
_route("GET", "/books/{book_id}/rut/{rut_claim_id}/receipts", "h_list_rut_receipts")
_route("GET", "/books/{book_id}/rut-claims", "h_list_rut_claims")
_route("POST", "/books/{book_id}/verifikationer/{verifikation_id}/reverse", "h_reverse_verifikation", 201)
_route("POST", "/books/{book_id}/period-locks", "h_lock_period", 201)
_route("POST", "/books/{book_id}/year-end-accruals", "h_year_end_accruals", 201)
_route("GET", "/books/{book_id}/verifikationer", "h_list_verifikationer")
_route("GET", "/books/{book_id}/verifikationer-full", "h_verifikationer_full")
_route("GET", "/books/{book_id}/huvudbok", "h_huvudbok")
_route("POST", "/books/{book_id}/verifikationer/manual", "h_manual_verifikation", 201)
_route("GET", "/books/{book_id}/transaktioner", "h_list_transaktioner")

_route("POST", "/books/{book_id}/transaktioner/{transaktion_id}/receipts", "h_upload_receipt", 201)
_route("GET", "/books/{book_id}/transaktioner/{transaktion_id}/receipts", "h_list_receipts")
_route("GET", "/books/{book_id}/receipts/{receipt_id}", "h_get_receipt")
_route("DELETE", "/books/{book_id}/receipts/{receipt_id}", "h_delete_receipt")

_route("GET", "/books/{book_id}/reports/momsdeklaration", "h_report_moms")
_route("GET", "/books/{book_id}/reports/result", "h_report_result")
_route("GET", "/books/{book_id}/reports/arsbokslut", "h_report_arsbokslut")
_route("GET", "/books/{book_id}/reports/sie", "h_report_sie")

_route("GET", "/books/{book_id}/accounting-method", "h_get_accounting_method")
_route("PUT", "/books/{book_id}/accounting-method", "h_set_accounting_method")
_route("GET", "/books/{book_id}/company", "h_get_company")
_route("PUT", "/books/{book_id}/company", "h_set_company")
_route("GET", "/books/{book_id}/logo", "h_get_logo")
_route("PUT", "/books/{book_id}/logo", "h_set_logo")
_route("DELETE", "/books/{book_id}/logo", "h_delete_logo")
_route("GET", "/books/{book_id}/payment-methods", "h_list_payment_methods")
_route("POST", "/books/{book_id}/payment-methods", "h_create_payment_method", 201)
_route("PATCH", "/books/{book_id}/payment-methods/{payment_method_id}", "h_update_payment_method")
_route("GET", "/books/{book_id}/invoice-drafts", "h_list_drafts")
_route("POST", "/books/{book_id}/invoice-drafts", "h_create_draft", 201)
_route("GET", "/books/{book_id}/invoice-drafts/{draft_id}", "h_get_draft")
_route("PUT", "/books/{book_id}/invoice-drafts/{draft_id}", "h_update_draft")
_route("DELETE", "/books/{book_id}/invoice-drafts/{draft_id}", "h_delete_draft")
_route("POST", "/books/{book_id}/invoices", "h_create_invoice", 201)
_route("GET", "/books/{book_id}/invoices", "h_list_invoices")
_route("GET", "/books/{book_id}/invoices/{invoice_id}/pdf", "h_invoice_pdf")
_route("GET", "/books/{book_id}/invoices/{invoice_id}/credit-notes/{event_id}/pdf", "h_credit_note_pdf")
_route("POST", "/books/{book_id}/invoices/{invoice_id}/cancel", "h_cancel_invoice", 201)
_route("POST", "/books/{book_id}/invoices/{invoice_id}/pay", "h_pay_invoice", 201)
_route("POST", "/books/{book_id}/invoices/{invoice_id}/refund", "h_refund_invoice", 201)
_route("POST", "/books/{book_id}/invoices/{invoice_id}/credit", "h_credit_invoice", 201)
_route("GET", "/books/{book_id}/invoices/{invoice_id}", "h_get_invoice")
