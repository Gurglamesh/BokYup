"""
operations.py — Layer 4: Core bookkeeping operations.

This is where the legal rules from CLAUDE.md actually run. Everything here writes
through the Layer 3 schema and is guarded by its DB-level immutability triggers.

What lives here:

- **Reference data** (freely editable): accounts, categories, customers, suppliers.
  Editing these never rewrites already-issued records — invoices are frozen by the
  customer snapshot taken at income time.

- **Booking engine** (kontantmetod — book when money moves): a business
  `transaktion` is recorded as *pending*; when payment is registered it is *booked*
  into an immutable `verifikation` with balanced double-entry `posting`s and the
  next unbroken verifikationsnummer.

- **Double-entry postings** that always balance to zero (asserted before commit).
  All BAS-konton used by the engine come from `config` (see schema `_DEFAULT_CONFIG`)
  so account mapping is never hardcoded in logic.

- **RUT state machine** (private customers): pending → customer_paid →
  skatteverket_paid, booked as TWO verifikationer (the two cash movements land in
  different months). Cap per customer/year is read from config.

- **Rättelse** (correction): `reverse_verifikation` posts a mirror entry that
  references the original via `rattelse_of`. The original is never mutated; both
  remain visible — this is the primitive the approve/decline/cancel UX builds on.

- **Period locking**: once a period is locked (after a momsdeklaration is filed),
  nothing can be booked with a date inside it.

Money is integer ören throughout (see schema money helpers).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from backend.db.manager import BookSession
from backend.models import schema as S


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OperationError(Exception):
    """Base class for bookkeeping operation errors."""


class PeriodLocked(OperationError):
    """Raised when booking into a locked (filed) period is attempted."""


class ImbalancedPostings(OperationError):
    """Raised if a verifikation's postings do not sum to zero (a code bug)."""


class InvalidState(OperationError):
    """Raised on an illegal lifecycle transition (e.g. RUT order, double-book)."""


# ---------------------------------------------------------------------------
# System-account names (used only when auto-creating a configured account row)
# ---------------------------------------------------------------------------

_SYS_ACCOUNT_NAMES = {
    "account_bank": "Företagskonto / bank",
    "account_ingaende_moms": "Ingående moms",
    "account_utgaende_moms_25": "Utgående moms 25 %",
    "account_utgaende_moms_12": "Utgående moms 12 %",
    "account_utgaende_moms_6": "Utgående moms 6 %",
    "account_rut_fordran": "Kundfordran husavdrag (RUT/ROT)",
    "account_kundfordran": "Kundfordringar",
    "account_leverantorsskuld": "Leverantörsskulder",
}

_UTG_MOMS_KEY = {
    "25": "account_utgaende_moms_25",
    "12": "account_utgaende_moms_12",
    "6": "account_utgaende_moms_6",
}

# Notes stamped on the SYNTHETIC transaktion rows that `_clone_transaktion_for_report`
# creates to attribute a rättelse/accrual to the right period in the moms/result
# reports. They are bookkeeping artefacts, not user transactions, so the default
# Transaktioner list hides them (the legal record lives in the verifikationer).
SYNTHETIC_TRANSAKTION_NOTES = ("rättelse", "periodisering", "återföring",
                               "fakturabetalning", "kreditering")


# ---------------------------------------------------------------------------
# Moms calculation (pure)
# ---------------------------------------------------------------------------

