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

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
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
                  "contact_person", "vat_nr", "address", "email", "phone"):
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
                  "vat_nr", "address", "email", "phone", "active"):
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
        if t["verifikation_id"] is not None:
            raise InvalidState("Transaktion is already booked")

        ex, moms_by_rate, inc = self._sum_moms(transaktion_id)
        sum_moms = sum(moms_by_rate.values())
        konto = self._category_konto(t["category_id"])

        claim = self.conn.execute(
            "SELECT * FROM rut_claim WHERE transaktion_id=?", (transaktion_id,)
        ).fetchone()
        rut = claim["rut_amount_ore"] if claim else 0

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
