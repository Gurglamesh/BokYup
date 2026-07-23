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
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
    "account_ores_kronutjamning": "Öres- och kronutjämning",
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


def _compose_address(street, zip_code, city, country) -> str:
    """Build a single-line display address from structured parts (zip + city on one
    line, country only if not Sweden)."""
    locality = " ".join(p for p in (str(zip_code).strip() if zip_code else "",
                                    str(city).strip() if city else "") if p)
    parts = [p for p in (street, locality) if p]
    if country and str(country).strip().lower() not in ("sverige", "sweden"):
        parts.append(str(country).strip())
    return ", ".join(parts)




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
                        account_name: Optional[str] = None,
                        default_rate_code: Optional[str] = None) -> int:
        """Create a category linked to a BAS-konto (auto-creating the account).

        `default_rate_code` is the moms rate the UI pre-fills for lines booked to this
        category (e.g. "Försäljning av IT-tjänster, 25 %"). Optional; validated."""
        if kind not in ("income", "expense"):
            raise ValueError("kind must be 'income' or 'expense'")
        if default_rate_code is not None and default_rate_code not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {default_rate_code!r}")
        self.ensure_account(bas_konto, account_name or name)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO category(name, kind, bas_konto, default_rate_code, created_at) "
                "VALUES (?,?,?,?,?)",
                (name, kind, bas_konto, default_rate_code, _now()),
            )
        return cur.lastrowid

    def update_category(self, category_id: int, *, name: Optional[str] = None,
                        bas_konto: Optional[int] = None, active: Optional[bool] = None,
                        account_name: Optional[str] = None,
                        default_rate_code: Optional[str] = None) -> None:
        """Edit reference data freely. Does not touch already-booked verifikationer."""
        if bas_konto is not None:
            self.ensure_account(bas_konto, account_name or name or f"Konto {bas_konto}")
        if default_rate_code is not None and default_rate_code not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {default_rate_code!r}")
        sets, params = _build_update(
            {"name": name, "bas_konto": bas_konto, "default_rate_code": default_rate_code,
             "active": None if active is None else int(active)}
        )
        if sets:
            with self.conn:
                self.conn.execute(f"UPDATE category SET {sets} WHERE id=?", (*params, category_id))

    def category_in_use(self, category_id: int) -> bool:
        """Whether a category is referenced by any transaktion, moms-line or invoice
        line (i.e. it has touched the books and may no longer be deleted)."""
        return self.conn.execute(
            "SELECT 1 FROM transaktion WHERE category_id=? "
            "UNION ALL SELECT 1 FROM moms_line WHERE category_id=? "
            "UNION ALL SELECT 1 FROM invoice_line WHERE category_id=? LIMIT 1",
            (category_id, category_id, category_id)).fetchone() is not None

    def delete_category(self, category_id: int) -> dict:
        """
        Delete a category (BAS-konto) that has NOT been used in the books yet.

        Reference data is freely editable, but once a category has been booked it is
        part of the legal record and must stay (inactivate it instead). A category
        that no transaktion/moms-line/invoice-line points at is safe to remove. If its
        BAS-konto is then orphaned (no other category, no postings, not a system
        konto) the chart-of-accounts row is cleaned up too.
        """
        row = self.conn.execute(
            "SELECT bas_konto FROM category WHERE id=?", (category_id,)).fetchone()
        if row is None:
            raise KeyError(f"No category {category_id}")
        if self.category_in_use(category_id):
            raise InvalidState(
                "Kategorin har använts i bokföringen och kan inte tas bort — inaktivera den istället")
        konto = row["bas_konto"]
        account_removed = False
        with self.conn:
            self.conn.execute("DELETE FROM category WHERE id=?", (category_id,))
            # Remove the BAS-konto too if nothing else references it (system konton stay).
            sys_nums = {int(self._config(k)) for k in _SYS_ACCOUNT_NAMES}
            still_used = self.conn.execute(
                "SELECT 1 FROM category WHERE bas_konto=? "
                "UNION ALL SELECT 1 FROM posting WHERE bas_konto=? LIMIT 1",
                (konto, konto)).fetchone()
            if not still_used and konto not in sys_nums:
                self.conn.execute("DELETE FROM account WHERE bas_konto=?", (konto,))
                account_removed = True
        return {"deleted": True, "bas_konto": konto, "account_removed": account_removed}

    # ==================================================================
    # Article catalog (reusable invoice line items)
    # ==================================================================

    def _next_article_number(self, prefix: str) -> str:
        """Build a unique article number `<prefix>-XXXX` (prefix = 4 digits the user
        picks; XXXX random). Retries on the rare collision."""
        if not (isinstance(prefix, str) and prefix.isdigit() and len(prefix) == 4):
            raise ValueError("Prefixet måste vara exakt 4 siffror")
        for _ in range(50):
            number = f"{prefix}-{secrets.randbelow(10000):04d}"
            if self.conn.execute("SELECT 1 FROM article WHERE article_number=?",
                                 (number,)).fetchone() is None:
                return number
        raise OperationError("Kunde inte skapa ett unikt artikelnummer")

    def create_article(self, description: str, prefix: str, *,
                       unit_price_ore: int = 0, rate_code: str = "25",
                       reduction_type: Optional[str] = None,
                       category_id: Optional[int] = None,
                       unit: Optional[str] = None) -> dict:
        """Create a catalog article. `prefix` is the user-chosen 4-digit number prefix."""
        if not description:
            raise ValueError("Artikeln behöver en beskrivning")
        if rate_code not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {rate_code!r}")
        if reduction_type not in (None, "rut", "rot"):
            raise ValueError(f"Unknown reduction_type {reduction_type!r}")
        if category_id is not None:
            self._check_category(category_id, "income")
        number = self._next_article_number(prefix)
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO article(article_number, description, unit, unit_price_ore, "
                "rate_code, reduction_type, category_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (number, description, unit, int(unit_price_ore), rate_code, reduction_type,
                 category_id, now, now))
        return {"id": cur.lastrowid, "article_number": number}

    def list_articles(self, active_only: bool = False) -> list[dict]:
        sql = ("SELECT a.id, a.article_number, a.description, a.unit, a.unit_price_ore, "
               "a.rate_code, a.reduction_type, a.category_id, c.name AS category_name, "
               "a.active FROM article a LEFT JOIN category c ON c.id = a.category_id ")
        if active_only:
            sql += "WHERE a.active = 1 "
        sql += "ORDER BY a.article_number"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def update_article(self, article_id: int, **fields) -> None:
        """Edit an article (e.g. categorise it, change price/moms, rename). Does not
        touch already-issued invoice lines (they carry their own frozen values)."""
        if self.conn.execute("SELECT 1 FROM article WHERE id=?", (article_id,)).fetchone() is None:
            raise KeyError(f"No article {article_id}")
        if fields.get("rate_code") is not None and fields["rate_code"] not in S.MOMS_RATES:
            raise ValueError("Unknown moms rate")
        if fields.get("category_id") is not None:
            self._check_category(fields["category_id"], "income")
        if "article_number" in fields and fields["article_number"]:
            num = fields["article_number"]
            clash = self.conn.execute(
                "SELECT 1 FROM article WHERE article_number=? AND id<>?",
                (num, article_id)).fetchone()
            if clash:
                raise InvalidState("Artikelnumret används redan")
        allowed = {k: fields[k] for k in
                   ("article_number", "description", "unit", "unit_price_ore", "rate_code",
                    "reduction_type", "category_id", "active") if k in fields}
        if "active" in allowed and allowed["active"] is not None:
            allowed["active"] = int(allowed["active"])
        allowed["updated_at"] = _now()
        sets, params = _build_update(allowed)
        if sets:
            with self.conn:
                self.conn.execute(f"UPDATE article SET {sets} WHERE id=?", (*params, article_id))

    def delete_article(self, article_id: int) -> None:
        """Delete a catalog article. Issued invoice lines keep their frozen values; any
        line that referenced it just loses the (informational) link."""
        if self.conn.execute("SELECT 1 FROM article WHERE id=?", (article_id,)).fetchone() is None:
            raise KeyError(f"No article {article_id}")
        with self.conn:
            self.conn.execute("UPDATE invoice_line SET article_id=NULL WHERE article_id=?",
                              (article_id,))
            self.conn.execute("DELETE FROM article WHERE id=?", (article_id,))

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
                  "contact_person", "vat_nr", "address", "shipping_address",
                  "street", "zip_code", "city", "country", "email", "phone"):
            if k in fields:
                cols[k] = fields[k]
        # Structured address defaults to Sverige and composes the legacy single-line
        # `address` (used by the invoice snapshot/PDF) when not given explicitly.
        if any(cols.get(k) for k in ("street", "zip_code", "city")):
            cols.setdefault("country", "Sverige")
            if not cols.get("country"):
                cols["country"] = "Sverige"
            if not cols.get("address"):
                cols["address"] = _compose_address(cols.get("street"), cols.get("zip_code"),
                                                   cols.get("city"), cols.get("country"))

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
                  "vat_nr", "address", "shipping_address", "street", "zip_code", "city",
                  "country", "email", "phone", "active"):
            if k in fields:
                updates[k] = fields[k]
        # Recompose the legacy single-line address when a structured part changed and
        # the caller didn't pass an explicit address.
        if any(k in updates for k in ("street", "zip_code", "city", "country")) \
                and "address" not in updates:
            cur = self.get_customer(kundnummer)
            cur.update(updates)
            updates["address"] = _compose_address(cur.get("street"), cur.get("zip_code"),
                                                  cur.get("city"), cur.get("country"))
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

    # ---- household relations (symmetric customer links, for RUT/ROT) ----------

    def link_customers(self, a: int, b: int) -> dict:
        """Create a symmetric household link between two customers (idempotent)."""
        a, b = int(a), int(b)
        if a == b:
            raise ValueError("Kan inte koppla en kund till sig själv")
        for k in (a, b):
            if self.conn.execute("SELECT 1 FROM customer WHERE kundnummer=?", (k,)).fetchone() is None:
                raise KeyError(f"No customer {k}")
        lo, hi = (a, b) if a < b else (b, a)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO customer_relation(customer_a, customer_b, created_at) "
                "VALUES (?,?,?)", (lo, hi, _now()))
        return {"customer_a": lo, "customer_b": hi}

    def unlink_customers(self, a: int, b: int) -> None:
        a, b = int(a), int(b)
        lo, hi = (a, b) if a < b else (b, a)
        with self.conn:
            self.conn.execute(
                "DELETE FROM customer_relation WHERE customer_a=? AND customer_b=?", (lo, hi))

    def list_related_customers(self, kundnummer: int) -> list[dict]:
        """Customers linked to `kundnummer` (household members), name + kundnummer."""
        rows = self.conn.execute(
            "SELECT c.kundnummer, c.type, c.first_name, c.last_name, c.company_name "
            "FROM customer_relation r "
            "JOIN customer c ON c.kundnummer = CASE WHEN r.customer_a=? THEN r.customer_b "
            "ELSE r.customer_a END "
            "WHERE r.customer_a=? OR r.customer_b=? ORDER BY c.kundnummer",
            (kundnummer, kundnummer, kundnummer)).fetchall()
        return [dict(r) for r in rows]

    def reduction_pcts(self) -> tuple[int, int]:
        """(rut_pct, rot_pct) skattereduktion percentages from config."""
        return int(self._config("rut_reduction_pct")), int(self._config("rot_reduction_pct"))

    def _resolve_recipient(self, r: dict, invoice_customer_id: int, single: bool) -> dict:
        """Normalise a recipient: resolve its linked customer + name + personnummer and
        its share percentage (centi). Personnummer may come from the request or the
        linked customer record."""
        cid = r.get("customer_id")
        pnr = r.get("personnummer")
        first, last = r.get("first_name"), r.get("last_name")
        if cid:
            cust = self.get_customer(int(cid))
            first = first or cust.get("first_name")
            last = last or cust.get("last_name")
            pnr = pnr or cust.get("personnummer")
            cid = int(cid)
        if not first or not last:
            raise ValueError("Varje mottagare behöver för- och efternamn")
        if not pnr or not S.is_valid_personnummer(pnr):
            raise ValueError("Mottagare har ogiltigt personnummer")
        # Separate RUT and ROT shares; `share_pct` is the shared fallback for both
        # (and the default 100 % for a single recipient).
        base = r.get("share_pct")
        if base is None and single:
            base = 100
        # A present-but-None key (e.g. from Pydantic) must fall back to `base`, so
        # coalesce explicitly rather than relying on dict.get's default.
        rut_share = r.get("rut_share_pct")
        rot_share = r.get("rot_share_pct")
        if rut_share is None:
            rut_share = base
        if rot_share is None:
            rot_share = base
        centi = lambda v: int(round(float(v) * 100)) if v is not None else 0
        rut_centi, rot_centi = centi(rut_share), centi(rot_share)
        if rut_centi <= 0 and rot_centi <= 0:
            raise ValueError("Ange en andel (%) större än 0 för varje mottagare")
        return {"customer_id": cid, "first_name": first, "last_name": last,
                "personnummer": S.normalize_personnummer(pnr),
                "rut_share_centi": rut_centi, "rot_share_centi": rot_centi}

    def _save_recipient_customer(self, rc: dict, invoice_customer_id: int) -> None:
        """Persist personnummer onto the recipient's customer (if missing) and ensure a
        household link to the invoice customer."""
        cid = rc["customer_id"]
        cust = self.get_customer(cid)
        if not cust.get("personnummer"):
            self.update_customer(cid, personnummer=rc["personnummer"])
        if cid != int(invoice_customer_id):
            self.link_customers(invoice_customer_id, cid)

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

        income_splits = self._income_splits(transaktion_id)
        if t["direction"] == "in":
            postings = [(k, ex_k, "utgift") for k, ex_k in income_splits]
            if sum_moms:
                postings.append((self._sys_account("account_ingaende_moms"), sum_moms, "ingående moms"))
            postings.append((self._sys_account("account_bank"), -inc, "betalning"))
            text = "Utgift"
        else:  # 'out' — sale
            postings = [(self._sys_account("account_bank"), inc - rut, "inbetalning")]
            if rut:
                postings.append((self._sys_account("account_rut_fordran"), rut, "husavdrag fordran"))
            postings.extend((k, -ex_k, "försäljning") for k, ex_k in income_splits)
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

    def skatteverket_payment_preview(self, rut_claim_id: int, received_ore: int) -> dict:
        """
        Interpret a manually-entered Skatteverket payout WITHOUT booking it, so the UI
        can confirm/override before committing. Returns the claimed amount, the
        difference, the rounding tolerance, and the suggested interpretation
        ('exact' | 'rounding' | 'partial' | 'overpaid').
        """
        claim = self.conn.execute(
            "SELECT rut_amount_ore FROM rut_claim WHERE id=?", (rut_claim_id,)).fetchone()
        if claim is None:
            raise KeyError(f"No rut_claim {rut_claim_id}")
        claimed = claim["rut_amount_ore"]
        received = int(received_ore)
        diff = claimed - received                       # >0 underpaid, <0 overpaid
        tol = int(self._config("rut_skv_rounding_tolerance_ore"))
        if diff == 0:
            interp = "exact"
        elif abs(diff) <= tol:
            interp = "rounding"
        elif diff > tol:
            interp = "partial"
        else:
            interp = "overpaid"
        return {"claimed_ore": claimed, "received_ore": received, "difference_ore": diff,
                "tolerance_ore": tol, "interpretation": interp}

    def register_rut_skatteverket_payment(self, rut_claim_id: int, payment_date: str,
                                          received_ore: Optional[int] = None, *,
                                          mode: Optional[str] = None,
                                          relation_note: Optional[str] = None) -> dict:
        """
        Book the Skatteverket payout of a RUT/ROT claim as its own verifikation. The
        claim must already be 'customer_paid'.

        `received_ore` is the amount Skatteverket actually paid (defaults to the full
        claimed amount). Booking depends on how it compares to the claimed amount:

        * exact / within ±tolerance (config `rut_skv_rounding_tolerance_ore`, 0,49 kr):
          treat the small diff as **rounding** and book it to 3740 Öres- och
          kronutjämning so the receivable (1513) clears exactly.
        * a larger underpayment: a **partial** payout — the unpaid remainder is
          reclassified 1513 → 1510 (now owed by the customer) and a linked follow-up
          invoice documents it (no moms; income/moms were already booked at the sale).

        `mode` (None=auto, 'rounding', 'partial') lets the caller confirm/override the
        interpretation. A >tolerance underpayment requires an explicit 'partial' so the
        app never silently swallows a quota-driven shortfall as rounding.
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
        claimed = claim["rut_amount_ore"]
        received = claimed if received_ore is None else int(received_ore)
        if received < 0:
            raise ValueError("Mottaget belopp kan inte vara negativt")
        diff = claimed - received                       # >0 underpaid, <0 overpaid
        tol = int(self._config("rut_skv_rounding_tolerance_ore"))

        interp = mode
        if interp is None:
            if abs(diff) <= tol:
                interp = "rounding"
            elif diff > tol:
                raise InvalidState(
                    f"Skatteverket betalade {received} öre mot begärda {claimed} öre "
                    f"(differens {diff} öre > {tol}). Bekräfta delbetalning för att skapa "
                    "en uppföljningsfaktura till kunden.")
            else:
                raise InvalidState(
                    f"Skatteverket betalade mer än begärt (differens {diff} öre); "
                    "kontrollera beloppet.")
        if interp == "rounding" and abs(diff) > tol:
            raise InvalidState(
                f"Differensen {diff} öre är för stor för öresavrundning (max {tol}).")
        if interp == "partial" and diff <= tol:
            raise InvalidState("Ingen delbetalning: differensen ryms inom avrundningen.")

        bank = self._sys_account("account_bank")
        fordran = self._sys_account("account_rut_fordran")
        postings = [(bank, received, "husavdrag utbetalt"),
                    (fordran, -claimed, "kvitta fordran")]
        if interp == "rounding":
            if diff != 0:
                postings.append((self._sys_account("account_ores_kronutjamning"),
                                 diff, "öresavrundning"))
            text = "Husavdrag utbetalt av Skatteverket"
        else:  # partial: the remainder becomes a receivable on the customer (1510)
            postings.append((self._sys_account("account_kundfordran"),
                             diff, "kvarstående fordran kund"))
            text = "Husavdrag delvis utbetalt av Skatteverket"

        shortfall_invoice_id = None
        with self.conn:
            vid, number = self._post_verifikation(payment_date, payment_date, text, postings)
            if interp == "partial":
                shortfall_invoice_id = self._create_husavdrag_shortfall_invoice(
                    claim, diff, payment_date, relation_note)
            self.conn.execute(
                "UPDATE rut_claim SET state='skatteverket_paid', skatteverket_payment_date=?, "
                "skatteverket_verifikation_id=?, skatteverket_received_ore=?, "
                "shortfall_invoice_id=? WHERE id=?",
                (payment_date, vid, received, shortfall_invoice_id, rut_claim_id),
            )
        return {"verifikation_id": vid, "ver_number": number, "interpretation": interp,
                "claimed_ore": claimed, "received_ore": received, "difference_ore": diff,
                "shortfall_invoice_id": shortfall_invoice_id}

    def _create_husavdrag_shortfall_invoice(self, claim, shortfall_ore: int,
                                            date: str, relation_note: Optional[str]) -> int:
        """
        Document the unpaid husavdrag remainder as a linked follow-up invoice on the
        customer. It carries NO moms (income + moms were fully booked at the original
        sale); the 1510 receivable was already established by the Skatteverket-payment
        verifikation, so this is the source document for it. Settled later via
        `pay_invoice` (bank ← 1510). Numbered from the same unbroken faktura series.
        """
        parent = self.conn.execute(
            "SELECT * FROM invoice WHERE transaktion_id=?", (claim["transaktion_id"],)).fetchone()
        if parent is None:
            raise OperationError("Hittar inte ursprungsfakturan för husavdraget")
        parent_number = parent["invoice_number"]
        note = relation_note or (
            f"Avser delbetalning av faktura {parent_number} — RUT/ROT-avdrag ej fullt "
            "utnyttjat av Skatteverket.")
        due = (datetime.fromisoformat(date) + timedelta(days=30)).date().isoformat()
        number = self._next_invoice_number()
        cur = self.conn.execute(
            "INSERT INTO invoice(invoice_number, customer_id, transaktion_id, invoice_date, "
            "due_date, buyer_snapshot_enc, seller_snapshot, payment_methods_snapshot, note, "
            "ex_moms_ore, moms_ore, inc_moms_ore, rut_total_ore, rot_total_ore, "
            "parent_invoice_id, relation_note, husavdrag_shortfall_ore, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (number, parent["customer_id"], None, date, due, parent["buyer_snapshot_enc"],
             parent["seller_snapshot"], parent["payment_methods_snapshot"], note,
             shortfall_ore, 0, shortfall_ore, 0, 0, parent["id"], note, shortfall_ore, _now()))
        invoice_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO invoice_line(invoice_id, line_no, description, category_id, "
            "quantity_centi, unit, unit_price_ore, rate_code, rut_eligible, reduction_type, "
            "article_id, ex_moms_ore, moms_ore) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (invoice_id, 1, note, None, 100, None, shortfall_ore, "momsfri", 0, None, None,
             shortfall_ore, 0))
        return invoice_id

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

    def husavdrag_cap_status(self, customer_id: int, year: int) -> dict:
        """Per-recipient husavdrag used in `year` vs BOTH per-person caps.

        Sums this customer's RUT and ROT amounts across the year's (non-cancelled)
        invoice recipients and checks them against two limits (Skatteverket, verified
        2026-06): the **combined** RUT+ROT cap (75 000 kr) AND the **ROT-only** sub-cap
        (50 000 kr). (Plain non-invoice RUT incomes are tracked separately by
        rut_cap_status on the income customer.)"""
        cap = int(self._config("rut_rot_cap_ore_per_customer_year"))
        rot_cap = int(self._config("rot_cap_ore_per_customer_year"))
        row = self.conn.execute(
            "SELECT COALESCE(SUM(rr.rut_amount_ore + rr.rot_amount_ore), 0) AS used, "
            "COALESCE(SUM(rr.rot_amount_ore), 0) AS rot_used "
            "FROM rut_recipient rr JOIN invoice i ON i.id = rr.invoice_id "
            "WHERE rr.customer_id=? AND substr(i.invoice_date,1,4)=? AND i.cancelled_at IS NULL",
            (customer_id, str(year))).fetchone()
        used, rot_used = row["used"], row["rot_used"]
        return {
            "customer_id": customer_id, "year": year,
            # combined RUT+ROT cap
            "cap_ore": cap, "used_ore": used, "remaining_ore": cap - used,
            "over_cap": used > cap, "near_cap": used >= cap * 0.9,
            # ROT-only sub-cap
            "rot_cap_ore": rot_cap, "rot_used_ore": rot_used, "rot_remaining_ore": rot_cap - rot_used,
            "rot_over_cap": rot_used > rot_cap, "rot_near_cap": rot_used >= rot_cap * 0.9,
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

    def verifikationer_full(self, start: Optional[str] = None,
                            end: Optional[str] = None) -> list[dict]:
        """
        Grundbok (journal): every verifikation with its konteringar (postings). Optional
        inclusive ver_date range. Postings are ordered debit-first for readability.
        """
        where, args = [], []
        if start:
            where.append("ver_date >= ?"); args.append(start)
        if end:
            where.append("ver_date <= ?"); args.append(end)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        vers = [dict(r) for r in self.conn.execute(
            "SELECT id, series, ver_number, ver_date, registration_date, text, posted, "
            "rattelse_of FROM verifikation" + clause + " ORDER BY ver_number", args)]
        for v in vers:
            v["postings"] = [dict(r) for r in self.conn.execute(
                "SELECT p.bas_konto, a.name AS konto_namn, p.amount_ore, p.text "
                "FROM posting p JOIN account a ON a.bas_konto = p.bas_konto "
                "WHERE p.verifikation_id=? ORDER BY p.amount_ore DESC, p.id", (v["id"],))]
        return vers

    def huvudbok(self, start: Optional[str] = None, end: Optional[str] = None) -> list[dict]:
        """
        Huvudbok (general ledger): per BAS-konto, every posting in ver-number order with
        a running saldo, plus per-account debit/credit sums and closing saldo. Optional
        inclusive ver_date range (a range gives the period's movements, not a true IB).
        """
        where, args = [], []
        if start:
            where.append("v.ver_date >= ?"); args.append(start)
        if end:
            where.append("v.ver_date <= ?"); args.append(end)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            "SELECT p.bas_konto, a.name AS konto_namn, v.id AS ver_id, v.series, "
            "v.ver_number, v.ver_date, v.text AS ver_text, p.amount_ore, p.text "
            "FROM posting p JOIN verifikation v ON v.id = p.verifikation_id "
            "JOIN account a ON a.bas_konto = p.bas_konto" + clause +
            " ORDER BY p.bas_konto, v.ver_number, p.id", args).fetchall()
        accounts: dict[int, dict] = {}
        for r in rows:
            acc = accounts.get(r["bas_konto"])
            if acc is None:
                acc = accounts[r["bas_konto"]] = {
                    "bas_konto": r["bas_konto"], "konto_namn": r["konto_namn"],
                    "debit_ore": 0, "credit_ore": 0, "saldo_ore": 0, "lines": []}
            amt = r["amount_ore"]
            acc["saldo_ore"] += amt
            if amt >= 0:
                acc["debit_ore"] += amt
            else:
                acc["credit_ore"] += -amt
            acc["lines"].append({
                "ver_id": r["ver_id"], "ver": f"{r['series']}{r['ver_number']}",
                "ver_date": r["ver_date"], "ver_text": r["ver_text"],
                "amount_ore": amt, "saldo_ore": acc["saldo_ore"], "text": r["text"]})
        return [accounts[k] for k in sorted(accounts)]

    def add_manual_verifikation(self, ver_date: str, text: str, lines: list[dict],
                                reg_date: Optional[str] = None) -> dict:
        """
        Post a MANUAL verifikation (a hand-entered journal entry, independent of
        invoices/transaktioner) — for corrections that the automated flows can't make,
        e.g. fixing a booking after a code bug. Still a normal legal verifikation: it
        gets the next unbroken number, must balance (debit = credit), respects period
        locks, and is immutable once posted (rätta it with a rättelse if wrong).

        `lines` = [{bas_konto, amount_ore (signed: debit>0/credit<0), text?, account_name?}].
        Unknown konton are auto-created (name from account_name or "Konto N").
        """
        if not text or not text.strip():
            raise ValueError("Ange en verifikationstext")
        postings: list[tuple[int, int, Optional[str]]] = []
        total = 0
        for ln in lines or []:
            amt = int(ln["amount_ore"])
            if amt == 0:
                continue
            konto = int(ln["bas_konto"])
            name = ln.get("account_name") or self._account_name(konto) or f"Konto {konto}"
            self.ensure_account(konto, name)
            postings.append((konto, amt, (ln.get("text") or None)))
            total += amt
        if len(postings) < 2:
            raise ValueError("En verifikation behöver minst två konteringsrader med belopp")
        if total != 0:
            raise ValueError(f"Debet och kredit balanserar inte (differens {total} öre)")
        with self.conn:
            vid, number = self._post_verifikation(
                ver_date, reg_date or ver_date, text.strip(), postings)
        return {"verifikation_id": vid, "ver_number": number}

    def _account_name(self, bas_konto: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT name FROM account WHERE bas_konto=?", (bas_konto,)).fetchone()
        return row["name"] if row else None

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

    def create_invoice(self, *, customer_id: int, category_id: Optional[int] = None,
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
        Each article line carries its own income category (BAS-konto); booking then
        splits the income across those konton. `category_id` is the fallback used for
        lines that don't set their own. Assigns the next unbroken invoice_number.
        """
        customer = self.get_customer(customer_id)
        recipients = recipients or []
        if not lines:
            raise ValueError("An invoice needs at least one article line")
        if category_id is not None:
            self._check_category(category_id, "income")

        # 1) Per-line figures + aggregate the moms lines by (category, rate). Each line
        #    books to its own category (BAS-konto), falling back to the invoice default.
        computed, agg = [], {}
        for i, ln in enumerate(lines, start=1):
            rate_code = ln["rate_code"]
            if rate_code not in S.MOMS_RATES:
                raise ValueError(f"Unknown moms rate {rate_code!r}")
            line_cat = ln.get("category_id") or category_id
            if line_cat is None:
                raise ValueError("Each invoice line needs a category (or set a default category)")
            self._check_category(line_cat, "income")
            qty_centi = int(ln["quantity_centi"])
            unit_price_ore = int(ln["unit_price_ore"])
            # Per-line percentage discount (rabatt): applied to the line total ex moms
            # BEFORE moms is computed, so moms + husavdrag follow the discounted amount.
            discount_pct_centi = int(ln.get("discount_pct_centi") or 0)
            if not 0 <= discount_pct_centi <= 10000:
                raise ValueError("Rabatt måste vara mellan 0 och 100 %")
            gross_ex = round(qty_centi * unit_price_ore / 100)
            ex = gross_ex - round(gross_ex * discount_pct_centi / 10000)
            _, moms, _ = compute_moms_figures(ex, rate_code, inclusive=False)
            # reduction_type: 'rut' | 'rot' | None (back-compat: a bare rut_eligible
            # flag from older callers means RUT).
            reduction_type = ln.get("reduction_type")
            if reduction_type is None and ln.get("rut_eligible"):
                reduction_type = "rut"
            if reduction_type not in (None, "rut", "rot"):
                raise ValueError(f"Unknown reduction_type {reduction_type!r}")
            computed.append({
                "line_no": i, "description": ln["description"], "category_id": line_cat,
                "quantity_centi": qty_centi, "unit": ln.get("unit"),
                "unit_price_ore": unit_price_ore, "rate_code": rate_code,
                "reduction_type": reduction_type, "rut_eligible": 1 if reduction_type else 0,
                "article_id": ln.get("article_id"), "discount_pct_centi": discount_pct_centi,
                "ex_moms_ore": ex, "moms_ore": moms,
            })
            agg[(line_cat, rate_code)] = agg.get((line_cat, rate_code), 0) + ex
        moms_lines = [{"rate_code": rc, "amount_ore": ex, "inclusive": False, "category_id": cat}
                      for (cat, rc), ex in agg.items()]
        # Fallback category for the underlying transaktion row (any line's category works
        # since per-line category_id on the moms_lines drives the actual income split).
        fallback_category = category_id or computed[0]["category_id"]

        # 2) RUT/ROT pots from the eligible lines: skattereduktion = labour cost INCL
        #    moms × the config percentage (RUT 50 %, ROT 30 %). The whole eligible line
        #    counts as labour (material goes on its own non-eligible lines).
        rut_pct, rot_pct = self.reduction_pcts()
        rut_pot = sum(round((cl["ex_moms_ore"] + cl["moms_ore"]) * rut_pct / 100)
                      for cl in computed if cl["reduction_type"] == "rut")
        rot_pot = sum(round((cl["ex_moms_ore"] + cl["moms_ore"]) * rot_pct / 100)
                      for cl in computed if cl["reduction_type"] == "rot")
        has_reduction = bool(rut_pot or rot_pot)
        if has_reduction and customer["type"] != "private":
            raise ValueError("RUT/ROT gäller endast privatpersoner")

        # 3) Recipients split each pot by their share %. Cumulative rounding keeps the
        #    per-person amounts öre-exact against the pot. Recipient customers get the
        #    personnummer saved and a household link to the invoice customer.
        rut_total = rot_total = 0
        clean_recipients = []
        if recipients:
            if customer["type"] != "private":
                raise ValueError("RUT/ROT-mottagare gäller endast privatpersoner")
            resolved = [self._resolve_recipient(r, customer_id, single=len(recipients) == 1)
                        for r in recipients]
            # Each pot is split by its OWN share sequence (RUT and ROT may differ per
            # person); cumulative rounding keeps each pot öre-exact. Validate the share
            # sum only for a pot that actually carries money.
            if rut_pot and sum(rc["rut_share_centi"] for rc in resolved) > 10000:
                raise ValueError("RUT-andelarna överstiger 100 %")
            if rot_pot and sum(rc["rot_share_centi"] for rc in resolved) > 10000:
                raise ValueError("ROT-andelarna överstiger 100 %")
            cum_rut = cum_rot = 0
            for rc in resolved:
                br, ar = cum_rut, cum_rut + rc["rut_share_centi"]
                cum_rut = ar
                rut_amt = round(rut_pot * ar / 10000) - round(rut_pot * br / 10000)
                bo, ao = cum_rot, cum_rot + rc["rot_share_centi"]
                cum_rot = ao
                rot_amt = round(rot_pot * ao / 10000) - round(rot_pot * bo / 10000)
                rut_total += rut_amt
                rot_total += rot_amt
                rc["rut_amount_ore"], rc["rot_amount_ore"] = rut_amt, rot_amt
                clean_recipients.append(rc)
            # Persist the recipient↔customer household links + personnummer (own txns).
            for rc in clean_recipients:
                if rc["customer_id"]:
                    self._save_recipient_customer(rc, customer_id)
        elif has_reduction:
            raise ValueError("RUT/ROT-rader kräver minst en mottagare")

        husavdrag = rut_total + rot_total       # total receivable from Skatteverket (1513)

        # 4) Underlying pending income (snapshot + moms_lines + rut_claim + reports).
        income = self.record_income(customer_id, fallback_category, moms_lines, invoice_date,
                                    rut_amount_ore=husavdrag, note=note)
        tid = income["transaktion_id"]
        ex_total, _, inc_total = self._sum_moms(tid)
        moms_total = inc_total - ex_total

        # 4b) Fakturametod: book the invoice NOW (kundfordran/income/moms at issue).
        # The transaktion's verifikation becomes this issue posting, so the moms is
        # reported in the invoice's period; payment later only settles the receivable.
        # Kontantmetod leaves it pending (booked when paid + year-end accrual).
        if self.get_accounting_method() == "fakturametod":
            self._book_invoice_issue(tid, invoice_date, husavdrag)

        # 4) Frozen snapshots + the sequential invoice number. Re-fetch the buyer so a
        # personnummer just saved via a recipient is captured in the snapshot.
        customer = self.get_customer(customer_id)
        buyer_snapshot_enc = self.session.encrypt_text(json.dumps(customer, default=str))
        seller_snapshot = json.dumps(self.get_company(), default=str)
        pm_snapshot = json.dumps(self.list_payment_methods(active_only=True), default=str)
        number = self._next_invoice_number()

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO invoice(invoice_number, customer_id, transaktion_id, invoice_date, "
                "due_date, delivery_date, payment_terms, buyer_snapshot_enc, seller_snapshot, "
                "payment_methods_snapshot, our_reference, your_reference, note, ex_moms_ore, "
                "moms_ore, inc_moms_ore, rut_total_ore, rot_total_ore, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (number, customer_id, tid, invoice_date, due_date, delivery_date, payment_terms,
                 buyer_snapshot_enc, seller_snapshot, pm_snapshot, our_reference, your_reference,
                 note, ex_total, moms_total, inc_total, rut_total, rot_total, _now()))
            invoice_id = cur.lastrowid
            for cl in computed:
                self.conn.execute(
                    "INSERT INTO invoice_line(invoice_id, line_no, description, category_id, "
                    "quantity_centi, unit, unit_price_ore, rate_code, rut_eligible, reduction_type, "
                    "article_id, discount_pct_centi, ex_moms_ore, moms_ore) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (invoice_id, cl["line_no"], cl["description"], cl["category_id"],
                     cl["quantity_centi"], cl["unit"], cl["unit_price_ore"], cl["rate_code"],
                     cl["rut_eligible"], cl["reduction_type"], cl["article_id"],
                     cl["discount_pct_centi"], cl["ex_moms_ore"], cl["moms_ore"]))
            for r in clean_recipients:
                self.conn.execute(
                    "INSERT INTO rut_recipient(invoice_id, customer_id, first_name, last_name, "
                    "personnummer_enc, share_pct_centi, rot_share_pct_centi, rut_amount_ore, "
                    "rot_amount_ore) VALUES (?,?,?,?,?,?,?,?,?)",
                    (invoice_id, r["customer_id"], r["first_name"], r["last_name"],
                     self.session.encrypt_text(r["personnummer"]), r["rut_share_centi"],
                     r["rot_share_centi"], r["rut_amount_ore"], r["rot_amount_ore"]))

        # Non-blocking per-recipient annual cap warnings (Skatteverket reduces the
        # actual payout; we only flag it). Computed AFTER insert so usage includes this
        # invoice; deduped per recipient customer.
        cap_warnings = []
        year = int(invoice_date[:4])
        for cid in {rc["customer_id"] for rc in clean_recipients if rc["customer_id"]}:
            status = self.husavdrag_cap_status(cid, year)
            if (status["over_cap"] or status["near_cap"]
                    or status["rot_over_cap"] or status["rot_near_cap"]):
                cust = self.get_customer(cid)
                cap_warnings.append({
                    "customer_id": cid,
                    "name": f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip(),
                    "used_ore": status["used_ore"], "cap_ore": status["cap_ore"],
                    "over_cap": status["over_cap"], "near_cap": status["near_cap"],
                    "rot_used_ore": status["rot_used_ore"], "rot_cap_ore": status["rot_cap_ore"],
                    "rot_over_cap": status["rot_over_cap"], "rot_near_cap": status["rot_near_cap"],
                })

        return {"invoice_id": invoice_id, "invoice_number": number, "transaktion_id": tid,
                "ex_moms_ore": ex_total, "moms_ore": moms_total, "inc_moms_ore": inc_total,
                "rut_total_ore": rut_total, "rot_total_ore": rot_total,
                "husavdrag_ore": husavdrag, "cap_warnings": cap_warnings}

    # ---- invoice drafts (unissued, editable; books nothing until create_invoice) ----

    def save_draft(self, payload: dict, draft_id: Optional[int] = None) -> dict:
        """Create or update an invoice draft. The full form payload is stored
        encrypted (it may contain recipient personnummer). No number is assigned and
        nothing is booked. Returns the draft id + timestamp."""
        enc = self.session.encrypt_text(json.dumps(payload, default=str))
        cid = payload.get("customer_id")
        now = _now()
        with self.conn:
            if draft_id is not None:
                if self.conn.execute("SELECT 1 FROM invoice_draft WHERE id=?",
                                     (draft_id,)).fetchone() is None:
                    raise KeyError(f"No invoice draft {draft_id}")
                self.conn.execute(
                    "UPDATE invoice_draft SET customer_id=?, payload_enc=?, updated_at=? WHERE id=?",
                    (cid, enc, now, draft_id))
            else:
                cur = self.conn.execute(
                    "INSERT INTO invoice_draft(customer_id, payload_enc, created_at, updated_at) "
                    "VALUES (?,?,?,?)", (cid, enc, now, now))
                draft_id = cur.lastrowid
        return {"id": draft_id, "updated_at": now}

    def list_drafts(self) -> list[dict]:
        """List drafts with a best-effort summary (line count + estimated inc total)."""
        out = []
        for r in self.conn.execute(
                "SELECT id, customer_id, payload_enc, updated_at FROM invoice_draft "
                "ORDER BY updated_at DESC").fetchall():
            try:
                payload = json.loads(self.session.decrypt_text(r["payload_enc"]))
            except Exception:
                payload = {}
            lines = payload.get("lines") or []
            total = 0
            for ln in lines:
                try:
                    ex = round(int(ln.get("quantity_centi", 0)) * int(ln.get("unit_price_ore", 0)) / 100)
                    rc = ln.get("rate_code")
                    _, _, inc = (compute_moms_figures(ex, rc, inclusive=False)
                                 if rc in S.MOMS_RATES else (ex, 0, ex))
                    total += inc
                except Exception:
                    pass
            out.append({"id": r["id"], "customer_id": r["customer_id"],
                        "updated_at": r["updated_at"], "line_count": len(lines),
                        "total_ore": total})
        return out

    def get_draft(self, draft_id: int) -> dict:
        """Return the decrypted form payload of a draft (to reload into the editor)."""
        row = self.conn.execute(
            "SELECT id, payload_enc, updated_at FROM invoice_draft WHERE id=?",
            (draft_id,)).fetchone()
        if row is None:
            raise KeyError(f"No invoice draft {draft_id}")
        return {"id": row["id"], "updated_at": row["updated_at"],
                "payload": json.loads(self.session.decrypt_text(row["payload_enc"]))}

    def delete_draft(self, draft_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM invoice_draft WHERE id=?", (draft_id,))

    def _invoice_balances(self, invoice_id: int) -> dict:
        """
        Derive an invoice's running settlement from the event subledger.

        customer_due = (inc − RUT) − credits   (what the customer should pay)
        paid         = payments − refunds
        outstanding  = customer_due − paid
        """
        inv = self.conn.execute(
            "SELECT inc_moms_ore, rut_total_ore, rot_total_ore, cancelled_at, credited_at, "
            "transaktion_id FROM invoice WHERE id=?", (invoice_id,)).fetchone()
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
        husavdrag = inv["rut_total_ore"] + inv["rot_total_ore"]
        customer_total = inv["inc_moms_ore"] - husavdrag   # what the customer pays
        customer_due = customer_total - credits
        paid = payments - refunds
        # Legacy/RUT path: paid in full via register_payment (no subledger events).
        if not has_events and tx_status == "paid":
            paid = customer_due
        return {
            "inc_moms_ore": inv["inc_moms_ore"], "rut_total_ore": inv["rut_total_ore"],
            "rot_total_ore": inv["rot_total_ore"], "husavdrag_ore": husavdrag,
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
            "inc_moms_ore, rut_total_ore, rot_total_ore, parent_invoice_id, "
            "husavdrag_shortfall_ore, relation_note FROM invoice ORDER BY invoice_number"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.update(self._invoice_balances(r["id"]))
            d["credit_notes"] = [dict(c) for c in self.conn.execute(
                "SELECT id, credit_note_number, date, amount_ore FROM invoice_event "
                "WHERE invoice_id=? AND kind='credit' AND credit_note_number IS NOT NULL "
                "ORDER BY id", (r["id"],)).fetchall()]
            # RUT/ROT husavdrag claim (for the Skatteverket-payment step that follows the
            # customer payment): expose its id + state so the Fakturor tab can keep a
            # "book the Skatteverket payout" button until it arrives.
            d["rut_claim_id"] = None
            d["rut_claim_state"] = None
            if r["transaktion_id"]:
                claim = self.conn.execute(
                    "SELECT id, state FROM rut_claim WHERE transaktion_id=?",
                    (r["transaktion_id"],)).fetchone()
                if claim:
                    d["rut_claim_id"] = claim["id"]
                    d["rut_claim_state"] = claim["state"]
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
            "SELECT il.line_no, il.description, il.category_id, c.name AS category_name, "
            "c.bas_konto AS category_bas_konto, il.quantity_centi, il.unit, il.unit_price_ore, "
            "il.rate_code, il.rut_eligible, il.reduction_type, il.discount_pct_centi, "
            "il.ex_moms_ore, il.moms_ore "
            "FROM invoice_line il LEFT JOIN category c ON c.id = il.category_id "
            "WHERE il.invoice_id=? ORDER BY il.line_no", (invoice_id,)).fetchall()]
        inv["recipients"] = [{
            "customer_id": r["customer_id"],
            "first_name": r["first_name"], "last_name": r["last_name"],
            "personnummer": self.session.decrypt_text(r["personnummer_enc"]),
            "share_pct": r["share_pct_centi"] / 100,                    # RUT share (legacy key)
            "rut_share_pct": r["share_pct_centi"] / 100,
            "rot_share_pct": (r["rot_share_pct_centi"] if r["rot_share_pct_centi"] is not None
                              else r["share_pct_centi"]) / 100,
            "rut_amount_ore": r["rut_amount_ore"], "rot_amount_ore": r["rot_amount_ore"],
        } for r in self.conn.execute(
            "SELECT customer_id, first_name, last_name, personnummer_enc, share_pct_centi, "
            "rot_share_pct_centi, rut_amount_ore, rot_amount_ore FROM rut_recipient "
            "WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()]
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
        if inv["husavdrag_shortfall_ore"]:
            return self._pay_husavdrag_shortfall(inv, bal, amount_ore, date)
        if inv["rut_total_ore"] or inv["rot_total_ore"]:
            raise InvalidState("RUT/ROT invoices: use the full payment + Skatteverket flow")
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

    def _pay_husavdrag_shortfall(self, inv, bal, amount_ore, date) -> dict:
        """Settle a husavdrag follow-up invoice: pure receivable collection (bank ←
        1510), no income/moms recognition (already booked at the original sale)."""
        amount = bal["outstanding_ore"] if amount_ore is None else int(amount_ore)
        if amount <= 0:
            raise ValueError("Payment amount must be > 0")
        if amount > bal["outstanding_ore"]:
            raise InvalidState("Payment exceeds the outstanding amount")
        date = date or _now()[:10]
        text = f"Betalning faktura {inv['invoice_number']} (kvarstående husavdrag)"
        postings = [(self._sys_account("account_bank"), amount, "inbetalning"),
                    (self._sys_account("account_kundfordran"), -amount, "kvitta kundfordran")]
        with self.conn:
            vid, num = self._post_verifikation(date, date, text, postings)
            self._record_invoice_event(inv["id"], "payment", amount, date, vid)
            self._sync_invoice_paid(inv["id"], None, date)
        return {"invoice_id": inv["id"], "verifikation_id": vid, "ver_number": num,
                "amount_ore": amount,
                "outstanding_ore": self._invoice_balances(inv["id"])["outstanding_ore"]}

    def refund_invoice(self, invoice_id: int, amount_ore: int,
                       date: Optional[str] = None, note: Optional[str] = None) -> dict:
        """Pay money back to the customer (full or partial) — the reverse of a payment."""
        inv, bal = self._require_open_invoice(invoice_id)
        if inv["husavdrag_shortfall_ore"]:
            raise InvalidState("Husavdrag-uppföljningsfaktura stöder bara betalning")
        if inv["rut_total_ore"] or inv["rot_total_ore"]:
            raise InvalidState("RUT/ROT invoices: refunds not supported via the subledger")
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
        if inv["husavdrag_shortfall_ore"]:
            raise InvalidState("Husavdrag-uppföljningsfaktura stöder bara betalning")
        if inv["rut_total_ore"] or inv["rot_total_ore"]:
            raise InvalidState("RUT/ROT invoices: partial credit not supported; use makulera/full flow")
        billable = inv["inc_moms_ore"] - inv["rut_total_ore"] - inv["rot_total_ore"] - bal["credited_ore"]
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

        # Reverse the proportional income/moms slice (split per line category),
        # balanced against the receivable.
        slices = self._recognition_slice(tid, bal["credited_ore"], amount, inv["inc_moms_ore"])
        postings = [(konto, ex, "kreditering försäljning")
                    for konto, ex in sorted(self._group_income(tid, slices).items())]
        for rate_code, moms in sorted(self._group_moms(slices).items()):
            postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), moms,
                             f"kreditering moms {rate_code}%"))
        postings.append((self._sys_account("account_kundfordran"), -amount, "minskad kundfordran"))
        with self.conn:
            vid, num = self._post_verifikation(date, date, text, postings)
            # negative report-clone so the momsdeklaration/result net the credit
            self._book_recognition_clone(tid, vid, date, slices, -1, "kreditering")
            credit_note_number = self._next_invoice_number()
            ev_id = self._record_invoice_event(invoice_id, "credit", amount, date, vid, reason,
                                               credit_note_number)
            fully = (bal["credited_ore"] + amount
                     >= inv["inc_moms_ore"] - inv["rut_total_ore"] - inv["rot_total_ore"])
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
        for s in slices:
            ex_s, moms_s = s["ex_s"], s["moms_s"]
            if ex_s or moms_s:
                lines.append({"line_no": len(lines) + 1,
                              "description": f"Kreditering avseende faktura {orig['invoice_number']}",
                              "quantity_centi": 100, "unit": None, "unit_price_ore": -ex_s,
                              "rate_code": s["rate_code"], "rut_eligible": 0,
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
            "rot_total_ore, husavdrag_shortfall_ore, cancelled_at FROM invoice WHERE id=?",
            (invoice_id,)).fetchone()
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

    def _recognition_slice(self, transaktion_id, recognized_before, amount, total_inc) -> list:
        """Per-line (category_id, rate, ex, moms) to recognise for `amount` of cash,
        using cumulative rounding so a sequence of partials reconciles exactly to the
        öre. One entry per moms_line (an invoice carries one line per category×rate)."""
        before, after = recognized_before, recognized_before + amount
        out = []
        for ln in self.conn.execute(
                "SELECT category_id, rate_code, ex_moms_ore, moms_ore FROM moms_line "
                "WHERE transaktion_id=?", (transaktion_id,)):
            ex_s = (round(ln["ex_moms_ore"] * after / total_inc)
                    - round(ln["ex_moms_ore"] * before / total_inc))
            moms_s = (round(ln["moms_ore"] * after / total_inc)
                      - round(ln["moms_ore"] * before / total_inc))
            out.append({"category_id": ln["category_id"], "rate_code": ln["rate_code"],
                        "ex_s": ex_s, "moms_s": moms_s})
        return out

    def _income_splits(self, transaktion_id) -> list:
        """[(bas_konto, sum_ex), …] — the full ex-moms of a transaktion grouped by each
        moms_line's category konto (fallback the transaktion category). Used when
        booking the whole amount at once (cash sale/purchase, fakturametod issue)."""
        fb = self.conn.execute("SELECT category_id FROM transaktion WHERE id=?",
                               (transaktion_id,)).fetchone()["category_id"]
        agg: dict[int, int] = {}
        for ln in self.conn.execute(
                "SELECT category_id, ex_moms_ore FROM moms_line WHERE transaktion_id=?",
                (transaktion_id,)):
            cat = ln["category_id"] if ln["category_id"] is not None else fb
            konto = self._category_konto(cat)
            agg[konto] = agg.get(konto, 0) + ln["ex_moms_ore"]
        return sorted(agg.items())

    def _group_income(self, transaktion_id, slices) -> dict:
        """{bas_konto: sum_ex} — slice ex grouped by each line's category konto, falling
        back to the transaktion's category when a line carries none (plain income)."""
        fb = self.conn.execute("SELECT category_id FROM transaktion WHERE id=?",
                               (transaktion_id,)).fetchone()["category_id"]
        agg: dict[int, int] = {}
        for s in slices:
            cat = s["category_id"] if s["category_id"] is not None else fb
            konto = self._category_konto(cat)
            agg[konto] = agg.get(konto, 0) + s["ex_s"]
        return agg

    @staticmethod
    def _group_moms(slices) -> dict:
        """{rate_code: sum_moms} over the utgående-moms rates only."""
        agg: dict[str, int] = {}
        for s in slices:
            if s["moms_s"] and s["rate_code"] in _UTG_MOMS_KEY:
                agg[s["rate_code"]] = agg.get(s["rate_code"], 0) + s["moms_s"]
        return agg

    def _book_recognition_clone(self, src_transaktion_id, vid, date, slices, sign, note) -> None:
        """Synthetic transaktion + sliced moms_lines linked to `vid` so the moms/result
        reports attribute this slice to `vid`'s date (hidden from the Transaktioner list).
        Carries each slice's category_id so the result report splits by line category."""
        src = self.conn.execute(
            "SELECT direction, category_id, supplier_id, customer_id FROM transaktion WHERE id=?",
            (src_transaktion_id,)).fetchone()
        cur = self.conn.execute(
            "INSERT INTO transaktion(direction, category_id, supplier_id, customer_id, trans_date, "
            "status, verifikation_id, note, created_at) VALUES (?,?,?,?,?, 'paid', ?, ?, ?)",
            (src["direction"], src["category_id"], src["supplier_id"], src["customer_id"],
             date, vid, note, _now()))
        rid = cur.lastrowid
        for s in slices:
            if s["ex_s"] or s["moms_s"]:
                self.conn.execute(
                    "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                    "moms_ore, inc_moms_ore) VALUES (?,?,?,?,?,?)",
                    (rid, s["rate_code"], s["category_id"], sign * s["ex_s"],
                     sign * s["moms_s"], sign * (s["ex_s"] + s["moms_s"])))

    def _book_kontant_recognition(self, transaktion_id, recognized_before, amount, total_inc,
                                  date, sign, text) -> tuple[int, int]:
        """Kontantmetod: book bank +/- amount against income + moms recognised for the
        proportional slice (income split per line category), plus the report-clone.
        Returns (verifikation_id, number)."""
        slices = self._recognition_slice(transaktion_id, recognized_before, amount, total_inc)
        postings = [(self._sys_account("account_bank"), sign * amount, "inbetalning")]
        for konto, ex in sorted(self._group_income(transaktion_id, slices).items()):
            postings.append((konto, -sign * ex, "försäljning"))
        for rate_code, moms in sorted(self._group_moms(slices).items()):
            postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -sign * moms,
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
        ex, moms_by_rate, inc = self._sum_moms(transaktion_id)
        cust_part = inc - rut_ore
        postings = []
        if cust_part:
            postings.append((self._sys_account("account_kundfordran"), cust_part, "kundfordran"))
        if rut_ore:
            postings.append((self._sys_account("account_rut_fordran"), rut_ore, "husavdrag fordran"))
        postings.extend((k, -ex_k, "försäljning") for k, ex_k in self._income_splits(transaktion_id))
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

    def system_accounts(self) -> dict[int, str]:
        """The booking engine's BAS-konton (bank, moms, receivables, …), each
        materialised into the chart and mapped {bas_konto: human label}. Lets the UI
        show the otherwise-hidden system konton alongside the user's categories."""
        out: dict[int, str] = {}
        for key, label in _SYS_ACCOUNT_NAMES.items():
            out[self._sys_account(key)] = label
        return out

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
            "SELECT rate_code, category_id, ex_moms_ore, moms_ore, inc_moms_ore FROM moms_line "
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
                "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                "moms_ore, inc_moms_ore) VALUES (?,?,?,?,?,?)",
                (rid, ln["rate_code"], ln["category_id"], sign * ln["ex_moms_ore"],
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
                    "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                    "moms_ore, inc_moms_ore) VALUES (?,?,?,?,?,?)",
                    (transaktion_id, rate_code, ln.get("category_id"), ex, moms, inc),
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