def compute_moms_figures(amount_ore: int, rate_code: str, inclusive: bool) -> tuple[int, int, int]:
    """
    Return (ex_moms_ore, moms_ore, inc_moms_ore) for an amount at a given rate.

    `inclusive` says whether `amount_ore` already includes moms. moms is derived as
    (inc - ex) so the three figures always reconcile exactly to the öre.

    Rates with no deductible/owed moms (0 %, momsfri, ej_avdragsgill) yield moms 0
    and ex == inc == amount — i.e. the full amount is booked to the income/expense
    account (for ej_avdragsgill this correctly folds non-deductible moms into cost).
    """
    rate = S.MOMS_RATES[rate_code]
    if not rate:  # None (momsfri / ej_avdragsgill) or Decimal('0')
        return amount_ore, 0, amount_ore
    if inclusive:
        inc = amount_ore
        ex = int((Decimal(inc) / (1 + rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        moms = inc - ex
    else:
        ex = amount_ore
        moms = int((Decimal(ex) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        inc = ex + moms
    return ex, moms, inc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()




# ---------------------------------------------------------------------------
# Operations facade over one unlocked book
# ---------------------------------------------------------------------------

class BookOps:
    """Bookkeeping operations bound to one unlocked BookSession."""

    def __init__(self, session: BookSession) -> None:
        self.session = session
        self.conn: sqlite3.Connection = session.connection()

    # ==================================================================
    # Reference data
    # ==================================================================

    def ensure_account(self, bas_konto: int, name: str) -> int:
        """Create a BAS-konto if absent (idempotent). Returns the account number."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO account(bas_konto, name, created_at) VALUES (?,?,?)",
                (bas_konto, name, _now()),
            )
        return bas_konto

    def create_category(self, name: str, kind: str, bas_konto: int,
                        account_name: Optional[str] = None) -> int:
        """Create a category linked to a BAS-konto (auto-creating the account)."""
        if kind not in ("income", "expense"):
            raise ValueError("kind must be 'income' or 'expense'")
        self.ensure_account(bas_konto, account_name or name)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO category(name, kind, bas_konto, created_at) VALUES (?,?,?,?)",
                (name, kind, bas_konto, _now()),
            )
        return cur.lastrowid

    def update_category(self, category_id: int, *, name: Optional[str] = None,
                        bas_konto: Optional[int] = None, active: Optional[bool] = None,
                        account_name: Optional[str] = None) -> None:
        """Edit reference data freely. Does not touch already-booked verifikationer."""
        if bas_konto is not None:
            self.ensure_account(bas_konto, account_name or name or f"Konto {bas_konto}")
        sets, params = _build_update(
            {"name": name, "bas_konto": bas_konto,
             "active": None if active is None else int(active)}
        )
        if sets:
            with self.conn:
                self.conn.execute(f"UPDATE category SET {sets} WHERE id=?", (*params, category_id))

    def create_customer(self, type: str, **fields) -> int:
        """
        Create a customer. type='private'|'business'. Returns the stable kundnummer.

        A private customer's personnummer (if given) is validated (format + Luhn)
        and stored encrypted. Pass it as the plaintext kwarg `personnummer`.
        """
        if type not in S.CUSTOMER_TYPES:
            raise ValueError(f"type must be one of {S.CUSTOMER_TYPES}")

        pnr = fields.pop("personnummer", None)
        pnr_enc = None
        if pnr:
            if not S.is_valid_personnummer(pnr):
                raise ValueError("Invalid personnummer (format/Luhn)")
            pnr_enc = self.session.encrypt_text(pnr)

        cols = {"type": type, "personnummer_enc": pnr_enc, "created_at": _now()}
        for k in ("first_name", "last_name", "company_name", "org_nr",
                  "contact_person", "vat_nr", "address", "shipping_address", "email", "phone"):
            if k in fields:
                cols[k] = fields[k]

        names = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO customer({names}) VALUES ({qs})", tuple(cols.values())
            )
        return cur.lastrowid

    def update_customer(self, kundnummer: int, **fields) -> None:
        """Edit a customer freely (kundnummer is stable). Re-encrypts personnummer."""
        updates: dict[str, object] = {}
        if "personnummer" in fields:
            pnr = fields.pop("personnummer")
            if pnr:
                if not S.is_valid_personnummer(pnr):
                    raise ValueError("Invalid personnummer (format/Luhn)")
                updates["personnummer_enc"] = self.session.encrypt_text(pnr)
            else:
                updates["personnummer_enc"] = None
        for k in ("first_name", "last_name", "company_name", "org_nr", "contact_person",
                  "vat_nr", "address", "shipping_address", "email", "phone", "active"):
            if k in fields:
                updates[k] = fields[k]
        sets, params = _build_update(updates)
        if sets:
            with self.conn:
                self.conn.execute(f"UPDATE customer SET {sets} WHERE kundnummer=?",
                                  (*params, kundnummer))

    def get_customer(self, kundnummer: int) -> dict:
        """Return a customer as a dict with the personnummer decrypted."""
        row = self.conn.execute(
            "SELECT * FROM customer WHERE kundnummer=?", (kundnummer,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No customer {kundnummer}")
        d = dict(row)
        enc = d.pop("personnummer_enc", None)
        d["personnummer"] = self.session.decrypt_text(enc) if enc else None
        return d

    def create_supplier(self, name: str, default_moms_rate: str = S.DEFAULT_MOMS_RATE,
                        org_nr: Optional[str] = None, address: Optional[str] = None) -> int:
        if default_moms_rate not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {default_moms_rate!r}")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO supplier(name, default_moms_rate, org_nr, address, created_at) "
                "VALUES (?,?,?,?,?)",
                (name, default_moms_rate, org_nr, address, _now()),
            )
        return cur.lastrowid

    def update_supplier(self, supplier_id: int, **fields) -> None:
        allowed = {k: fields[k] for k in
                   ("name", "default_moms_rate", "org_nr", "address", "active")
                   if k in fields}
        if "default_moms_rate" in allowed and allowed["default_moms_rate"] not in S.MOMS_RATES:
            raise ValueError("Unknown moms rate")
        sets, params = _build_update(allowed)
        if sets:
            with self.conn:
                self.conn.execute(f"UPDATE supplier SET {sets} WHERE id=?",
                                  (*params, supplier_id))

    # ==================================================================
    # Recording business transactions (still pending — no money moved yet)
    # ==================================================================

    def record_expense(self, supplier_id: Optional[int], category_id: int,
                       lines: list[dict], trans_date: str, *,
                       note: Optional[str] = None,
                       receipt_original_format: Optional[str] = None,
                       paid_date: Optional[str] = None) -> dict:
        """
        Record a purchase (ingående moms, deductible). `lines` is a list of
        {rate_code, amount_ore, inclusive} dicts. If `paid_date` is given the
        transaktion is booked immediately (cash); otherwise it stays pending.
        """
        self._check_category(category_id, "expense")
        tid = self._insert_transaktion(
            direction="in", category_id=category_id, supplier_id=supplier_id,
            customer_id=None, trans_date=trans_date, note=note,
            receipt_original_format=receipt_original_format, snapshot_enc=None,
        )
        self._insert_moms_lines(tid, lines)
        result = {"transaktion_id": tid}
        if paid_date:
            result.update(self.register_payment(tid, paid_date))
        return result

    def record_income(self, customer_id: int, category_id: int,
                      lines: list[dict], trans_date: str, *,
                      rut_amount_ore: int = 0, note: Optional[str] = None,
                      paid_date: Optional[str] = None) -> dict:
        """
        Record a sale (utgående moms, owed). Snapshots the customer onto the record
        (frozen at issue). If `rut_amount_ore` > 0 a RUT claim is opened (private
        customers only). If `paid_date` is given the customer payment is booked now.
        """
        self._check_category(category_id, "income")
        customer = self.get_customer(customer_id)

        if rut_amount_ore:
            if customer["type"] != "private":
                raise ValueError("RUT applies to private customers only")
            if not customer["personnummer"]:
                raise ValueError("RUT requires the customer's personnummer")

        snapshot_enc = self.session.encrypt_text(json.dumps(customer, default=str))
        tid = self._insert_transaktion(
            direction="out", category_id=category_id, supplier_id=None,
            customer_id=customer_id, trans_date=trans_date, note=note,
            receipt_original_format=None, snapshot_enc=snapshot_enc,
        )
        self._insert_moms_lines(tid, lines)

        result: dict = {"transaktion_id": tid}
        if rut_amount_ore:
            claim_id = self._insert_rut_claim(tid, customer_id, rut_amount_ore,
                                              int(trans_date[:4]))
            result["rut_claim_id"] = claim_id
            result["rut_cap"] = self.rut_cap_status(customer_id, int(trans_date[:4]))

        if paid_date:
            result.update(self.register_payment(tid, paid_date))
        return result

    # ==================================================================
    # Booking (money moved) — creates the immutable verifikation
    # ==================================================================

    def register_payment(self, transaktion_id: int, payment_date: str) -> dict:
        """
        Book a pending transaktion: create the verifikation + balanced postings,
        assign the next verifikationsnummer, and mark the transaktion paid.

        For a RUT sale this books the CUSTOMER portion (bank gets inc − rut, the rut
        part becomes a receivable) and advances the claim to 'customer_paid'.
        """
        t = self.conn.execute(
            "SELECT * FROM transaktion WHERE id=?", (transaktion_id,)
        ).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["status"] == "paid":
            raise InvalidState("Transaktion is already paid")

        ex, moms_by_rate, inc = self._sum_moms(transaktion_id)
        sum_moms = sum(moms_by_rate.values())
        konto = self._category_konto(t["category_id"])

        claim = self.conn.execute(
            "SELECT * FROM rut_claim WHERE transaktion_id=?", (transaktion_id,)
        ).fetchone()
        rut = claim["rut_amount_ore"] if claim else 0

        # Fakturametod: the invoice was already booked at issue (kundfordran). Payment
        # only settles the receivable (bank <- kundfordran); income/moms already booked.
        if t["verifikation_id"] is not None:
            cust_part = inc - rut
            postings = []
            if cust_part:
                postings.append((self._sys_account("account_bank"), cust_part, "inbetalning"))
                postings.append((self._sys_account("account_kundfordran"), -cust_part,
                                 "kvitta kundfordran"))
            with self.conn:
                vid, number = self._post_verifikation(
                    payment_date, payment_date, "Betalning faktura", postings)
                self.conn.execute(
                    "UPDATE transaktion SET status='paid', payment_date=? WHERE id=?",
                    (payment_date, transaktion_id))
                if claim:
                    self.conn.execute(
                        "UPDATE rut_claim SET state='customer_paid', customer_payment_date=? "
                        "WHERE id=?", (payment_date, claim["id"]))
            return {"verifikation_id": vid, "ver_number": number}

        if t["direction"] == "in":
            postings = [(konto, ex, "utgift")]
            if sum_moms:
                postings.append((self._sys_account("account_ingaende_moms"), sum_moms, "ingående moms"))
            postings.append((self._sys_account("account_bank"), -inc, "betalning"))
            text = "Utgift"
        else:  # 'out' — sale
            postings = [(self._sys_account("account_bank"), inc - rut, "inbetalning")]
            if rut:
                postings.append((self._sys_account("account_rut_fordran"), rut, "husavdrag fordran"))
            postings.append((konto, -ex, "försäljning"))
            for rate_code, m in moms_by_rate.items():
                if m and rate_code in _UTG_MOMS_KEY:
                    postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -m, f"utgående moms {rate_code}%"))
            text = "Försäljning"

        with self.conn:
            vid, number = self._post_verifikation(payment_date, payment_date, text, postings)
            self.conn.execute(
                "UPDATE transaktion SET status='paid', payment_date=?, verifikation_id=? WHERE id=?",
                (payment_date, vid, transaktion_id),
            )
            if claim:
                self.conn.execute(
                    "UPDATE rut_claim SET state='customer_paid', customer_payment_date=? WHERE id=?",
                    (payment_date, claim["id"]),
                )
        return {"verifikation_id": vid, "ver_number": number}

    def register_rut_skatteverket_payment(self, rut_claim_id: int, payment_date: str) -> dict:
        """
        Book the Skatteverket payout of a RUT claim as its own verifikation
        (bank ← receivable). The claim must already be 'customer_paid'.
        """
        claim = self.conn.execute(
            "SELECT * FROM rut_claim WHERE id=?", (rut_claim_id,)
        ).fetchone()
        if claim is None:
            raise KeyError(f"No rut_claim {rut_claim_id}")
        if claim["state"] != "customer_paid":
            raise InvalidState(
                f"RUT claim must be 'customer_paid' to receive Skatteverket payment "
                f"(is '{claim['state']}')"
            )
        rut = claim["rut_amount_ore"]
        postings = [
            (self._sys_account("account_bank"), rut, "husavdrag utbetalt"),
            (self._sys_account("account_rut_fordran"), -rut, "kvitta fordran"),
        ]
        with self.conn:
            vid, number = self._post_verifikation(
                payment_date, payment_date, "Husavdrag utbetalt av Skatteverket", postings
            )
            self.conn.execute(
                "UPDATE rut_claim SET state='skatteverket_paid', "
                "skatteverket_payment_date=?, skatteverket_verifikation_id=? WHERE id=?",
                (payment_date, vid, rut_claim_id),
            )
        return {"verifikation_id": vid, "ver_number": number}

    def rut_cap_status(self, customer_id: int, year: int) -> dict:
        """Return RUT/ROT cap usage for a customer in a year (cap is config)."""
        cap = int(self._config("rut_rot_cap_ore_per_customer_year"))
        used = self.conn.execute(
            "SELECT COALESCE(SUM(rut_amount_ore), 0) FROM rut_claim "
            "WHERE customer_id=? AND claim_year=?",
            (customer_id, year),
        ).fetchone()[0]
        remaining = cap - used
        return {
            "cap_ore": cap, "used_ore": used, "remaining_ore": remaining,
            "over_cap": used > cap,
            # warn when within 10 % of the cap (CLAUDE.md: warn as customer approaches)
            "near_cap": used >= cap * 0.9,
        }

    # ==================================================================
    # Corrections (rättelse)
    # ==================================================================

    def reverse_verifikation(self, verifikation_id: int, reason: str,
                             reg_date: Optional[str] = None) -> dict:
        """
        Post a rättelse: a new verifikation whose postings mirror (negate) the
        original, referencing it via `rattelse_of`. The original is untouched and
        both remain visible. Returns the new verifikation id + number.
        """
        original = self.conn.execute(
            "SELECT * FROM verifikation WHERE id=?", (verifikation_id,)
        ).fetchone()
        if original is None:
            raise KeyError(f"No verifikation {verifikation_id}")
        if not original["posted"]:
            raise InvalidState("Only a posted verifikation needs a rättelse")

        orig_postings = self.conn.execute(
            "SELECT bas_konto, amount_ore, text FROM posting WHERE verifikation_id=?",
            (verifikation_id,),
        ).fetchall()
        mirror = [(p["bas_konto"], -p["amount_ore"], f"rättelse: {p['text'] or ''}".strip())
                  for p in orig_postings]

        ver_date = reg_date or _now()[:10]
        text = f"Rättelse av ver {original['series']}{original['ver_number']}: {reason}"
        with self.conn:
            vid, number = self._post_verifikation(
                ver_date, _now()[:10], text, mirror, rattelse_of=verifikation_id,
            )
            # If the reversed verifikation was a transaktion's booking, also mirror
            # its moms_lines (negated) so the momsdeklaration and result report net
            # the correction in the rättelse's period.
            src = self.conn.execute(
                "SELECT id FROM transaktion WHERE verifikation_id=?", (verifikation_id,)
            ).fetchone()
            if src:
                self._clone_transaktion_for_report(src["id"], vid, ver_date, -1, "rättelse")
        return {"verifikation_id": vid, "ver_number": number}

    # ==================================================================
    # Year-end accrual (bokslut) — kontantmetod must book unpaid invoices
    # ==================================================================

    def book_year_end_accruals(self, fiscal_year_end: str) -> dict:
        """
        Book all still-unpaid (pending) invoices dated on/before `fiscal_year_end`
        into that fiscal year, as required at bokslut even under kontantmetod.

        Uses the standard *vändning* method: an accrual verifikation on the last
        day of the year (kundfordran/leverantörsskuld + income/expense + moms) and
        an automatic reversal on the first day of the next year. The original
        pending invoice is left untouched and books normally (cash) when actually
        paid, so nothing is double-counted — while the income and the moms still
        land in the correct (closing) year. Returns a summary of what was booked.
        """
        from datetime import date, timedelta

        y, m, d = (int(x) for x in fiscal_year_end.split("-"))
        next_day = (date(y, m, d) + timedelta(days=1)).isoformat()

        pending = self.conn.execute(
            "SELECT * FROM transaktion WHERE status='pending' AND verifikation_id IS NULL "
            "AND trans_date <= ? ORDER BY id",
            (fiscal_year_end,),
        ).fetchall()

        booked = []
        for t in pending:
            ex, moms_by_rate, inc = self._sum_moms(t["id"])
            sum_moms = sum(moms_by_rate.values())
            konto = self._category_konto(t["category_id"])

            if t["direction"] == "out":
                postings = [(self._sys_account("account_kundfordran"), inc, "kundfordran"),
                            (konto, -ex, "försäljning")]
                for rate_code, mm in moms_by_rate.items():
                    if mm and rate_code in _UTG_MOMS_KEY:
                        postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -mm,
                                         f"utgående moms {rate_code}%"))
            else:
                postings = [(konto, ex, "utgift")]
                if sum_moms:
                    postings.append((self._sys_account("account_ingaende_moms"), sum_moms, "ingående moms"))
                postings.append((self._sys_account("account_leverantorsskuld"), -inc, "leverantörsskuld"))

            reversal = [(k, -a, f"återföring: {txt}") for (k, a, txt) in postings]

            with self.conn:
                avid, anum = self._post_verifikation(
                    fiscal_year_end, fiscal_year_end,
                    f"Periodisering bokslut (transaktion {t['id']})", postings)
                self._clone_transaktion_for_report(t["id"], avid, fiscal_year_end, 1, "periodisering")

                rvid, rnum = self._post_verifikation(
                    next_day, next_day,
                    f"Återföring periodisering (transaktion {t['id']})", reversal)
                self._clone_transaktion_for_report(t["id"], rvid, next_day, -1, "återföring")

            booked.append({"transaktion_id": t["id"], "accrual_ver": anum, "reversal_ver": rnum})

        return {"count": len(booked), "fiscal_year_end": fiscal_year_end, "accruals": booked}

    # ==================================================================
    # Period locking
    # ==================================================================

    def lock_period(self, period_start: str, period_end: str, kind: str = "moms") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO period_lock(period_start, period_end, kind, locked_at) "
                "VALUES (?,?,?,?)",
                (period_start, period_end, kind, _now()),
            )
        return cur.lastrowid

    def is_period_locked(self, date: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM period_lock WHERE ? BETWEEN period_start AND period_end LIMIT 1",
            (date,),
        ).fetchone()
        return row is not None

    # ==================================================================
    # Company profile (seller) + payment methods — reference data
    # ==================================================================

    def get_accounting_method(self) -> str:
        """'kontantmetod' (book at payment) or 'fakturametod' (book at issue + payment)."""
        row = self.conn.execute(
            "SELECT value FROM config WHERE key='bokforingsmetod'").fetchone()
        return row[0] if row else "kontantmetod"

    def set_accounting_method(self, method: str) -> None:
        if method not in ("kontantmetod", "fakturametod"):
            raise ValueError("method must be 'kontantmetod' or 'fakturametod'")
        with self.conn:
            self.conn.execute(
                "INSERT INTO config(key, value) VALUES ('bokforingsmetod', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (method,))

    def get_company(self) -> dict:
        row = self.conn.execute(
            "SELECT id, name, org_nr, vat_nr, address, email, phone, f_skatt, updated_at, "
            "(logo_enc IS NOT NULL) AS has_logo FROM company WHERE id=1"
        ).fetchone()
        if row is None:
            return {"id": 1, "name": None, "org_nr": None, "vat_nr": None,
                    "address": None, "email": None, "phone": None, "f_skatt": 1,
                    "has_logo": False}
        d = dict(row)
        d["has_logo"] = bool(d["has_logo"])
        return d

    def set_logo(self, image_bytes: bytes) -> None:
        """
        Store the book's logo (used on every document). Any common format the phone
        or PC can produce (PNG/JPG/WEBP/…) is normalised to a size-bounded PNG via
        Pillow, then AES-256-GCM-encrypted with the DEK and stored in the company row
        (so it travels in the .buyn bundle). Replaces any existing logo.
        """
        import io
        from PIL import Image
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
        except Exception as exc:
            raise ValueError("Unsupported or corrupt image") from exc
        img = img.convert("RGBA")
        max_px = 600
        if max(img.size) > max_px:
            img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        enc = self.session.encrypt_blob(buf.getvalue())
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO company(id) VALUES (1)")
            self.conn.execute("UPDATE company SET logo_enc=?, updated_at=? WHERE id=1",
                              (enc, _now()))

    def get_logo(self) -> Optional[tuple[bytes, str]]:
        """Return (png_bytes, 'image/png') for the book's logo, or None if unset."""
        row = self.conn.execute("SELECT logo_enc FROM company WHERE id=1").fetchone()
        if row is None or row["logo_enc"] is None:
            return None
        return self.session.decrypt_blob(row["logo_enc"]), "image/png"

    def delete_logo(self) -> None:
        with self.conn:
            self.conn.execute("UPDATE company SET logo_enc=NULL, updated_at=? WHERE id=1", (_now(),))

    def set_company(self, **fields) -> None:
        allowed = ("name", "org_nr", "vat_nr", "address", "email", "phone", "f_skatt")
        data = {k: v for k, v in fields.items() if k in allowed}
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO company(id) VALUES (1)")
            if data:
                sets = ", ".join(f"{k}=?" for k in data)
                self.conn.execute(f"UPDATE company SET {sets}, updated_at=? WHERE id=1",
                                  (*data.values(), _now()))

    def list_payment_methods(self, active_only: bool = False) -> list[dict]:
        sql = ("SELECT id, label, value, sort_order, active FROM payment_method "
               + ("WHERE active=1 " if active_only else "") + "ORDER BY sort_order, id")
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def create_payment_method(self, label: str, value: str, sort_order: int = 0) -> int:
        if not label or not value:
            raise ValueError("Payment method needs a label and a value")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO payment_method(label, value, sort_order) VALUES (?,?,?)",
                (label, value, sort_order))
        return cur.lastrowid

    def update_payment_method(self, payment_method_id: int, **fields) -> None:
        allowed = ("label", "value", "sort_order", "active")
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        sets = ", ".join(f"{k}=?" for k in data)
        with self.conn:
            self.conn.execute(f"UPDATE payment_method SET {sets} WHERE id=?",
                              (*data.values(), payment_method_id))

    # ==================================================================
    # Invoices (faktura) — issued as a pending income; numbered sequentially
    # ==================================================================

    def create_invoice(self, *, customer_id: int, category_id: int,
                       invoice_date: str, due_date: str, lines: list[dict],
                       recipients: Optional[list[dict]] = None,
                       delivery_date: Optional[str] = None,
                       payment_terms: Optional[str] = None,
                       our_reference: Optional[str] = None,
                       your_reference: Optional[str] = None,
                       note: Optional[str] = None) -> dict:
        """
        Issue a faktura: compute the article lines, snapshot the buyer/seller/payment
        methods, split RUT across household recipients, and create the underlying
        PENDING income (so the existing pending->paid booking and the reports apply).
        Assigns the next unbroken invoice_number. Returns a summary dict.
        """
        customer = self.get_customer(customer_id)
        self._check_category(category_id, "income")
        recipients = recipients or []
        if not lines:
            raise ValueError("An invoice needs at least one article line")

        # 1) Per-line figures + aggregate the moms lines by rate (unit price is ex-moms).
        computed, agg = [], {}
        for i, ln in enumerate(lines, start=1):
            rate_code = ln["rate_code"]
            if rate_code not in S.MOMS_RATES:
                raise ValueError(f"Unknown moms rate {rate_code!r}")
            qty_centi = int(ln["quantity_centi"])
            unit_price_ore = int(ln["unit_price_ore"])
            ex = round(qty_centi * unit_price_ore / 100)
            _, moms, _ = compute_moms_figures(ex, rate_code, inclusive=False)
            computed.append({
                "line_no": i, "description": ln["description"], "quantity_centi": qty_centi,
                "unit": ln.get("unit"), "unit_price_ore": unit_price_ore, "rate_code": rate_code,
                "rut_eligible": 1 if ln.get("rut_eligible") else 0,
                "ex_moms_ore": ex, "moms_ore": moms,
            })
            agg[rate_code] = agg.get(rate_code, 0) + ex
        moms_lines = [{"rate_code": rc, "amount_ore": ex, "inclusive": False}
                      for rc, ex in agg.items()]

        # 2) RUT recipients (household split) — validate name + personnummer.
        rut_total = 0
        clean_recipients = []
        for r in recipients:
            if not r.get("first_name") or not r.get("last_name"):
                raise ValueError("Each RUT recipient needs a first and last name")
            if not S.is_valid_personnummer(r["personnummer"]):
                raise ValueError("RUT recipient has an invalid personnummer")
            amount = int(r["rut_amount_ore"])
            rut_total += amount
            clean_recipients.append({
                "first_name": r["first_name"], "last_name": r["last_name"],
                "personnummer": S.normalize_personnummer(r["personnummer"]),
                "rut_amount_ore": amount,
            })

        # 3) Underlying pending income (snapshot + moms_lines + rut_claim + reports).
        income = self.record_income(customer_id, category_id, moms_lines, invoice_date,
                                    rut_amount_ore=rut_total, note=note)
        tid = income["transaktion_id"]
        ex_total, _, inc_total = self._sum_moms(tid)
        moms_total = inc_total - ex_total

        # 3b) Fakturametod: book the invoice NOW (kundfordran/income/moms at issue).
        # The transaktion's verifikation becomes this issue posting, so the moms is
        # reported in the invoice's period; payment later only settles the receivable.
        # Kontantmetod leaves it pending (booked when paid + year-end accrual).
        if self.get_accounting_method() == "fakturametod":
            self._book_invoice_issue(tid, invoice_date, rut_total)

        # 4) Frozen snapshots + the sequential invoice number.
        buyer_snapshot_enc = self.session.encrypt_text(json.dumps(customer, default=str))
        seller_snapshot = json.dumps(self.get_company(), default=str)
        pm_snapshot = json.dumps(self.list_payment_methods(active_only=True), default=str)
        number = self._next_invoice_number()

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO invoice(invoice_number, customer_id, transaktion_id, invoice_date, "
                "due_date, delivery_date, payment_terms, buyer_snapshot_enc, seller_snapshot, "
                "payment_methods_snapshot, our_reference, your_reference, note, ex_moms_ore, "
                "moms_ore, inc_moms_ore, rut_total_ore, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (number, customer_id, tid, invoice_date, due_date, delivery_date, payment_terms,
                 buyer_snapshot_enc, seller_snapshot, pm_snapshot, our_reference, your_reference,
                 note, ex_total, moms_total, inc_total, rut_total, _now()))
            invoice_id = cur.lastrowid
            for cl in computed:
                self.conn.execute(
                    "INSERT INTO invoice_line(invoice_id, line_no, description, quantity_centi, "
                    "unit, unit_price_ore, rate_code, rut_eligible, ex_moms_ore, moms_ore) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (invoice_id, cl["line_no"], cl["description"], cl["quantity_centi"],
                     cl["unit"], cl["unit_price_ore"], cl["rate_code"], cl["rut_eligible"],
                     cl["ex_moms_ore"], cl["moms_ore"]))
            for r in clean_recipients:
                self.conn.execute(
                    "INSERT INTO rut_recipient(invoice_id, first_name, last_name, "
                    "personnummer_enc, rut_amount_ore) VALUES (?,?,?,?,?)",
                    (invoice_id, r["first_name"], r["last_name"],
                     self.session.encrypt_text(r["personnummer"]), r["rut_amount_ore"]))

        return {"invoice_id": invoice_id, "invoice_number": number, "transaktion_id": tid,
                "ex_moms_ore": ex_total, "moms_ore": moms_total, "inc_moms_ore": inc_total,
                "rut_total_ore": rut_total}

    def _invoice_balances(self, invoice_id: int) -> dict:
        """
        Derive an invoice's running settlement from the event subledger.

        customer_due = (inc − RUT) − credits   (what the customer should pay)
        paid         = payments − refunds
        outstanding  = customer_due − paid
        """
        inv = self.conn.execute(
            "SELECT inc_moms_ore, rut_total_ore, cancelled_at, credited_at, transaktion_id "
            "FROM invoice WHERE id=?", (invoice_id,)).fetchone()
        ev = {r["kind"]: r["s"] for r in self.conn.execute(
            "SELECT kind, COALESCE(SUM(amount_ore),0) AS s FROM invoice_event "
            "WHERE invoice_id=? GROUP BY kind", (invoice_id,))}
        payments, refunds, credits = ev.get("payment", 0), ev.get("refund", 0), ev.get("credit", 0)
        has_events = bool(ev)
        tx_status = None
        if inv["transaktion_id"]:
            row = self.conn.execute("SELECT status FROM transaktion WHERE id=?",
                                    (inv["transaktion_id"],)).fetchone()
            tx_status = row["status"] if row else None
        customer_total = inv["inc_moms_ore"] - inv["rut_total_ore"]
        customer_due = customer_total - credits
        paid = payments - refunds
        # Legacy/RUT path: paid in full via register_payment (no subledger events).
        if not has_events and tx_status == "paid":
            paid = customer_due
        return {
            "inc_moms_ore": inv["inc_moms_ore"], "rut_total_ore": inv["rut_total_ore"],
            "customer_total_ore": customer_total, "credited_ore": credits,
            "customer_due_ore": customer_due, "paid_ore": payments - refunds,
            "refunded_ore": refunds,
            "outstanding_ore": customer_due - paid,
            "state": self._invoice_state(inv, customer_total, credits, paid),
        }

    @staticmethod
    def _invoice_state(inv_row, customer_total, credits, paid) -> str:
        if inv_row["cancelled_at"]:
            return "cancelled"
        if inv_row["credited_at"] or (customer_total > 0 and credits >= customer_total):
            return "credited"
        if paid <= 0:
            return "pending"
        if paid >= customer_total - credits:
            return "paid"
        return "partial"

    def list_invoices(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, invoice_number, invoice_date, due_date, customer_id, transaktion_id, "
            "inc_moms_ore, rut_total_ore FROM invoice ORDER BY invoice_number").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.update(self._invoice_balances(r["id"]))
            d["credit_notes"] = [dict(c) for c in self.conn.execute(
                "SELECT id, credit_note_number, date, amount_ore FROM invoice_event "
                "WHERE invoice_id=? AND kind='credit' AND credit_note_number IS NOT NULL "
                "ORDER BY id", (r["id"],)).fetchall()]
            out.append(d)
        return out

    def get_invoice(self, invoice_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM invoice WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise KeyError(f"No invoice {invoice_id}")
        inv = dict(row)
        status_row = self.conn.execute(
            "SELECT status, payment_date FROM transaktion WHERE id=?",
            (inv["transaktion_id"],)).fetchone()
        inv["status"] = status_row["status"] if status_row else None
        inv["payment_date"] = status_row["payment_date"] if status_row else None
        inv.update(self._invoice_balances(invoice_id))
        inv["events"] = [dict(r) for r in self.conn.execute(
            "SELECT id, kind, amount_ore, date, verifikation_id, credit_note_number, note "
            "FROM invoice_event WHERE invoice_id=? ORDER BY date, id", (invoice_id,)).fetchall()]
        inv["buyer"] = json.loads(self.session.decrypt_text(inv.pop("buyer_snapshot_enc")))
        inv["seller"] = json.loads(inv.pop("seller_snapshot") or "{}")
        inv["payment_methods"] = json.loads(inv.pop("payment_methods_snapshot") or "[]")
        inv["lines"] = [dict(r) for r in self.conn.execute(
            "SELECT line_no, description, quantity_centi, unit, unit_price_ore, rate_code, "
            "rut_eligible, ex_moms_ore, moms_ore FROM invoice_line WHERE invoice_id=? "
            "ORDER BY line_no", (invoice_id,)).fetchall()]
        inv["recipients"] = [{
            "first_name": r["first_name"], "last_name": r["last_name"],
            "personnummer": self.session.decrypt_text(r["personnummer_enc"]),
            "rut_amount_ore": r["rut_amount_ore"],
        } for r in self.conn.execute(
            "SELECT first_name, last_name, personnummer_enc, rut_amount_ore "
            "FROM rut_recipient WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()]
        return inv

    def cancel_invoice(self, invoice_id: int) -> dict:
        """
        Makulera (void) an UNBOOKED invoice. Only allowed while the underlying income
        is still pending (kontantmetod: nothing has hit the ledger). The pending
        transaktion + its moms_lines/rut_claim are removed so it is no longer payable;
        the invoice_number stays reserved (a gap-free series) and the invoice row is
        kept and flagged makulerad. A booked invoice must be credited instead.
        """
        inv = self.conn.execute(
            "SELECT transaktion_id, cancelled_at, credited_at FROM invoice WHERE id=?",
            (invoice_id,)).fetchone()
        if inv is None:
            raise KeyError(f"No invoice {invoice_id}")
        if inv["cancelled_at"] or inv["credited_at"]:
            raise InvalidState("Invoice is already makulerad/krediterad")
        tid = inv["transaktion_id"]
        t = self.conn.execute("SELECT verifikation_id FROM transaktion WHERE id=?",
                              (tid,)).fetchone() if tid else None
        if t and t["verifikation_id"] is not None:
            raise InvalidState("Invoice is booked (paid); kreditera it instead of makulera")
        if self.conn.execute("SELECT 1 FROM invoice_event WHERE invoice_id=? LIMIT 1",
                             (invoice_id,)).fetchone():
            raise InvalidState("Invoice has payments/credits; kreditera/återbetala it instead")
        with self.conn:
            # Detach the invoice first so deleting the transaktion can't trip the FK.
            self.conn.execute(
                "UPDATE invoice SET cancelled_at=?, transaktion_id=NULL WHERE id=?",
                (_now(), invoice_id))
            if tid:
                self.conn.execute("DELETE FROM moms_line WHERE transaktion_id=?", (tid,))
                self.conn.execute("DELETE FROM rut_claim WHERE transaktion_id=?", (tid,))
                self.conn.execute("DELETE FROM transaktion WHERE id=?", (tid,))
        return {"invoice_id": invoice_id, "cancelled": True}

    # ---- settlement subledger: partial payments / refunds / credits ----------

    def pay_invoice(self, invoice_id: int, amount_ore: Optional[int] = None,
                    date: Optional[str] = None) -> dict:
        """
        Register a customer payment against an invoice (partial or full). Books the
        cash and records an invoice_event; the invoice's outstanding balance + state
        follow from the events. `amount_ore` defaults to the full outstanding amount.
        RUT invoices use the full register_payment + Skatteverket flow instead.
        """
        inv, bal = self._require_open_invoice(invoice_id)
        if inv["rut_total_ore"]:
            raise InvalidState("RUT invoices: use the full payment + Skatteverket flow")
        amount = bal["outstanding_ore"] if amount_ore is None else int(amount_ore)
        if amount <= 0:
            raise ValueError("Payment amount must be > 0")
        if amount > bal["outstanding_ore"]:
            raise InvalidState("Payment exceeds the outstanding amount")
        date = date or _now()[:10]
        tid = inv["transaktion_id"]
        text = f"Betalning faktura {inv['invoice_number']}"

        if self.get_accounting_method() == "fakturametod":
            postings = [(self._sys_account("account_bank"), amount, "inbetalning"),
                        (self._sys_account("account_kundfordran"), -amount, "kvitta kundfordran")]
            with self.conn:
                vid, num = self._post_verifikation(date, date, text, postings)
                self._record_invoice_event(invoice_id, "payment", amount, date, vid)
                self._sync_invoice_paid(invoice_id, tid, date)
        else:  # kontantmetod: recognise income + moms proportionally as cash arrives
            vid, num = self._book_kontant_recognition(tid, bal["paid_ore"], amount,
                                                      inv["inc_moms_ore"], date, +1, text)
            with self.conn:
                self._record_invoice_event(invoice_id, "payment", amount, date, vid)
                self._sync_invoice_paid(invoice_id, tid, date)
        return {"invoice_id": invoice_id, "verifikation_id": vid, "ver_number": num,
                "amount_ore": amount, "outstanding_ore": self._invoice_balances(invoice_id)["outstanding_ore"]}

    def refund_invoice(self, invoice_id: int, amount_ore: int,
                       date: Optional[str] = None, note: Optional[str] = None) -> dict:
        """Pay money back to the customer (full or partial) — the reverse of a payment."""
        inv, bal = self._require_open_invoice(invoice_id)
        if inv["rut_total_ore"]:
            raise InvalidState("RUT invoices: refunds not supported via the subledger")
        amount = int(amount_ore)
        if amount <= 0:
            raise ValueError("Refund amount must be > 0")
        if amount > bal["paid_ore"]:
            raise InvalidState("Refund exceeds what has been paid")
        date = date or _now()[:10]
        tid = inv["transaktion_id"]
        text = f"Återbetalning faktura {inv['invoice_number']}"

        if self.get_accounting_method() == "fakturametod":
            postings = [(self._sys_account("account_bank"), -amount, "återbetalning"),
                        (self._sys_account("account_kundfordran"), amount, "återställd kundfordran")]
            with self.conn:
                vid, num = self._post_verifikation(date, date, text, postings)
                self._record_invoice_event(invoice_id, "refund", amount, date, vid, note)
                self._sync_invoice_paid(invoice_id, tid, date)
        else:  # kontantmetod: de-recognise the income/moms slice being refunded
            vid, num = self._book_kontant_recognition(tid, bal["paid_ore"] - amount, amount,
                                                      inv["inc_moms_ore"], date, -1, text)
            with self.conn:
                self._record_invoice_event(invoice_id, "refund", amount, date, vid, note)
                self._sync_invoice_paid(invoice_id, tid, date)
        return {"invoice_id": invoice_id, "verifikation_id": vid, "ver_number": num,
                "amount_ore": amount}

    def credit_invoice(self, invoice_id: int, amount_ore: Optional[int] = None,
                       reason: Optional[str] = None, date: Optional[str] = None) -> dict:
        """
        Kreditera an invoice (full or partial). Reverses income + moms for the credited
        slice and lowers the customer receivable (1510) — so a credit on an already-paid
        invoice makes the receivable negative, i.e. money owed back, which `refund_invoice`
        then pays out. A fully credited invoice is flagged krediterad.
        """
        inv, bal = self._require_open_invoice(invoice_id)
        if inv["rut_total_ore"]:
            raise InvalidState("RUT invoices: partial credit not supported; use makulera/full flow")
        billable = inv["inc_moms_ore"] - inv["rut_total_ore"] - bal["credited_ore"]
        amount = billable if amount_ore is None else int(amount_ore)
        if amount <= 0:
            raise ValueError("Credit amount must be > 0")
        if amount > billable:
            raise InvalidState("Credit exceeds the invoice amount")
        # Kontantmetod recognises income only as cash arrives, so there is nothing to
        # reverse beyond what has been paid; void an unpaid part with makulera instead.
        if self.get_accounting_method() == "kontantmetod" and amount > bal["paid_ore"]:
            raise InvalidState(
                "Kontantmetod: credit cannot exceed the paid amount (makulera the unpaid part)")
        date = date or _now()[:10]
        tid = inv["transaktion_id"]
        text = f"Kreditering faktura {inv['invoice_number']}" + (f": {reason}" if reason else "")

        # Reverse the proportional income/moms slice, balanced against the receivable.
        slices = self._recognition_slice(tid, bal["credited_ore"], amount, inv["inc_moms_ore"])
        konto = self._category_konto(
            self.conn.execute("SELECT category_id FROM transaktion WHERE id=?", (tid,)).fetchone()["category_id"])
        sum_ex = sum(ex for ex, _ in slices.values())
        postings = [(konto, sum_ex, "kreditering försäljning")]
        for rate_code, (ex_s, moms_s) in slices.items():
            if moms_s and rate_code in _UTG_MOMS_KEY:
                postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), moms_s,
                                 f"kreditering moms {rate_code}%"))
        postings.append((self._sys_account("account_kundfordran"), -amount, "minskad kundfordran"))
        with self.conn:
            vid, num = self._post_verifikation(date, date, text, postings)
            # negative report-clone so the momsdeklaration/result net the credit
            self._book_recognition_clone(tid, vid, date, slices, -1, "kreditering")
            credit_note_number = self._next_invoice_number()
            ev_id = self._record_invoice_event(invoice_id, "credit", amount, date, vid, reason,
                                               credit_note_number)
            fully = bal["credited_ore"] + amount >= inv["inc_moms_ore"] - inv["rut_total_ore"]
            if fully:
                self.conn.execute(
                    "UPDATE invoice SET credited_at=?, credit_verifikation_id=? WHERE id=?",
                    (_now(), vid, invoice_id))
        return {"invoice_id": invoice_id, "verifikation_id": vid, "ver_number": num,
                "amount_ore": amount, "credited": True, "credit_note_number": credit_note_number,
                "credit_event_id": ev_id}

    def _next_invoice_number(self) -> int:
        """Next number in the faktura series, shared by invoices and credit notes
        (so every issued document has a unique, unbroken number)."""
        a = self.conn.execute("SELECT COALESCE(MAX(invoice_number), 0) FROM invoice").fetchone()[0]
        b = self.conn.execute(
            "SELECT COALESCE(MAX(credit_note_number), 0) FROM invoice_event").fetchone()[0]
        return max(a, b) + 1

    def get_credit_note(self, invoice_id: int, event_id: int) -> dict:
        """Build a render dict for a kreditfaktura: the original invoice's frozen
        buyer/seller/payment snapshots, a reference to the original number, and the
        credited slice as NEGATIVE line(s)."""
        ev = self.conn.execute(
            "SELECT id, amount_ore, date, credit_note_number, note FROM invoice_event "
            "WHERE id=? AND invoice_id=? AND kind='credit'", (event_id, invoice_id)).fetchone()
        if ev is None:
            raise KeyError(f"No credit note {event_id} on invoice {invoice_id}")
        orig = self.get_invoice(invoice_id)
        credited_before = self.conn.execute(
            "SELECT COALESCE(SUM(amount_ore),0) FROM invoice_event "
            "WHERE invoice_id=? AND kind='credit' AND id<?", (invoice_id, event_id)).fetchone()[0]
        slices = self._recognition_slice(orig["transaktion_id"], credited_before,
                                         ev["amount_ore"], orig["inc_moms_ore"])
        lines, ex_total, moms_total = [], 0, 0
        for rate_code, (ex_s, moms_s) in slices.items():
            if ex_s or moms_s:
                lines.append({"line_no": len(lines) + 1,
                              "description": f"Kreditering avseende faktura {orig['invoice_number']}",
                              "quantity_centi": 100, "unit": None, "unit_price_ore": -ex_s,
                              "rate_code": rate_code, "rut_eligible": 0,
                              "ex_moms_ore": -ex_s, "moms_ore": -moms_s})
                ex_total -= ex_s
                moms_total -= moms_s
        return {
            "invoice_number": ev["credit_note_number"], "invoice_date": ev["date"],
            "due_date": None, "delivery_date": None,
            "buyer": orig["buyer"], "seller": orig["seller"],
            "payment_methods": [], "payment_terms": None,
            "our_reference": orig.get("our_reference"), "your_reference": orig.get("your_reference"),
            "note": ev["note"], "lines": lines, "recipients": [],
            "ex_moms_ore": ex_total, "moms_ore": moms_total, "inc_moms_ore": -ev["amount_ore"],
            "rut_total_ore": 0, "credit_of": orig["invoice_number"],
        }

    # ---- subledger internals -------------------------------------------------

    def _require_open_invoice(self, invoice_id: int) -> tuple:
        inv = self.conn.execute(
            "SELECT id, invoice_number, transaktion_id, inc_moms_ore, rut_total_ore, "
            "cancelled_at FROM invoice WHERE id=?", (invoice_id,)).fetchone()
        if inv is None:
            raise KeyError(f"No invoice {invoice_id}")
        if inv["cancelled_at"]:
            raise InvalidState("Invoice is makulerad")
        return inv, self._invoice_balances(invoice_id)

    def _record_invoice_event(self, invoice_id, kind, amount, date, vid, note=None,
                              credit_note_number=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO invoice_event(invoice_id, kind, amount_ore, date, verifikation_id, "
            "credit_note_number, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (invoice_id, kind, amount, date, vid, credit_note_number, note, _now()))
        return cur.lastrowid

    def _sync_invoice_paid(self, invoice_id, transaktion_id, date) -> None:
        """Mark the underlying transaktion paid once the invoice is fully settled."""
        bal = self._invoice_balances(invoice_id)
        if transaktion_id and bal["outstanding_ore"] <= 0:
            self.conn.execute("UPDATE transaktion SET status='paid', payment_date=? WHERE id=?",
                              (date, transaktion_id))
        elif transaktion_id:
            self.conn.execute("UPDATE transaktion SET status='pending' WHERE id=?", (transaktion_id,))

    def _recognition_slice(self, transaktion_id, recognized_before, amount, total_inc) -> dict:
        """Per-rate (ex, moms) to recognise for `amount` of cash, using cumulative
        rounding so a sequence of partials reconciles exactly to the öre."""
        before, after = recognized_before, recognized_before + amount
        out = {}
        for ln in self.conn.execute(
                "SELECT rate_code, ex_moms_ore, moms_ore FROM moms_line WHERE transaktion_id=?",
                (transaktion_id,)):
            ex_s = (round(ln["ex_moms_ore"] * after / total_inc)
                    - round(ln["ex_moms_ore"] * before / total_inc))
            moms_s = (round(ln["moms_ore"] * after / total_inc)
                      - round(ln["moms_ore"] * before / total_inc))
            out[ln["rate_code"]] = (ex_s, moms_s)
        return out

    def _book_recognition_clone(self, src_transaktion_id, vid, date, slices, sign, note) -> None:
        """Synthetic transaktion + sliced moms_lines linked to `vid` so the moms/result
        reports attribute this slice to `vid`'s date (hidden from the Transaktioner list)."""
        src = self.conn.execute(
            "SELECT direction, category_id, supplier_id, customer_id FROM transaktion WHERE id=?",
            (src_transaktion_id,)).fetchone()
        cur = self.conn.execute(
            "INSERT INTO transaktion(direction, category_id, supplier_id, customer_id, trans_date, "
            "status, verifikation_id, note, created_at) VALUES (?,?,?,?,?, 'paid', ?, ?, ?)",
            (src["direction"], src["category_id"], src["supplier_id"], src["customer_id"],
             date, vid, note, _now()))
        rid = cur.lastrowid
        for rate_code, (ex_s, moms_s) in slices.items():
            if ex_s or moms_s:
                self.conn.execute(
                    "INSERT INTO moms_line(transaktion_id, rate_code, ex_moms_ore, moms_ore, "
                    "inc_moms_ore) VALUES (?,?,?,?,?)",
                    (rid, rate_code, sign * ex_s, sign * moms_s, sign * (ex_s + moms_s)))

    def _book_kontant_recognition(self, transaktion_id, recognized_before, amount, total_inc,
                                  date, sign, text) -> tuple[int, int]:
        """Kontantmetod: book bank +/- amount against income + moms recognised for the
        proportional slice, plus the report-clone. Returns (verifikation_id, number)."""
        slices = self._recognition_slice(transaktion_id, recognized_before, amount, total_inc)
        konto = self._category_konto(
            self.conn.execute("SELECT category_id FROM transaktion WHERE id=?",
                              (transaktion_id,)).fetchone()["category_id"])
        sum_ex = sum(ex for ex, _ in slices.values())
        postings = [(self._sys_account("account_bank"), sign * amount, "inbetalning"),
                    (konto, -sign * sum_ex, "försäljning")]
        for rate_code, (ex_s, moms_s) in slices.items():
            if moms_s and rate_code in _UTG_MOMS_KEY:
                postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -sign * moms_s,
                                 f"utgående moms {rate_code}%"))
        with self.conn:
            vid, num = self._post_verifikation(date, date, text, postings)
            self._book_recognition_clone(transaktion_id, vid, date, slices, sign, "fakturabetalning")
        return vid, num

    def _book_invoice_issue(self, transaktion_id: int, ver_date: str, rut_ore: int) -> tuple[int, int]:
        """
        Fakturametod issue posting for a sale: debit the receivable(s) and credit the
        income + utgående moms, dated `ver_date`, and set it as the transaktion's
        verifikation (so the moms is reported in the invoice's period). The later
        payment settles the receivable. Must be a sale (direction 'out').
        """
        t = self.conn.execute(
            "SELECT category_id FROM transaktion WHERE id=?", (transaktion_id,)).fetchone()
        ex, moms_by_rate, inc = self._sum_moms(transaktion_id)
        konto = self._category_konto(t["category_id"])
        cust_part = inc - rut_ore
        postings = []
        if cust_part:
            postings.append((self._sys_account("account_kundfordran"), cust_part, "kundfordran"))
        if rut_ore:
            postings.append((self._sys_account("account_rut_fordran"), rut_ore, "husavdrag fordran"))
        postings.append((konto, -ex, "försäljning"))
        for rate_code, m in moms_by_rate.items():
            if m and rate_code in _UTG_MOMS_KEY:
                postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -m,
                                 f"utgående moms {rate_code}%"))
        with self.conn:
            vid, number = self._post_verifikation(ver_date, ver_date, "Faktura (kundfordran)", postings)
            self.conn.execute("UPDATE transaktion SET verifikation_id=? WHERE id=?",
                              (vid, transaktion_id))
        return vid, number

    # ==================================================================
    # Receipts (encrypted photos, stored as files beside the db)
    # ==================================================================

    def _photos_dir(self) -> Path:
        from backend.db import bundle  # local import avoids a circular dependency
        return bundle._photos_dir(Path(self.session.record.db_path))

    def attach_receipt(self, transaktion_id: int, data: bytes, mime: str,
                       original_format: Optional[str] = None) -> dict:
        """
        Store a receipt photo for a transaktion: the bytes are encrypted with the
        book DEK and written as a file in `<db>.photos/`; a `receipt` row indexes it.
        The plaintext never touches disk. Returns the new receipt's metadata.
        """
        if self.conn.execute(
            "SELECT 1 FROM transaktion WHERE id=?", (transaktion_id,)
        ).fetchone() is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if original_format is not None and original_format not in S.RECEIPT_FORMATS:
            raise ValueError(f"Invalid receipt format: {original_format}")

        enc = self.session.encrypt_blob(data)
        filename = f"{uuid.uuid4().hex}.bin"
        photos = self._photos_dir()
        photos.mkdir(parents=True, exist_ok=True)
        (photos / filename).write_bytes(enc)

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO receipt(transaktion_id, filename, mime, original_format, "
                "byte_size, sha256, created_at) VALUES (?,?,?,?,?,?,?)",
                (transaktion_id, filename, mime, original_format,
                 len(data), hashlib.sha256(enc).hexdigest(), _now()),
            )
        return {"id": cur.lastrowid, "transaktion_id": transaktion_id,
                "filename": filename, "mime": mime, "byte_size": len(data)}

    def list_receipts(self, transaktion_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, transaktion_id, mime, original_format, byte_size, created_at "
            "FROM receipt WHERE transaktion_id=? ORDER BY id",
            (transaktion_id,),
        ).fetchall()]

    def get_receipt(self, receipt_id: int) -> tuple[bytes, str]:
        """Return (plaintext_bytes, mime) for a stored receipt, verifying integrity."""
        row = self.conn.execute(
            "SELECT filename, mime, sha256 FROM receipt WHERE id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No receipt {receipt_id}")
        enc = (self._photos_dir() / row["filename"]).read_bytes()
        if hashlib.sha256(enc).hexdigest() != row["sha256"]:
            raise OperationError(f"Receipt {receipt_id} failed integrity check")
        return self.session.decrypt_blob(enc), row["mime"]

    def delete_receipt(self, receipt_id: int) -> None:
        """
        Remove a receipt — allowed ONLY while its transaktion is still pending
        (not yet booked into a verifikation). Once posted the receipt is part of the
        immutable legal record.
        """
        row = self.conn.execute(
            "SELECT r.filename, t.verifikation_id FROM receipt r "
            "JOIN transaktion t ON t.id = r.transaktion_id WHERE r.id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No receipt {receipt_id}")
        if row["verifikation_id"] is not None:
            raise InvalidState("Cannot delete a receipt once its transaktion is booked")
        with self.conn:
            self.conn.execute("DELETE FROM receipt WHERE id=?", (receipt_id,))
        f = self._photos_dir() / row["filename"]
        if f.exists():
            f.unlink()

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _config(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        if row is None:
            raise OperationError(f"Missing config key: {key}")
        return row[0]

    def _sys_account(self, config_key: str) -> int:
        """Resolve a configured system BAS-konto, creating the row if needed."""
        n = int(self._config(config_key))
        self.ensure_account(n, _SYS_ACCOUNT_NAMES.get(config_key, f"Konto {n}"))
        return n

    def _check_category(self, category_id: int, expected_kind: str) -> None:
        row = self.conn.execute(
            "SELECT kind FROM category WHERE id=?", (category_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No category {category_id}")
        if row["kind"] != expected_kind:
            raise ValueError(f"Category {category_id} is '{row['kind']}', expected '{expected_kind}'")

    def _category_konto(self, category_id: int) -> int:
        row = self.conn.execute(
            "SELECT bas_konto FROM category WHERE id=?", (category_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No category {category_id}")
        return row["bas_konto"]

    def _clone_transaktion_for_report(self, src_transaktion_id: int, new_ver_id: int,
                                      ver_date: str, sign: int, note: str) -> Optional[int]:
        """
        Create a synthetic transaktion (linked to new_ver_id) carrying sign-scaled
        copies of src's moms_lines, so the moms/result reports attribute the
        correction/accrual to new_ver_id's period. MUST run inside `with self.conn`.
        """
        src = self.conn.execute(
            "SELECT direction, category_id, supplier_id, customer_id FROM transaktion WHERE id=?",
            (src_transaktion_id,),
        ).fetchone()
        lines = self.conn.execute(
            "SELECT rate_code, ex_moms_ore, moms_ore, inc_moms_ore FROM moms_line "
            "WHERE transaktion_id=?", (src_transaktion_id,),
        ).fetchall()
        if src is None or not lines:
            return None
        cur = self.conn.execute(
            "INSERT INTO transaktion(direction, category_id, supplier_id, customer_id, "
            "trans_date, status, verifikation_id, note, created_at) "
            "VALUES (?,?,?,?,?, 'paid', ?, ?, ?)",
            (src["direction"], src["category_id"], src["supplier_id"], src["customer_id"],
             ver_date, new_ver_id, note, _now()),
        )
        rid = cur.lastrowid
        for ln in lines:
            self.conn.execute(
                "INSERT INTO moms_line(transaktion_id, rate_code, ex_moms_ore, moms_ore, inc_moms_ore) "
                "VALUES (?,?,?,?,?)",
                (rid, ln["rate_code"], sign * ln["ex_moms_ore"],
                 sign * ln["moms_ore"], sign * ln["inc_moms_ore"]),
            )
        return rid

    def _insert_transaktion(self, *, direction, category_id, supplier_id, customer_id,
                            trans_date, note, receipt_original_format, snapshot_enc) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO transaktion(direction, category_id, supplier_id, customer_id, "
                "trans_date, status, customer_snapshot_enc, receipt_original_format, note, created_at) "
                "VALUES (?,?,?,?,?, 'pending', ?,?,?,?)",
                (direction, category_id, supplier_id, customer_id, trans_date,
                 snapshot_enc, receipt_original_format, note, _now()),
            )
        return cur.lastrowid

    def _insert_moms_lines(self, transaktion_id: int, lines: list[dict]) -> None:
        if not lines:
            raise ValueError("At least one moms line is required")
        with self.conn:
            for ln in lines:
                rate_code = ln["rate_code"]
                if rate_code not in S.MOMS_RATES:
                    raise ValueError(f"Unknown moms rate {rate_code!r}")
                ex, moms, inc = compute_moms_figures(
                    ln["amount_ore"], rate_code, ln.get("inclusive", True)
                )
                self.conn.execute(
                    "INSERT INTO moms_line(transaktion_id, rate_code, ex_moms_ore, "
                    "moms_ore, inc_moms_ore) VALUES (?,?,?,?,?)",
                    (transaktion_id, rate_code, ex, moms, inc),
                )

    def _insert_rut_claim(self, transaktion_id, customer_id, rut_amount_ore, year) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO rut_claim(transaktion_id, customer_id, rut_amount_ore, "
                "claim_year, created_at) VALUES (?,?,?,?,?)",
                (transaktion_id, customer_id, rut_amount_ore, year, _now()),
            )
        return cur.lastrowid

    def _sum_moms(self, transaktion_id: int) -> tuple[int, dict[str, int], int]:
        """Return (sum_ex, {rate_code: sum_moms}, sum_inc) for a transaktion."""
        rows = self.conn.execute(
            "SELECT rate_code, ex_moms_ore, moms_ore, inc_moms_ore FROM moms_line "
            "WHERE transaktion_id=?", (transaktion_id,),
        ).fetchall()
        ex = sum(r["ex_moms_ore"] for r in rows)
        inc = sum(r["inc_moms_ore"] for r in rows)
        moms_by_rate: dict[str, int] = {}
        for r in rows:
            moms_by_rate[r["rate_code"]] = moms_by_rate.get(r["rate_code"], 0) + r["moms_ore"]
        return ex, moms_by_rate, inc

    def _next_ver_number(self, series: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(ver_number) FROM verifikation WHERE series=?", (series,)
        ).fetchone()
        return (row[0] or 0) + 1

    def _post_verifikation(self, ver_date: str, reg_date: str, text: str,
                           postings: list[tuple[int, int, str | None]],
                           series: str = "A",
                           rattelse_of: Optional[int] = None) -> tuple[int, int]:
        """
        Insert a posted verifikation with balanced postings. Asserts the postings
        sum to zero and the period is open. MUST be called inside a `with self.conn`.
        Returns (verifikation_id, ver_number).
        """
        total = sum(amount for _, amount, _ in postings)
        if total != 0:
            raise ImbalancedPostings(f"Postings do not balance (sum={total} öre)")
        if self.is_period_locked(ver_date):
            raise PeriodLocked(f"Period containing {ver_date} is locked")

        number = self._next_ver_number(series)
        cur = self.conn.execute(
            "INSERT INTO verifikation(series, ver_number, ver_date, registration_date, "
            "text, posted, rattelse_of, created_at) VALUES (?,?,?,?,?,1,?,?)",
            (series, number, ver_date, reg_date, text, rattelse_of, _now()),
        )
        vid = cur.lastrowid
        for konto, amount, ptext in postings:
            self.conn.execute(
                "INSERT INTO posting(verifikation_id, bas_konto, amount_ore, text) "
                "VALUES (?,?,?,?)",
                (vid, konto, amount, ptext),
            )
        return vid, number


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _build_update(fields: dict) -> tuple[str, list]:
    """Build a 'col=?, col2=?' SET clause from non-None fields."""
    items = [(k, v) for k, v in fields.items() if v is not None]
    sets = ", ".join(f"{k}=?" for k, _ in items)
    params = [v for _, v in items]
    return sets, params
