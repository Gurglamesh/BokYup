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
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from backend.db.manager import BookSession
from backend.models import schema as S
from backend.models.bas_catalog import BAS_CATALOG, CATEGORY_KINDS, catalog_entry


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
    "account_egna_insattningar": "Egna insättningar (privat)",
    "account_ingaende_moms": "Ingående moms",
    "account_utgaende_moms_25": "Utgående moms 25 %",
    "account_utgaende_moms_12": "Utgående moms 12 %",
    "account_utgaende_moms_6": "Utgående moms 6 %",
    "account_rut_fordran": "Kundfordran husavdrag (RUT/ROT)",
    "account_kundfordran": "Kundfordringar",
    "account_leverantorsskuld": "Leverantörsskulder",
    "account_ores_kronutjamning": "Öres- och kronutjämning",
    "account_utg_moms_omvand_25": "Utgående moms omvänd skattskyldighet 25 %",
    "account_utg_moms_omvand_12": "Utgående moms omvänd skattskyldighet 12 %",
    "account_utg_moms_omvand_6": "Utgående moms omvänd skattskyldighet 6 %",
    "account_ing_moms_utland": "Beräknad ingående moms på förvärv från utlandet",
    "account_forbrukningsinventarier": "Förbrukningsinventarier",
    "account_inventarier": "Inventarier och verktyg",
}

_DEFAULT_PAY_METHODS = ["Företagskonto", "Kort", "Swish", "Klarna delbetalning",
                        "Klarna faktura", "Qliro", "Leverantörsfaktura", "Autogiro", "Kontant"]

_UTG_MOMS_KEY = {
    "25": "account_utgaende_moms_25",
    "12": "account_utgaende_moms_12",
    "6": "account_utgaende_moms_6",
}

# Omvänd betalningsskyldighet (reverse charge) on a PURCHASE: the seller invoices
# without moms and you report both sides yourself. The key picks the momsdeklaration
# box for the beskattningsunderlag; the moms itself lands in box 30/31/32 (utgående)
# and box 48 (ingående), so it nets to zero with full avdragsrätt.
REVERSE_CHARGE_KINDS = S.REVERSE_CHARGE_BOXES
REVERSE_CHARGE_RATES = S.REVERSE_CHARGE_RATES

_UTG_MOMS_OMVAND_KEY = {
    "25": "account_utg_moms_omvand_25",
    "12": "account_utg_moms_omvand_12",
    "6": "account_utg_moms_omvand_6",
}

# Notes stamped on the SYNTHETIC transaktion rows that `_clone_transaktion_for_report`
# creates to attribute a rättelse/accrual to the right period in the moms/result
# reports. They are bookkeeping artefacts, not user transactions, so the default
# Transaktioner list hides them (the legal record lives in the verifikationer).
SYNTHETIC_TRANSAKTION_NOTES = ("rättelse", "periodisering", "återföring",
                               "fakturabetalning", "kreditering", "ombokföring")


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


def _clean_reverse_charge(rate_code: str, value) -> Optional[str]:
    """Validate a moms line's omvänd-betalningsskyldighet marking (None = normal moms)."""
    if value in (None, "", "none"):
        return None
    value = str(value)
    if value not in REVERSE_CHARGE_KINDS:
        raise ValueError(
            f"Okänd omvänd betalningsskyldighet {value!r} — välj en av: "
            + ", ".join(REVERSE_CHARGE_KINDS))
    if rate_code not in REVERSE_CHARGE_RATES:
        raise ValueError(
            "Omvänd betalningsskyldighet kräver en momssats (25, 12 eller 6 %) — "
            f"{rate_code!r} går inte att räkna moms på")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Support-time (gratis distanssupport) constants: 15 minutes per full 1000 kr.
SUPPORT_STEP_ORE = 100000           # 1000 kr
SUPPORT_MINUTES_PER_STEP = 15


def support_minutes_earned(total_inc_ore: int) -> int:
    """15 minutes for every full 1000 kr of the invoice total (round down; any
    remainder under 1000 kr earns nothing). E.g. 2 499 kr -> 30 min, not 37,5."""
    if total_inc_ore <= 0:
        return 0
    return (total_inc_ore // SUPPORT_STEP_ORE) * SUPPORT_MINUTES_PER_STEP


def _round_to_krona(ore: int) -> int:
    """Öresavrundning: round an öre amount to the nearest whole krona (0–49 öre down,
    50–99 öre up). The faktura requests whole kronor; the booked amount is this final,
    already-rounded amount (no separate 3740 posting in this flow — see CLAUDE.md)."""
    if ore >= 0:
        return ((ore + 50) // 100) * 100
    return -(((-ore + 50) // 100) * 100)


def _add_months(iso_date: str, months: int) -> str:
    """Add whole months to a YYYY-MM-DD date, clamping the day to the target month's
    last day (e.g. 2024-02-29 + 36 months -> 2027-02-28)."""
    import calendar
    y, m, d = (int(x) for x in iso_date[:10].split("-"))
    total = (m - 1) + months
    y2, m2 = y + total // 12, total % 12 + 1
    return f"{y2:04d}-{m2:02d}-{min(d, calendar.monthrange(y2, m2)[1]):02d}"


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

    def _next_unused_prefix(self) -> str:
        """Lowest unused 4-digit article-number prefix ('0000'..'9999'). Each category
        owns one; there is room for 10 000 categories (0000 included)."""
        used = {r[0] for r in self.conn.execute(
            "SELECT prefix FROM category WHERE prefix IS NOT NULL")}
        for n in range(10000):
            p = f"{n:04d}"
            if p not in used:
                return p
        raise OperationError("Inga lediga prefix kvar (max 10000 kategorier)")

    def prefix_in_use(self, prefix: str, exclude_id: Optional[int] = None) -> bool:
        """Whether a 4-digit category prefix is already taken (optionally ignoring one
        category, for edit-in-place)."""
        row = self.conn.execute(
            "SELECT id FROM category WHERE prefix=?", (str(prefix),)).fetchone()
        return row is not None and row["id"] != exclude_id

    def _validate_prefix(self, prefix: str, exclude_id: Optional[int] = None) -> str:
        prefix = str(prefix).strip()
        if not (prefix.isdigit() and len(prefix) == 4):
            raise ValueError("Prefixet måste vara exakt 4 siffror (0000–9999)")
        if self.prefix_in_use(prefix, exclude_id):
            raise InvalidState(f"Prefixet {prefix} används redan")
        return prefix

    def _category_prefix(self, category_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT prefix FROM category WHERE id=?", (category_id,)).fetchone()
        return row["prefix"] if row else None

    def category_descendants(self, category_id: int) -> set:
        """All categories under `category_id` (any depth), excluding itself."""
        out, stack = set(), [category_id]
        while stack:
            for r in self.conn.execute(
                    "SELECT id FROM category WHERE parent_id=?", (stack.pop(),)).fetchall():
                if r["id"] not in out:
                    out.add(r["id"])
                    stack.append(r["id"])
        return out

    def create_category(self, name: str, kind: str, bas_konto: Optional[int] = None,
                        account_name: Optional[str] = None,
                        default_rate_code: Optional[str] = None,
                        prefix: Optional[str] = None,
                        parent_id: Optional[int] = None) -> int:
        """Create a category linked to a BAS-konto (auto-creating the account).

        `default_rate_code` is the moms rate the UI pre-fills for lines booked to this
        category. `prefix` is the unique 4-digit article-number prefix; if omitted the
        lowest unused one is assigned. `parent_id` makes this a **subcategory**: it takes
        its parent's `kind` and, when not given, inherits the parent's BAS-konto +
        default moms (purely organizational — it still books to whatever konto it holds).
        """
        if parent_id is not None:
            parent = self.conn.execute(
                "SELECT id, kind, bas_konto, default_rate_code FROM category WHERE id=?",
                (parent_id,)).fetchone()
            if parent is None:
                raise KeyError(f"No parent category {parent_id}")
            kind = parent["kind"]                       # a subcategory shares its parent's kind
            if bas_konto is None:
                bas_konto = parent["bas_konto"]          # inherit the konto by default
            if default_rate_code is None:
                default_rate_code = parent["default_rate_code"]
        if kind not in ("income", "expense"):
            raise ValueError("kind must be 'income' or 'expense'")
        if bas_konto is None:
            raise ValueError("En kategori behöver ett BAS-konto")
        if default_rate_code is not None and default_rate_code not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {default_rate_code!r}")
        prefix = self._validate_prefix(prefix) if prefix is not None else self._next_unused_prefix()
        self.ensure_account(bas_konto, account_name or name)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO category(name, kind, bas_konto, default_rate_code, prefix, "
                "parent_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (name, kind, bas_konto, default_rate_code, prefix, parent_id, _now()),
            )
        return cur.lastrowid

    def update_category(self, category_id: int, *, name: Optional[str] = None,
                        bas_konto: Optional[int] = None, active: Optional[bool] = None,
                        account_name: Optional[str] = None,
                        default_rate_code: Optional[str] = None,
                        prefix: Optional[str] = None,
                        parent_id: Optional[int] = None) -> None:
        """Edit reference data freely — including the BAS-konto of a category that has
        ALREADY been booked on. Nothing historical moves: a posting carries its konto as
        a frozen number, and every booked moms_line carries the konto it was booked to
        (`_freeze_line_konton`), so huvudbok, SIE, årsbokslut and the result report all
        keep showing the old konto for the old entries. The new konto applies from the
        next booking onwards. (A rättelse of an already-booked entry is the way to move
        history — see `rebook_transaktion`.)

        Changing the prefix only affects future article numbers (issued ones are frozen).
        `parent_id` reparents (0/negative → make top-level); a cycle is refused."""
        if bas_konto is not None:
            self.ensure_account(bas_konto, account_name or name or f"Konto {bas_konto}")
        if default_rate_code is not None and default_rate_code not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {default_rate_code!r}")
        if prefix is not None:
            prefix = self._validate_prefix(prefix, exclude_id=category_id)
        sets, params = _build_update(
            {"name": name, "bas_konto": bas_konto, "default_rate_code": default_rate_code,
             "prefix": prefix, "active": None if active is None else int(active)}
        )
        with self.conn:
            if sets:
                self.conn.execute(f"UPDATE category SET {sets} WHERE id=?", (*params, category_id))
            if parent_id is not None:
                new_parent = int(parent_id) if int(parent_id) > 0 else None
                if new_parent is not None:
                    if new_parent == category_id or new_parent in self.category_descendants(category_id):
                        raise InvalidState("En kategori kan inte bli underkategori till sig själv")
                    if self.conn.execute("SELECT 1 FROM category WHERE id=?",
                                         (new_parent,)).fetchone() is None:
                        raise KeyError(f"No parent category {new_parent}")
                self.conn.execute("UPDATE category SET parent_id=? WHERE id=?",
                                  (new_parent, category_id))

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

    # ---------------- preset BAS-konton (the BAS-kontoplan picker) ------------

    def bas_catalog(self) -> list[dict]:
        """The preset BAS-kontoplan, each entry flagged with whether this book already
        has it (`added` = a category on that konto, `in_chart` = the konto exists)."""
        cat_konton = {r["bas_konto"] for r in self.conn.execute(
            "SELECT DISTINCT bas_konto FROM category")}
        chart = {r["bas_konto"] for r in self.conn.execute("SELECT bas_konto FROM account")}
        out = []
        for e in BAS_CATALOG:
            out.append({**e,
                        "added": e["bas_konto"] in cat_konton,
                        "in_chart": e["bas_konto"] in chart})
        return out

    def add_catalog_accounts(self, konton: list) -> dict:
        """
        Add preset BAS-konton to this book.

        An income/expense konto becomes an ordinary **category** (freely editable
        afterwards — name, number and default moms). A balance-sheet konto (tillgång/
        skuld/eget kapital) is only added to the chart of accounts, so it can be picked
        in a manual verifikation; it is never a category, since a category is always
        income or expense.

        Konton the book already has are skipped, so the picker is safe to re-run.
        """
        created, skipped = [], []
        for raw in konton or []:
            entry = catalog_entry(raw)
            if entry is None:
                raise ValueError(f"{raw} finns inte i den förinställda BAS-kontoplanen")
            konto = entry["bas_konto"]
            if entry["kind"] in CATEGORY_KINDS:
                if self.conn.execute("SELECT 1 FROM category WHERE bas_konto=?",
                                     (konto,)).fetchone():
                    skipped.append(konto)
                    continue
                cid = self.create_category(entry["name"], entry["kind"], konto,
                                           account_name=entry["name"],
                                           default_rate_code=entry["rate_code"])
                created.append({"bas_konto": konto, "category_id": cid,
                                "name": entry["name"], "kind": entry["kind"]})
            else:
                if self.conn.execute("SELECT 1 FROM account WHERE bas_konto=?",
                                     (konto,)).fetchone():
                    skipped.append(konto)
                    continue
                self.ensure_account(konto, entry["name"])
                created.append({"bas_konto": konto, "category_id": None,
                                "name": entry["name"], "kind": entry["kind"]})
        return {"created": created, "skipped": skipped}

    # ==================================================================
    # Article catalog (reusable invoice line items)
    # ==================================================================

    def _gen_article_number(self, prefix: str) -> str:
        """Build a unique article number `<prefix>-XXXX` (XXXX random). `prefix` is a
        category's 4-digit prefix, or the provisional 'NY' bucket for an uncategorised
        article. Retries on the rare collision."""
        prefix = str(prefix or "NY")
        for _ in range(50):
            number = f"{prefix}-{secrets.randbelow(10000):04d}"
            if self.conn.execute("SELECT 1 FROM article WHERE article_number=?",
                                 (number,)).fetchone() is None:
                return number
        raise OperationError("Kunde inte skapa ett unikt artikelnummer")

    def article_in_use(self, article_id: int) -> bool:
        """Whether the article appears on any invoice line (issued) — once it does its
        number is frozen (it may be printed on a legal faktura)."""
        return self.conn.execute(
            "SELECT 1 FROM invoice_line WHERE article_id=? LIMIT 1",
            (article_id,)).fetchone() is not None

    def create_article(self, description: str, prefix: Optional[str] = None, *,
                       unit_price_ore: int = 0, rate_code: str = "25",
                       reduction_type: Optional[str] = None,
                       category_id: Optional[int] = None,
                       unit: Optional[str] = None) -> dict:
        """Create a catalog article. The article number's prefix comes from the chosen
        category's prefix (or the provisional 'NY' bucket when uncategorised); an explicit
        `prefix` overrides that. The suffix is random and unique."""
        if not description:
            raise ValueError("Artikeln behöver en beskrivning")
        if rate_code not in S.MOMS_RATES:
            raise ValueError(f"Unknown moms rate {rate_code!r}")
        if reduction_type not in (None, "rut", "rot"):
            raise ValueError(f"Unknown reduction_type {reduction_type!r}")
        if category_id is not None:
            self._check_category(category_id, "income")
        if prefix is not None:
            # An explicit override must still be a valid 4-digit prefix.
            if not (str(prefix).isdigit() and len(str(prefix)) == 4):
                raise ValueError("Prefixet måste vara exakt 4 siffror")
        else:
            prefix = self._category_prefix(category_id) if category_id is not None else "NY"
        number = self._gen_article_number(prefix)
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
               "c.prefix AS category_prefix, a.active "
               "FROM article a LEFT JOIN category c ON c.id = a.category_id ")
        if active_only:
            sql += "WHERE a.active = 1 "
        sql += "ORDER BY a.article_number"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def update_article(self, article_id: int, **fields) -> None:
        """Edit an article (e.g. categorise it, change price/moms, rename). Does not
        touch already-issued invoice lines (they carry their own frozen values).

        Assigning/changing the category re-issues the article number to the new
        category's prefix — but only while the article has never appeared on an issued
        invoice (once it has, the number is frozen)."""
        row = self.conn.execute(
            "SELECT category_id, article_number FROM article WHERE id=?",
            (article_id,)).fetchone()
        if row is None:
            raise KeyError(f"No article {article_id}")
        if fields.get("rate_code") is not None and fields["rate_code"] not in S.MOMS_RATES:
            raise ValueError("Unknown moms rate")
        if fields.get("category_id") is not None:
            self._check_category(fields["category_id"], "income")
        # Re-issue the number to the new category's prefix (unless frozen / explicit).
        if ("category_id" in fields and fields["category_id"] != row["category_id"]
                and "article_number" not in fields and not self.article_in_use(article_id)):
            new_prefix = (self._category_prefix(fields["category_id"])
                          if fields["category_id"] is not None else "NY")
            fields["article_number"] = self._gen_article_number(new_prefix)
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
        """Delete a catalog article. Issued invoice lines keep their frozen values (incl.
        the picked-batch cost); any live link to the article or its stock batches is just
        detached. The article's stock batches are pure tracking, so they go with it."""
        if self.conn.execute("SELECT 1 FROM article WHERE id=?", (article_id,)).fetchone() is None:
            raise KeyError(f"No article {article_id}")
        with self.conn:
            self.conn.execute("UPDATE invoice_line SET article_id=NULL WHERE article_id=?",
                              (article_id,))
            # Detach invoice lines that picked one of this article's batches, then drop the
            # batches (a NOT NULL FK to article, so they must go before the article).
            self.conn.execute(
                "UPDATE invoice_line SET stock_batch_id=NULL WHERE stock_batch_id IN "
                "(SELECT id FROM stock_batch WHERE article_id=?)", (article_id,))
            self.conn.execute("DELETE FROM stock_batch WHERE article_id=?", (article_id,))
            self.conn.execute("DELETE FROM article WHERE id=?", (article_id,))

    # ==================================================================
    # Stock / lager (inventory batches — pure tracking, no ledger impact)
    # ==================================================================

    def _next_batch_number(self, article_id: int) -> int:
        """The next batch number WITHIN an article (max + 1, starting at 1). The full
        batch id is article_number + batch_number."""
        row = self.conn.execute(
            "SELECT MAX(batch_number) FROM stock_batch WHERE article_id=?",
            (article_id,)).fetchone()
        return (row[0] or 0) + 1

    def find_or_create_article(self, description: str, category_id: Optional[int] = None,
                               *, rate_code: str = "25",
                               reduction_type: Optional[str] = None,
                               unit: Optional[str] = None,
                               unit_price_ore: int = 0) -> int:
        """Return an existing active article matching (description, category) or create a
        new one. Used by the Inköp flow so buying the same article again adds a batch to
        the SAME article. Match is on trimmed, case-insensitive description + category.
        A newly-created article's default à-pris is `unit_price_ore` (the inköp price);
        an existing article keeps its own price."""
        desc = (description or "").strip()
        if not desc:
            raise ValueError("Artikeln behöver en beskrivning")
        if category_id is None:
            row = self.conn.execute(
                "SELECT id FROM article WHERE active=1 AND category_id IS NULL "
                "AND lower(trim(description))=lower(?) ORDER BY id LIMIT 1", (desc,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM article WHERE active=1 AND category_id=? "
                "AND lower(trim(description))=lower(?) ORDER BY id LIMIT 1",
                (category_id, desc)).fetchone()
        if row:
            return row["id"]
        return self.create_article(desc, category_id=category_id, rate_code=rate_code,
                                   reduction_type=reduction_type, unit=unit,
                                   unit_price_ore=int(unit_price_ore))["id"]

    def add_stock_batch(self, article_id: int, qty_centi: int, unit_cost_ore: int, *,
                        received_date: Optional[str] = None,
                        supplier_id: Optional[int] = None,
                        purchase_transaktion_id: Optional[int] = None,
                        note: Optional[str] = None) -> dict:
        """Add a buy-in of `article_id` to stock as a new batch. `qty_centi` is the
        purchased quantity ×100; `unit_cost_ore` the ex-moms cost per unit. Returns the
        new batch's id + its (visible-in-Lager) batch_number. Pure inventory tracking —
        it books nothing (any ledger side lives in the linked purchase transaktion)."""
        if self.conn.execute("SELECT 1 FROM article WHERE id=?", (article_id,)).fetchone() is None:
            raise KeyError(f"No article {article_id}")
        qty_centi = int(qty_centi)
        if qty_centi <= 0:
            raise ValueError("Antalet måste vara större än 0")
        unit_cost_ore = int(unit_cost_ore)
        if unit_cost_ore < 0:
            raise ValueError("Inköpspriset kan inte vara negativt")
        if supplier_id is not None and self.conn.execute(
                "SELECT 1 FROM supplier WHERE id=?", (supplier_id,)).fetchone() is None:
            raise KeyError(f"No supplier {supplier_id}")
        number = self._next_batch_number(article_id)
        date = received_date or _now()[:10]
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO stock_batch(batch_number, article_id, qty_in_centi, "
                "qty_remaining_centi, unit_cost_ore, supplier_id, purchase_transaktion_id, "
                "received_date, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (number, article_id, qty_centi, qty_centi, unit_cost_ore, supplier_id,
                 purchase_transaktion_id, date, note, _now()))
        return {"id": cur.lastrowid, "batch_number": number}

    def list_stock(self, include_empty: bool = False) -> list[dict]:
        """Per-article stock summary: quantity on hand, number of open batches and the
        total value (Σ qty_remaining × unit_cost). Only articles that have ever been
        stocked appear; set include_empty to also show fully-consumed articles."""
        rows = self.conn.execute(
            "SELECT a.id AS article_id, a.article_number, a.description, a.unit, "
            "a.category_id, c.name AS category_name, "
            "COUNT(sb.id) AS batch_count, "
            "COALESCE(SUM(sb.qty_remaining_centi), 0) AS qty_remaining_centi, "
            "COALESCE(SUM(sb.qty_remaining_centi * sb.unit_cost_ore) / 100, 0) AS value_ore "
            "FROM stock_batch sb JOIN article a ON a.id = sb.article_id "
            "LEFT JOIN category c ON c.id = a.category_id "
            "WHERE sb.deleted_at IS NULL "
            "GROUP BY a.id ORDER BY a.article_number").fetchall()
        out = [dict(r) for r in rows]
        if not include_empty:
            out = [r for r in out if r["qty_remaining_centi"] > 0]
        return out

    def list_article_batches(self, article_id: int, open_only: bool = False) -> list[dict]:
        """All stock batches for one article (newest first), each with batch_number,
        quantities, unit cost and source. `open_only` hides fully-consumed batches."""
        sql = ("SELECT sb.id, sb.batch_number, sb.article_id, a.article_number, "
               "(a.article_number || '-' || sb.batch_number) AS full_batch_id, "
               "sb.qty_in_centi, sb.qty_remaining_centi, sb.unit_cost_ore, sb.supplier_id, "
               "s.name AS supplier_name, sb.purchase_transaktion_id, sb.received_date, sb.note "
               "FROM stock_batch sb LEFT JOIN supplier s ON s.id = sb.supplier_id "
               "LEFT JOIN article a ON a.id = sb.article_id "
               "WHERE sb.article_id=? AND sb.deleted_at IS NULL ")
        if open_only:
            sql += "AND sb.qty_remaining_centi > 0 "
        sql += "ORDER BY sb.batch_number DESC"
        return [dict(r) for r in self.conn.execute(sql, (article_id,)).fetchall()]

    def delete_stock_batch(self, batch_id: int) -> None:
        """Delete a stock batch. Refused once any of it has been consumed (sold on an
        invoice line) — the cost is then part of a frozen margin; adjust with a new
        counter-batch instead."""
        row = self.conn.execute(
            "SELECT qty_in_centi, qty_remaining_centi FROM stock_batch WHERE id=?",
            (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"No stock batch {batch_id}")
        if row["qty_remaining_centi"] != row["qty_in_centi"]:
            raise InvalidState("Batchen är delvis förbrukad och kan inte tas bort")
        if self.conn.execute("SELECT 1 FROM invoice_line WHERE stock_batch_id=? LIMIT 1",
                             (batch_id,)).fetchone():
            raise InvalidState("Batchen används på en faktura och kan inte tas bort")
        with self.conn:
            self.conn.execute("DELETE FROM stock_batch WHERE id=?", (batch_id,))

    def update_stock_batch(self, batch_id: int, **fields) -> None:
        """Edit a stock batch's non-audit fields: unit_cost_ore, received_date, note,
        supplier_id, and a stock correction of qty_remaining_centi. Cost edits do NOT
        rewrite already-sold lines (invoice_line.cost_ore is frozen at issue) — they only
        affect this batch's remaining stock value + future consumption."""
        row = self.conn.execute("SELECT qty_in_centi, qty_remaining_centi FROM stock_batch "
                                "WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"No stock batch {batch_id}")
        allowed = {"unit_cost_ore", "received_date", "note", "supplier_id", "qty_remaining_centi"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "unit_cost_ore":
                v = int(v)
                if v < 0:
                    raise ValueError("Inköpspriset kan inte vara negativt")
            if k == "qty_remaining_centi":
                v = int(v)
                if v < 0:
                    raise ValueError("Antalet kan inte vara negativt")
            if k == "supplier_id" and self.conn.execute(
                    "SELECT 1 FROM supplier WHERE id=?", (v,)).fetchone() is None:
                raise KeyError(f"No supplier {v}")
            sets.append(f"{k}=?"); params.append(v)
        if not sets:
            return
        params.append(batch_id)
        with self.conn:
            self.conn.execute(f"UPDATE stock_batch SET {', '.join(sets)} WHERE id=?", params)

    def adjust_stock_batch(self, batch_id: int, qty_delta_centi: int, reason: str) -> dict:
        """Write off / reduce a batch's remaining stock (svinn) with a logged reason —
        e.g. broken, lost, used in an unpaid project. Append-only: records the reduction
        in `stock_adjustment` and decrements qty_remaining. Reductions only (qty_delta < 0);
        the write-off can never exceed what is left (goods already sold stay untouched).
        Pure inventory tracking — it books nothing (the inköp is already expensed)."""
        row = self.conn.execute(
            "SELECT qty_remaining_centi, deleted_at FROM stock_batch WHERE id=?",
            (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"No stock batch {batch_id}")
        if row["deleted_at"] is not None:
            raise InvalidState("Batchen är borttagen")
        qty_delta_centi = int(qty_delta_centi)
        if qty_delta_centi >= 0:
            raise ValueError("Justeringen måste minska lagret (ange ett antal att skriva av)")
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("Ange en anledning till avskrivningen")
        new_remaining = row["qty_remaining_centi"] + qty_delta_centi
        if new_remaining < 0:
            raise InvalidState(
                f"Kan inte skriva av mer än vad som finns kvar "
                f"(kvar {row['qty_remaining_centi'] / 100:g})")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO stock_adjustment(batch_id, qty_delta_centi, reason, created_at) "
                "VALUES (?,?,?,?)", (batch_id, qty_delta_centi, reason, _now()))
            self.conn.execute(
                "UPDATE stock_batch SET qty_remaining_centi=? WHERE id=?",
                (new_remaining, batch_id))
        return {"id": cur.lastrowid, "qty_remaining_centi": new_remaining}

    def list_stock_adjustments(self, batch_id: int) -> list[dict]:
        """The write-off / adjustment history for one batch (newest first)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT id, batch_id, qty_delta_centi, reason, created_at "
            "FROM stock_adjustment WHERE batch_id=? ORDER BY id DESC", (batch_id,)).fetchall()]

    def _prune_invoice_empty_batches(self, invoice_id: int) -> None:
        """After an invoice is fully paid the reserved stock is permanently gone: drop any
        of its batches that now have 0 remaining (keeping the article and the lines' frozen
        cost_ore). Runs inside the caller's transaction. Batches shared by other lines are
        detached from all of them before deletion so no reference dangles."""
        batch_ids = [r["stock_batch_id"] for r in self.conn.execute(
            "SELECT DISTINCT stock_batch_id FROM invoice_line "
            "WHERE invoice_id=? AND stock_batch_id IS NOT NULL", (invoice_id,)).fetchall()]
        for bid in batch_ids:
            row = self.conn.execute("SELECT qty_remaining_centi FROM stock_batch WHERE id=?",
                                    (bid,)).fetchone()
            if row is None or row["qty_remaining_centi"] > 0:
                continue
            # Keep the frozen cost on every line; just drop the (now empty) batch link.
            self.conn.execute("UPDATE invoice_line SET stock_batch_id=NULL WHERE stock_batch_id=?",
                              (bid,))
            self.conn.execute("DELETE FROM stock_batch WHERE id=?", (bid,))

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

    def delete_customer(self, kundnummer: int) -> dict:
        """Remove a customer card from the register. Invoices, transaktioner, offerter and
        RUT/ROT-ärenden are KEPT (each carries a frozen buyer snapshot + immutable
        bookings); only their live link to this customer is detached. The customer's
        household relations and support-time ledger are removed with the card."""
        if self.conn.execute("SELECT 1 FROM customer WHERE kundnummer=?",
                             (kundnummer,)).fetchone() is None:
            raise KeyError(f"No customer {kundnummer}")
        with self.conn:
            # Detach the (nullable) live links — the frozen snapshots keep each record whole.
            for tbl in ("invoice", "transaktion", "rut_recipient", "rut_claim",
                        "offert", "invoice_draft"):
                self.conn.execute(f"UPDATE {tbl} SET customer_id=NULL WHERE customer_id=?",
                                  (kundnummer,))
            # Detach the (nullable) contact link too; the frozen buyer snapshot keeps the name.
            self.conn.execute(
                "UPDATE invoice SET contact_customer_id=NULL WHERE contact_customer_id=?",
                (kundnummer,))
            # Household links, company-contact links + this customer's support ledger go with the card.
            self.conn.execute(
                "DELETE FROM customer_relation WHERE customer_a=? OR customer_b=?",
                (kundnummer, kundnummer))
            self.conn.execute(
                "DELETE FROM company_contact WHERE company_id=? OR contact_id=?",
                (kundnummer, kundnummer))
            self.conn.execute("DELETE FROM support_ledger WHERE customer_id=?", (kundnummer,))
            self.conn.execute("DELETE FROM customer WHERE kundnummer=?", (kundnummer,))
        return {"deleted": True, "kundnummer": kundnummer}

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

    # ---- gratis distanssupport (free remote-support time bank) -----------------

    def support_balance(self, customer_id: int) -> dict:
        """
        A customer's remaining support time: the sum of `support_minutes_earned` from
        their invoices whose support is still valid (expiry in the future) MINUS the net
        used (deductions − additions from the ledger). Expired invoices drop out of the
        earned sum. Also returns the contributing invoices for transparency.
        """
        self.get_customer(customer_id)                 # 404 if unknown
        today = _now()[:10]
        earned_rows = self.conn.execute(
            "SELECT invoice_number, invoice_date, inc_moms_ore, support_minutes_earned, "
            "support_expiry_date FROM invoice WHERE customer_id=? AND husavdrag_shortfall_ore=0 "
            "AND cancelled_at IS NULL "     # makulerade fakturor ger ingen supporttid
            "AND support_minutes_earned>0 ORDER BY invoice_number", (customer_id,)).fetchall()
        active = [dict(r) for r in earned_rows if (r["support_expiry_date"] or "") >= today]
        earned_active = sum(r["support_minutes_earned"] for r in active)
        ded = self.conn.execute(
            "SELECT COALESCE(SUM(minutes),0) FROM support_ledger WHERE customer_id=? "
            "AND kind='deduction'", (customer_id,)).fetchone()[0]
        add = self.conn.execute(
            "SELECT COALESCE(SUM(minutes),0) FROM support_ledger WHERE customer_id=? "
            "AND kind='addition'", (customer_id,)).fetchone()[0]
        used = ded - add
        cap = int(self._config("support_cap_minutes"))
        raw_remaining = max(0, earned_active - used)
        return {
            "customer_id": customer_id,
            "earned_active_minutes": earned_active,
            "used_minutes": used,                      # net (deductions − additions)
            "deductions_minutes": ded,
            "additions_minutes": add,
            # Floored at 0 (over-use is recorded in full but never shows negative) and
            # capped at the per-customer maximum (12 h) — that IS the balance ceiling.
            "remaining_minutes": min(cap, raw_remaining),
            "cap_minutes": cap,
            "at_cap": raw_remaining >= cap,
            "active_invoices": active,
        }

    def record_support_entry(self, customer_id: int, minutes: int, kind: str,
                             note: Optional[str] = None) -> dict:
        """Log a manual support deduction (customer used time) or addition (bonus time
        outside the invoice logic). `minutes` is a positive amount; `kind` gives the
        direction. Returns the created ledger entry."""
        self.get_customer(customer_id)                 # 404 if unknown
        if kind not in ("deduction", "addition"):
            raise ValueError("kind must be 'deduction' or 'addition'")
        minutes = int(minutes)
        if minutes <= 0:
            raise ValueError("minutes must be > 0")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO support_ledger(customer_id, minutes, kind, note, created_at) "
                "VALUES (?,?,?,?,?)", (customer_id, minutes, kind, note, _now()))
        return {"id": cur.lastrowid, "customer_id": customer_id, "minutes": minutes,
                "kind": kind, "note": note}

    def list_support_ledger(self, customer_id: int) -> list[dict]:
        """The full support history for a customer (newest first)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT id, minutes, kind, note, created_at FROM support_ledger "
            "WHERE customer_id=? ORDER BY id DESC", (customer_id,)).fetchall()]

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

    # ---- company contacts (directed: a business customer -> private persons) ----

    def link_company_contact(self, company_id: int, contact_id: int) -> dict:
        """Attach a private customer as a contact person of a business customer
        (idempotent). The company must be a business and the contact a private
        customer — only the contact's name is used on documents; the rest of the
        contact info comes from the company's kundkort."""
        company_id, contact_id = int(company_id), int(contact_id)
        if company_id == contact_id:
            raise ValueError("Kan inte koppla en kund till sig själv")
        company = self.get_customer(company_id)
        contact = self.get_customer(contact_id)
        if company["type"] != "business":
            raise InvalidState("Kontaktpersoner kan bara läggas till på företagskunder")
        if contact["type"] != "private":
            raise InvalidState("En kontaktperson måste vara en privatkund")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO company_contact(company_id, contact_id, created_at) "
                "VALUES (?,?,?)", (company_id, contact_id, _now()))
        return {"company_id": company_id, "contact_id": contact_id}

    def unlink_company_contact(self, company_id: int, contact_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM company_contact WHERE company_id=? AND contact_id=?",
                (int(company_id), int(contact_id)))

    # Per-invoice delivery-address fields (full, company-style). Kept as a normalised dict.
    _DELIVERY_KEYS = ("name", "street", "zip_code", "city", "org_nr", "vat_nr", "email", "phone")

    def _clean_delivery(self, d) -> Optional[dict]:
        """Normalise a per-invoice delivery address to a dict of the known fields, or None
        when nothing was entered (then the billing address IS the delivery address)."""
        if not d:
            return None
        out = {k: str(d.get(k) or "").strip() for k in self._DELIVERY_KEYS}
        return out if any(out.values()) else None

    def _apply_contact(self, customer: dict, contact_customer_id) -> dict:
        """Return a copy of the buyer dict with an optional contact person's name merged
        in (contact_first_name/contact_last_name/contact_person) — the rest of the
        contact info stays the company's. No-op when no contact is given."""
        if not contact_customer_id:
            return customer
        contact = self.get_customer(int(contact_customer_id))
        out = dict(customer)
        out["contact_first_name"] = contact.get("first_name")
        out["contact_last_name"] = contact.get("last_name")
        out["contact_person"] = (
            f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}".strip()
            or customer.get("contact_person"))
        return out

    def list_company_contacts(self, company_id: int) -> list[dict]:
        """The private-person contacts attached to a business customer."""
        rows = self.conn.execute(
            "SELECT c.kundnummer, c.first_name, c.last_name "
            "FROM company_contact cc JOIN customer c ON c.kundnummer = cc.contact_id "
            "WHERE cc.company_id=? ORDER BY c.first_name, c.last_name, c.kundnummer",
            (int(company_id),)).fetchall()
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
                       ext_ref: Optional[str] = None,
                       ores_rounding: bool = False,
                       paid_date: Optional[str] = None,
                       paid_account: str = "bank") -> dict:
        """
        Record a purchase (ingående moms, deductible). `lines` is a list of
        {rate_code, amount_ore, inclusive} dicts. `ext_ref` is the supplier's
        kvitto-/fakturanummer. `ores_rounding` books the paid total to whole kronor
        (supplier öresavrundning) with the öre diff to 3740 (moms/underlag stay exact).
        If `paid_date` is given the transaktion is booked immediately (cash); otherwise
        it stays pending (an incoming supplier invoice — mark it paid later). `paid_account`
        chooses the funding source at payment: 'bank' (1930) or 'privat' (2018 Egna
        insättningar — paid from private money).
        """
        self._check_category(category_id, "expense")
        # A line may override the purchase's konto (one receipt can mix verktyg,
        # förbrukningsmaterial and programvara) — but only with another EXPENSE konto.
        for ln in lines or []:
            if ln.get("category_id") is not None:
                self._check_category(int(ln["category_id"]), "expense")
        tid = self._insert_transaktion(
            direction="in", category_id=category_id, supplier_id=supplier_id,
            customer_id=None, trans_date=trans_date, note=note,
            receipt_original_format=receipt_original_format, snapshot_enc=None,
            ext_ref=(ext_ref.strip() if ext_ref and ext_ref.strip() else None),
        )
        self._insert_moms_lines(tid, lines)
        if ores_rounding:
            with self.conn:
                self.conn.execute("UPDATE transaktion SET ores_rounding=1 WHERE id=?", (tid,))
        result = {"transaktion_id": tid}
        if paid_date:
            result.update(self.register_payment(tid, paid_date, paid_account=paid_account))
        return result

    def update_expense_meta(self, transaktion_id: int, *,
                            supplier_id: Optional[int] = None,
                            ext_ref: Optional[str] = None,
                            note: Optional[str] = None,
                            receipt_original_format: Optional[str] = None,
                            ores_rounding: Optional[bool] = None) -> dict:
        """Edit an inköp's NON-ledger fields: leverantör, kvitto-/fakturanummer, note and
        kvittots originalformat. The BAS-konto, belopp, moms and any articles/batches stay
        immutable (even after booking). `ores_rounding` (öresavrundning) is only meaningful
        BEFORE the inköp is booked — it is read at register_payment — so it may only be
        toggled while the transaktion is still pending; on a booked inköp change it via a
        rättelse (rebook). A field left None is untouched; pass an empty string to clear
        ext_ref/note."""
        t = self.conn.execute("SELECT direction, status, verifikation_id FROM transaktion "
                              "WHERE id=?", (transaktion_id,)).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["direction"] != "in":
            raise InvalidState("Endast inköp kan redigeras här")
        updates: dict[str, object] = {}
        if ores_rounding is not None:
            if t["status"] == "paid" or t["verifikation_id"] is not None:
                raise InvalidState(
                    "Öresavrundning kan bara ändras innan inköpet är bokfört (rätta annars via rättelse)")
            updates["ores_rounding"] = 1 if ores_rounding else 0
        if supplier_id is not None:
            if supplier_id and self.conn.execute(
                    "SELECT 1 FROM supplier WHERE id=?", (supplier_id,)).fetchone() is None:
                raise KeyError(f"No supplier {supplier_id}")
            updates["supplier_id"] = supplier_id or None
        if ext_ref is not None:
            updates["ext_ref"] = ext_ref.strip() or None
        if note is not None:
            updates["note"] = note.strip() or None
        if receipt_original_format is not None:
            if receipt_original_format and receipt_original_format not in S.RECEIPT_FORMATS:
                raise ValueError(f"Invalid receipt format: {receipt_original_format}")
            updates["receipt_original_format"] = receipt_original_format or None
        if updates:
            cols = list(updates.keys())
            sets = ", ".join(f"{c}=?" for c in cols)
            with self.conn:
                self.conn.execute(f"UPDATE transaktion SET {sets} WHERE id=?",
                                  (*[updates[c] for c in cols], transaktion_id))
        return {"transaktion_id": transaktion_id, "updated": list(updates.keys())}

    def update_expense(self, transaktion_id: int, *, category_id: int,
                       lines: list[dict], trans_date: str,
                       supplier_id: Optional[int] = None,
                       note: Optional[str] = None,
                       receipt_original_format: Optional[str] = None,
                       ext_ref: Optional[str] = None,
                       ores_rounding: bool = False) -> dict:
        """FULL edit of an UNBOOKED inköp (a direct purchase / leverantörsfaktura that is
        still pending). Because nothing has hit the ledger yet — no verifikation, no
        verifikationsnummer — every field is safe to rewrite: BAS-konto, belopp, moms,
        datum, leverantör, kvittonummer and öresavrundning. The transaktion id (and any
        attached receipt) is preserved. A BOOKED inköp is immutable — correct it with a
        rättelse (rebook) instead.

        The old stock batches this purchase created are dropped and rebuilt by the caller
        from the new lines; that is only allowed while they are wholly unconsumed."""
        t = self.conn.execute(
            "SELECT direction, status, verifikation_id, deleted_at, category_id "
            "FROM transaktion WHERE id=?", (transaktion_id,)).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["direction"] != "in":
            raise InvalidState("Endast inköp kan redigeras här")
        if t["category_id"] is None:
            raise InvalidState(
                "Ett inventarieinköp redigeras inte här — ta bort det och lägg in det på nytt")
        if t["deleted_at"] is not None:
            raise InvalidState("Transaktionen är borttagen")
        if t["status"] == "paid" or t["verifikation_id"] is not None:
            raise InvalidState(
                "Ett bokfört inköp kan inte redigeras — rätta via en rättelse istället")
        self._check_category(category_id, "expense")
        if receipt_original_format and receipt_original_format not in S.RECEIPT_FORMATS:
            raise ValueError(f"Invalid receipt format: {receipt_original_format}")
        if supplier_id and self.conn.execute(
                "SELECT 1 FROM supplier WHERE id=?", (supplier_id,)).fetchone() is None:
            raise KeyError(f"No supplier {supplier_id}")
        # Guard + drop the purchase's live batches (unconsumed only); the caller rebuilds.
        batches = self.conn.execute(
            "SELECT id, qty_in_centi, qty_remaining_centi FROM stock_batch "
            "WHERE purchase_transaktion_id=? AND deleted_at IS NULL", (transaktion_id,)).fetchall()
        for sb in batches:
            if sb["qty_remaining_centi"] != sb["qty_in_centi"] or self.conn.execute(
                    "SELECT 1 FROM invoice_line WHERE stock_batch_id=? LIMIT 1",
                    (sb["id"],)).fetchone():
                raise InvalidState(
                    "Inköpets varor är redan sålda/reserverade — inköpet kan inte redigeras")
        with self.conn:
            for sb in batches:
                self.conn.execute("DELETE FROM stock_batch WHERE id=?", (sb["id"],))
            self.conn.execute("DELETE FROM moms_line WHERE transaktion_id=?", (transaktion_id,))
            self.conn.execute(
                "UPDATE transaktion SET category_id=?, supplier_id=?, trans_date=?, note=?, "
                "receipt_original_format=?, ext_ref=?, ores_rounding=? WHERE id=?",
                (category_id, supplier_id or None, trans_date,
                 (note.strip() if note and note.strip() else None),
                 receipt_original_format or None,
                 (ext_ref.strip() if ext_ref and ext_ref.strip() else None),
                 1 if ores_rounding else 0, transaktion_id))
        self._insert_moms_lines(transaktion_id, lines)
        return {"transaktion_id": transaktion_id}

    def expense_edit_payload(self, transaktion_id: int) -> dict:
        """Reconstruct an inköp as a form-prefill (the same shape the inköp form edits):
        its header fields plus `items` (article line-items). Named stock batches become
        stocked item rows; any remaining moms per rate becomes a pure-cost row. Lets the UI
        reopen a pending inköp for a full edit. `booked` says whether it is immutable."""
        t = self.conn.execute(
            "SELECT direction, supplier_id, category_id, trans_date, ext_ref, note, "
            "ores_rounding, receipt_original_format, status, verifikation_id, deleted_at "
            "FROM transaktion WHERE id=?", (transaktion_id,)).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["direction"] != "in":
            raise InvalidState("Endast inköp kan redigeras här")
        if t["category_id"] is None:
            raise InvalidState(
                "Ett inventarieinköp redigeras inte här — ta bort det och lägg in det på "
                "nytt (det är inte bokfört), eller rätta det bokförda med en rättelse")
        # Keyed by (rate, omvänd betalningsskyldighet) so a reverse-charge line survives
        # the round trip through the form.
        remaining: dict[tuple, int] = {}
        for r in self.conn.execute(
                "SELECT rate_code, reverse_charge, category_id, ex_moms_ore FROM moms_line "
                "WHERE transaktion_id=?", (transaktion_id,)).fetchall():
            key = (r["rate_code"], r["reverse_charge"], r["category_id"])
            remaining[key] = remaining.get(key, 0) + r["ex_moms_ore"]
        items = []
        batches = self.conn.execute(
            "SELECT sb.qty_in_centi, sb.unit_cost_ore, a.description, a.category_id, "
            "a.rate_code, a.unit, a.reduction_type FROM stock_batch sb "
            "JOIN article a ON a.id = sb.article_id "
            "WHERE sb.purchase_transaktion_id=? AND sb.deleted_at IS NULL "
            "ORDER BY sb.batch_number", (transaktion_id,)).fetchall()
        for sb in batches:
            rate = sb["rate_code"] or "25"
            ex = round(sb["qty_in_centi"] * sb["unit_cost_ore"] / 100)
            # Charge the batch against a moms line of the same rate, plain moms first.
            key = next((k for k in ((rate, None, None),
                                    *(k for k in remaining if k[0] == rate))
                        if remaining.get(k, 0) >= ex), (rate, None, None))
            remaining[key] = remaining.get(key, 0) - ex
            items.append({"description": sb["description"], "category_id": sb["category_id"],
                          "quantity_centi": sb["qty_in_centi"], "unit_cost_ore": sb["unit_cost_ore"],
                          "rate_code": rate, "unit": sb["unit"], "reverse_charge": key[1],
                          "expense_category_id": key[2],
                          "reduction_type": sb["reduction_type"], "to_stock": True})
        for (rate, rc, exp_cat), ex in remaining.items():
            if ex > 0:
                items.append({"description": "", "category_id": None, "quantity_centi": 100,
                              "unit_cost_ore": ex, "rate_code": rate, "reverse_charge": rc,
                              "expense_category_id": exp_cat, "to_stock": False})
        return {
            "transaktion_id": transaktion_id, "supplier_id": t["supplier_id"],
            "category_id": t["category_id"], "trans_date": t["trans_date"],
            "ext_ref": t["ext_ref"], "note": t["note"],
            "ores_rounding": bool(t["ores_rounding"]),
            "receipt_original_format": t["receipt_original_format"],
            "status": t["status"], "booked": t["verifikation_id"] is not None,
            "deleted": t["deleted_at"] is not None, "items": items,
        }

    def soft_delete_transaktion(self, transaktion_id: int) -> dict:
        """Move an UNBOOKED transaktion (a pending inköp / income that never hit the
        ledger) to the 'borttagna' list: it stays in the database but is hidden from the
        normal lists. Legal-safe because nothing was booked — no verifikationsnummer was
        consumed, so the sequence stays unbroken. A BOOKED transaktion cannot be soft-
        deleted (that would leave a gap in the grundbok); reverse it with a rättelse
        instead.

        Any stock batches this purchase created are pulled out of stock too (soft-hidden),
        but only if still wholly unconsumed — you cannot discard a purchase whose goods
        have already been sold/reserved on an invoice."""
        t = self.conn.execute(
            "SELECT status, verifikation_id, deleted_at FROM transaktion WHERE id=?",
            (transaktion_id,)).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["deleted_at"] is not None:
            raise InvalidState("Transaktionen är redan borttagen")
        if t["status"] == "paid" or t["verifikation_id"] is not None:
            raise InvalidState(
                "En bokförd transaktion kan inte tas bort — reversera med en rättelse istället")
        # Guard + collect this purchase's live stock batches; refuse if any is consumed.
        batches = self.conn.execute(
            "SELECT id, qty_in_centi, qty_remaining_centi FROM stock_batch "
            "WHERE purchase_transaktion_id=? AND deleted_at IS NULL", (transaktion_id,)).fetchall()
        for sb in batches:
            if sb["qty_remaining_centi"] != sb["qty_in_centi"] or self.conn.execute(
                    "SELECT 1 FROM invoice_line WHERE stock_batch_id=? LIMIT 1",
                    (sb["id"],)).fetchone():
                raise InvalidState(
                    "Inköpets varor är redan sålda/reserverade och inköpet kan inte tas bort")
        now = _now()
        with self.conn:
            self.conn.execute("UPDATE transaktion SET deleted_at=? WHERE id=?",
                              (now, transaktion_id))
            for sb in batches:
                self.conn.execute("UPDATE stock_batch SET deleted_at=? WHERE id=?",
                                  (now, sb["id"]))
        return {"transaktion_id": transaktion_id, "deleted_at": now,
                "batches_hidden": [sb["id"] for sb in batches]}

    def restore_transaktion(self, transaktion_id: int) -> dict:
        """Bring a soft-deleted transaktion (and the stock batches hidden with it) back to
        the normal lists."""
        t = self.conn.execute(
            "SELECT deleted_at FROM transaktion WHERE id=?", (transaktion_id,)).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["deleted_at"] is None:
            raise InvalidState("Transaktionen är inte borttagen")
        with self.conn:
            self.conn.execute("UPDATE transaktion SET deleted_at=NULL WHERE id=?",
                              (transaktion_id,))
            self.conn.execute(
                "UPDATE stock_batch SET deleted_at=NULL "
                "WHERE purchase_transaktion_id=? AND deleted_at=?",
                (transaktion_id, t["deleted_at"]))
        return {"transaktion_id": transaktion_id, "restored": True}

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
        # A line may override the entry's konto, but only with another INCOME konto.
        for ln in lines or []:
            if ln.get("category_id") is not None:
                self._check_category(int(ln["category_id"]), "income")
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
    # Återkommande betalningar (recurring templates)
    # ==================================================================
    #
    # A template books NOTHING by itself. When an occurrence falls due it shows up in
    # "att bekräfta"; confirming it creates an ordinary transaktion (and books it, if a
    # payment date is given) exactly as if it had been entered by hand. That is what
    # makes editing safe: a template edit can only ever affect occurrences that have not
    # been confirmed yet — every already-booked one is an immutable verifikation.

    _REC_FIELDS = ("name", "category_id", "supplier_id", "customer_id", "note", "ext_ref",
                   "paid_account", "interval_unit", "interval_count", "next_date",
                   "end_date", "active")

    @staticmethod
    def _rec_next_date(date: str, unit: str, count: int) -> str:
        """The occurrence after `date` for a month/year interval."""
        return _add_months(date, count * (12 if unit == "year" else 1))

    def _validate_rec_lines(self, kind: str, lines: list) -> str:
        """Check a template's moms lines the same way a real entry would be checked, so a
        broken template is rejected when it is SAVED rather than when it is confirmed."""
        if not lines:
            raise ValueError("En återkommande betalning behöver minst en rad")
        clean = []
        for ln in lines:
            rate_code = ln.get("rate_code")
            if rate_code not in S.MOMS_RATES:
                raise ValueError(f"Okänd momssats {rate_code!r}")
            amount = int(ln.get("amount_ore") or 0)
            if amount <= 0:
                raise ValueError("Beloppet måste vara större än noll")
            rc = _clean_reverse_charge(rate_code, ln.get("reverse_charge"))
            if rc and kind != "expense":
                raise ValueError("Omvänd betalningsskyldighet gäller bara inköp")
            cat = ln.get("category_id")
            if cat is not None:
                self._check_category(int(cat), "expense" if kind == "expense" else "income")
            clean.append({"rate_code": rate_code, "amount_ore": amount,
                          "inclusive": bool(ln.get("inclusive", True)),
                          "category_id": cat, "reverse_charge": rc})
        return json.dumps(clean)

    def create_recurring(self, kind: str, name: str, category_id: int, lines: list,
                         start_date: str, interval_unit: str = "month",
                         interval_count: int = 1, *,
                         supplier_id: Optional[int] = None,
                         customer_id: Optional[int] = None,
                         note: Optional[str] = None, ext_ref: Optional[str] = None,
                         paid_account: str = "bank",
                         end_date: Optional[str] = None) -> int:
        """Create a recurring template. `start_date` is the FIRST occurrence to confirm."""
        if kind not in ("expense", "income"):
            raise ValueError("kind måste vara 'expense' eller 'income'")
        if not (name or "").strip():
            raise ValueError("Ge den återkommande betalningen ett namn")
        if interval_unit not in ("month", "year"):
            raise ValueError("Intervallet måste vara 'month' eller 'year'")
        if int(interval_count) < 1:
            raise ValueError("Intervallet måste vara minst 1")
        if paid_account not in ("bank", "privat"):
            raise ValueError("Okänt betalkonto")
        self._check_category(category_id, "expense" if kind == "expense" else "income")
        lines_json = self._validate_rec_lines(kind, lines)
        if end_date and end_date < start_date:
            raise ValueError("Slutdatum kan inte ligga före startdatum")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO recurring(kind, name, category_id, supplier_id, customer_id, "
                "lines_json, note, ext_ref, paid_account, interval_unit, interval_count, "
                "next_date, end_date, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (kind, name.strip(), category_id, supplier_id, customer_id, lines_json,
                 note, (ext_ref or None), paid_account, interval_unit, int(interval_count),
                 start_date, end_date, _now()))
        return cur.lastrowid

    def update_recurring(self, recurring_id: int, **changes) -> None:
        """
        Edit a recurring template. The change applies to the WHOLE SERIES GOING FORWARD —
        every occurrence not yet confirmed, including the one currently due. Occurrences
        already confirmed are booked verifikationer and are never touched (correct them
        with a rättelse if they are wrong).
        """
        row = self.conn.execute("SELECT * FROM recurring WHERE id=?", (recurring_id,)).fetchone()
        if row is None:
            raise KeyError(f"No recurring {recurring_id}")
        sets: dict = {}
        if "lines" in changes and changes["lines"] is not None:
            sets["lines_json"] = self._validate_rec_lines(row["kind"], changes["lines"])
        for f in self._REC_FIELDS:
            if f not in changes or changes[f] is None:
                continue
            v = changes[f]
            if f == "category_id":
                self._check_category(int(v), "expense" if row["kind"] == "expense" else "income")
            if f == "interval_unit" and v not in ("month", "year"):
                raise ValueError("Intervallet måste vara 'month' eller 'year'")
            if f == "interval_count" and int(v) < 1:
                raise ValueError("Intervallet måste vara minst 1")
            if f == "paid_account" and v not in ("bank", "privat"):
                raise ValueError("Okänt betalkonto")
            if f == "name" and not str(v).strip():
                raise ValueError("Namnet kan inte vara tomt")
            sets[f] = int(v) if f == "active" else v
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.conn:
            self.conn.execute(f"UPDATE recurring SET {cols}, updated_at=? WHERE id=?",
                              (*sets.values(), _now(), recurring_id))

    def delete_recurring(self, recurring_id: int) -> dict:
        """Remove a template. Refused once it has produced booked occurrences — those are
        part of the books, so the series is paused (active=0) instead, keeping its history."""
        row = self.conn.execute("SELECT id FROM recurring WHERE id=?", (recurring_id,)).fetchone()
        if row is None:
            raise KeyError(f"No recurring {recurring_id}")
        if self.conn.execute("SELECT 1 FROM recurring_occurrence WHERE recurring_id=? "
                             "AND status='booked' LIMIT 1", (recurring_id,)).fetchone():
            raise InvalidState(
                "Serien har redan bokförda betalningar och kan inte tas bort — pausa den "
                "istället, så finns historiken kvar")
        with self.conn:
            self.conn.execute("DELETE FROM recurring_occurrence WHERE recurring_id=?",
                              (recurring_id,))
            self.conn.execute("DELETE FROM recurring WHERE id=?", (recurring_id,))
        return {"deleted": True, "id": recurring_id}

    def _rec_row(self, row) -> dict:
        d = dict(row)
        d["lines"] = json.loads(d.pop("lines_json") or "[]")
        d["total_ore"] = sum(
            compute_moms_figures(ln["amount_ore"], ln["rate_code"],
                                 False if ln.get("reverse_charge") else ln.get("inclusive", True))[2]
            for ln in d["lines"])
        return d

    def list_recurring(self, active_only: bool = False) -> list[dict]:
        """All recurring templates with their lines, total and a `due` flag."""
        where = " WHERE active=1" if active_only else ""
        today = _now()[:10]
        out = []
        for row in self.conn.execute(
                f"SELECT * FROM recurring{where} ORDER BY active DESC, next_date, id"):
            d = self._rec_row(row)
            d["due"] = bool(d["active"]) and d["next_date"] <= today and (
                not d["end_date"] or d["next_date"] <= d["end_date"])
            d["booked_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM recurring_occurrence WHERE recurring_id=? "
                "AND status='booked'", (row["id"],)).fetchone()[0]
            out.append(d)
        return out

    def due_recurring(self, as_of: Optional[str] = None) -> list[dict]:
        """Active templates whose next occurrence has fallen due — the confirm worklist."""
        as_of = (as_of or _now())[:10]
        return [d for d in self.list_recurring(active_only=True)
                if d["next_date"] <= as_of and (not d["end_date"] or d["next_date"] <= d["end_date"])]

    def _advance_recurring(self, rec: dict, due_date: str) -> None:
        """Move the series past `due_date`, deactivating it when it runs past end_date."""
        nxt = self._rec_next_date(due_date, rec["interval_unit"], rec["interval_count"])
        done = bool(rec["end_date"]) and nxt > rec["end_date"]
        self.conn.execute(
            "UPDATE recurring SET next_date=?, active=?, updated_at=? WHERE id=?",
            (nxt, 0 if done else rec["active"], _now(), rec["id"]))

    def _rec_due(self, recurring_id: int, date: Optional[str]) -> tuple[dict, str]:
        rec = self.conn.execute("SELECT * FROM recurring WHERE id=?", (recurring_id,)).fetchone()
        if rec is None:
            raise KeyError(f"No recurring {recurring_id}")
        rec = self._rec_row(rec)
        due_date = date or rec["next_date"]
        if self.conn.execute("SELECT 1 FROM recurring_occurrence WHERE recurring_id=? "
                             "AND due_date=?", (recurring_id, due_date)).fetchone():
            raise InvalidState(f"{due_date} är redan hanterad i den här serien")
        return rec, due_date

    def confirm_recurring(self, recurring_id: int, *, date: Optional[str] = None,
                          lines: Optional[list] = None, paid_date: Optional[str] = None,
                          note: Optional[str] = None, ext_ref: Optional[str] = None,
                          paid_account: Optional[str] = None) -> dict:
        """
        Confirm one occurrence: create the real transaktion from the template (booking it
        when `paid_date` is given) and move the series to the next date.

        `lines` overrides the amount for THIS occurrence only (a subscription that varies
        month to month) — the template itself is untouched. To change the series, edit the
        template instead.
        """
        rec, due_date = self._rec_due(recurring_id, date)
        use_lines = (json.loads(self._validate_rec_lines(rec["kind"], lines))
                     if lines is not None else rec["lines"])
        acct = paid_account or rec["paid_account"]
        note = note if note is not None else rec["note"]
        ext_ref = ext_ref if ext_ref is not None else rec["ext_ref"]
        text = f"{rec['name']} ({due_date})" + (f" – {note}" if note else "")
        if rec["kind"] == "expense":
            res = self.record_expense(
                rec["supplier_id"], rec["category_id"], use_lines, due_date,
                note=text, ext_ref=ext_ref, paid_date=paid_date, paid_account=acct)
        else:
            if not rec["customer_id"]:
                raise InvalidState("Den återkommande inkomsten saknar kund")
            res = self.record_income(rec["customer_id"], rec["category_id"], use_lines,
                                     due_date, note=text, paid_date=paid_date)
        with self.conn:
            self.conn.execute(
                "INSERT INTO recurring_occurrence(recurring_id, due_date, status, "
                "transaktion_id, created_at) VALUES (?,?,'booked',?,?)",
                (recurring_id, due_date, res["transaktion_id"], _now()))
            self._advance_recurring(rec, due_date)
        return {**res, "recurring_id": recurring_id, "due_date": due_date}

    def skip_recurring(self, recurring_id: int, date: Optional[str] = None) -> dict:
        """Skip one occurrence (a month you were not charged) without booking anything."""
        rec, due_date = self._rec_due(recurring_id, date)
        with self.conn:
            self.conn.execute(
                "INSERT INTO recurring_occurrence(recurring_id, due_date, status, created_at) "
                "VALUES (?,?,'skipped',?)", (recurring_id, due_date, _now()))
            self._advance_recurring(rec, due_date)
        return {"recurring_id": recurring_id, "due_date": due_date, "status": "skipped"}

    def recurring_history(self, recurring_id: int) -> list[dict]:
        """Every occurrence already confirmed or skipped, newest first."""
        return [dict(r) for r in self.conn.execute(
            "SELECT o.*, t.verifikation_id, v.ver_number FROM recurring_occurrence o "
            "LEFT JOIN transaktion t ON t.id = o.transaktion_id "
            "LEFT JOIN verifikation v ON v.id = t.verifikation_id "
            "WHERE o.recurring_id=? ORDER BY o.due_date DESC", (recurring_id,))]

    # ==================================================================
    # Booking (money moved) — creates the immutable verifikation
    # ==================================================================

    def register_payment(self, transaktion_id: int, payment_date: str, *,
                         extra_fee_ore: int = 0,
                         extra_fee_category_id: Optional[int] = None,
                         note: Optional[str] = None,
                         paid_account: str = "bank") -> dict:
        """
        Book a pending transaktion: create the verifikation + balanced postings,
        assign the next verifikationsnummer, and mark the transaktion paid.

        For a RUT sale this books the CUSTOMER portion (bank gets inc − rut, the rut
        part becomes a receivable) and advances the claim to 'customer_paid'.

        `extra_fee_ore` (+ `extra_fee_category_id`) books an extra MOMSFRI cost on the
        SAME verifikation — e.g. a Klarna/Qliro delbetalnings-/fakturaavgift for paying
        an inköp later. It debits the fee's expense konto and adds it to the bank outflow,
        so the payment still nets to zero. Only valid for a pending inköp (direction 'in').

        `note` is a free reference/comment appended to the verifikation's text (e.g. an
        OCR- or betalningsreferens), so it becomes part of the bookkeeping description.
        """
        t = self.conn.execute(
            "SELECT * FROM transaktion WHERE id=?", (transaktion_id,)
        ).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        if t["status"] == "paid":
            raise InvalidState("Transaktion is already paid")

        note_suffix = f" – {note.strip()}" if note and note.strip() else ""

        fee = int(extra_fee_ore or 0)
        fee_konto = None
        if fee:
            if fee < 0:
                raise ValueError("Avgiften kan inte vara negativ")
            if t["direction"] != "in" or t["verifikation_id"] is not None:
                raise InvalidState("Extra avgift kan bara läggas på ett obetalt inköp")
            if not extra_fee_category_id:
                raise ValueError("Välj ett konto (kategori) för avgiften")
            self._check_category(int(extra_fee_category_id), "expense")
            fee_konto = self._category_konto(int(extra_fee_category_id))

        # Funding source for an inköp payment: the company bank account (1930) or private
        # money (2018 Egna insättningar — you paid a firma cost from your own pocket).
        if paid_account not in ("bank", "privat"):
            raise ValueError("Okänt betalkonto")
        if paid_account == "privat" and (t["direction"] != "in" or t["verifikation_id"] is not None):
            raise InvalidState("Privat insättning kan bara väljas för ett obetalt inköp")
        outflow_konto = self._sys_account(
            "account_egna_insattningar" if paid_account == "privat" else "account_bank")
        outflow_label = "betalning (privat insättning)" if paid_account == "privat" else "betalning"

        ex, moms_by_rate, inc = self._sum_moms(transaktion_id)
        sum_moms = sum(moms_by_rate.values())

        claim = self.conn.execute(
            "SELECT * FROM rut_claim WHERE transaktion_id=?", (transaktion_id,)
        ).fetchone()
        rut = claim["rut_amount_ore"] if claim else 0

        # Fakturametod: the invoice was already booked at issue (kundfordran). Payment
        # only settles the receivable (bank <- kundfordran); income/moms already booked.
        if t["verifikation_id"] is not None:
            # Fakturametod: settle the kundfordran (booked exact at issue). Öresavrundning
            # — the customer pays whole kronor, so the bank gets the rounded amount and the
            # öre difference clears against 3740 (moms/underlag untouched, per Skatteverket).
            cust_exact = inc - rut
            round_cust = _round_to_krona(cust_exact)
            postings = []
            if cust_exact:
                postings.append((self._sys_account("account_bank"), round_cust, "inbetalning"))
                postings.append((self._sys_account("account_kundfordran"), -cust_exact,
                                 "kvitta kundfordran"))
                if round_cust != cust_exact:
                    postings.append((self._sys_account("account_ores_kronutjamning"),
                                     cust_exact - round_cust, "öresavrundning"))
            with self.conn:
                vid, number = self._post_verifikation(
                    payment_date, payment_date, "Betalning faktura" + note_suffix, postings)
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
            # Omvänd betalningsskyldighet: the supplier invoiced without moms, so you
            # report BOTH sides — the computed moms is debited as ingående moms on 2645
            # and credited as utgående moms on 26x4. The two cancel in the ledger (and in
            # the momsdeklaration, with full avdragsrätt), and no money moves for it.
            rc_by_rate = self._reverse_charge_moms(transaktion_id)
            rc_total = sum(rc_by_rate.values())
            normal_moms = sum_moms - rc_total
            if normal_moms:
                postings.append((self._sys_account("account_ingaende_moms"), normal_moms,
                                 "ingående moms"))
            if rc_total:
                postings.append((self._sys_account("account_ing_moms_utland"), rc_total,
                                 "beräknad ingående moms (omvänd betalningsskyldighet)"))
                for rate_code, m in sorted(rc_by_rate.items()):
                    if m:
                        postings.append((self._sys_account(_UTG_MOMS_OMVAND_KEY[rate_code]),
                                         -m, f"utgående moms omvänd skattskyldighet {rate_code}%"))
            # Öresavrundning (supplier rounded to whole kronor): the bank pays the rounded
            # total; ex-moms + ingående moms stay exact and the öre diff goes to 3740.
            # An extra momsfri betaltjänstavgift (Klarna/Qliro) is debited to its own konto
            # and added to the bank outflow (the fee is exact, never rounded).
            cash_exact = inc - rc_total          # reverse-charge moms is never paid out
            round_cash = _round_to_krona(cash_exact) if t["ores_rounding"] else cash_exact
            postings.append((outflow_konto, -(round_cash + fee), outflow_label))
            if round_cash != cash_exact:
                postings.append((self._sys_account("account_ores_kronutjamning"),
                                 round_cash - cash_exact, "öresavrundning"))
            if fee:
                postings.append((fee_konto, fee, "betaltjänstavgift (momsfri)"))
            text = "Utgift" + note_suffix
        elif rut:  # 'out' — RUT/ROT faktura: öresavrundning on the customer's summa att betala
            # Per avrundningslagen the customer pays whole kronor, but per Skatteverket's
            # ställningstagande the avrundning may NOT touch the beskattningsunderlag or the
            # moms — those stay exact. The öre difference goes to 3740 Öres- och
            # kronutjämning, exactly like the "Öresavrundning"-raden on the faktura.
            cust_exact = inc - rut          # kundens del (Skatteverkets del = rut on 1513)
            round_cust = _round_to_krona(cust_exact)
            postings = [(self._sys_account("account_bank"), round_cust, "inbetalning"),
                        (self._sys_account("account_rut_fordran"), rut, "husavdrag fordran")]
            postings.extend((k, -ex_k, "försäljning") for k, ex_k in income_splits)
            for rate_code, m in moms_by_rate.items():
                if m and rate_code in _UTG_MOMS_KEY:
                    postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -m, f"utgående moms {rate_code}%"))
            if round_cust != cust_exact:
                postings.append((self._sys_account("account_ores_kronutjamning"),
                                 cust_exact - round_cust, "öresavrundning"))
            text = "Försäljning" + note_suffix
        else:  # 'out' — plain sale (not a faktura): booked exact, no öresavrundning
            postings = [(self._sys_account("account_bank"), inc, "inbetalning")]
            postings.extend((k, -ex_k, "försäljning") for k, ex_k in income_splits)
            for rate_code, m in moms_by_rate.items():
                if m and rate_code in _UTG_MOMS_KEY:
                    postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -m, f"utgående moms {rate_code}%"))
            text = "Försäljning" + note_suffix

        with self.conn:
            # Persist the fee as a momsfri moms_line (its own category) so the result report
            # picks it up as a cost; momsfri → nothing in the momsdeklaration. Atomic with
            # the booking, so a period-lock refusal rolls the fee line back too.
            if fee:
                self.conn.execute(
                    "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                    "moms_ore, inc_moms_ore, bas_konto) VALUES (?, 'momsfri', ?, ?, 0, ?, ?)",
                    (transaktion_id, int(extra_fee_category_id), fee, fee, fee_konto))
            # Carry the kvitto-/fakturanummer onto the verifikation, so the grundbok shows
            # the same reference a manual entry can be given by hand.
            vid, number = self._post_verifikation(payment_date, payment_date, text, postings,
                                                  ext_ref=t["ext_ref"])
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
                                          relation_note: Optional[str] = None,
                                          reference: Optional[str] = None) -> dict:
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
        if reference and reference.strip():
            text += f" ({reference.strip()})"

        shortfall_invoice_id = None
        with self.conn:
            vid, number = self._post_verifikation(payment_date, payment_date, text, postings)
            if interp == "partial":
                shortfall_invoice_id = self._create_husavdrag_shortfall_invoice(
                    claim, diff, payment_date, relation_note)
            self.conn.execute(
                "UPDATE rut_claim SET state='skatteverket_paid', skatteverket_payment_date=?, "
                "skatteverket_verifikation_id=?, skatteverket_received_ore=?, "
                "shortfall_invoice_id=?, skatteverket_reference=? WHERE id=?",
                (payment_date, vid, received, shortfall_invoice_id,
                 (reference.strip() if reference and reference.strip() else None), rut_claim_id),
            )
        return {"verifikation_id": vid, "ver_number": number, "interpretation": interp,
                "claimed_ore": claimed, "received_ore": received, "difference_ore": diff,
                "shortfall_invoice_id": shortfall_invoice_id}

    def attach_rut_receipt(self, rut_claim_id: int, data: bytes, mime: str) -> dict:
        """
        Store Skatteverket's kvittens for a RUT/ROT payout, encrypted with the book DEK.
        It is tagged with the claim (and filed under the claim's sale transaktion so it
        travels in the .buyn bundle like any receipt).
        """
        claim = self.conn.execute(
            "SELECT transaktion_id FROM rut_claim WHERE id=?", (rut_claim_id,)).fetchone()
        if claim is None:
            raise KeyError(f"No rut_claim {rut_claim_id}")
        return self.attach_receipt(claim["transaktion_id"], data, mime,
                                   original_format="digital", rut_claim_id=rut_claim_id)

    def list_rut_receipts(self, rut_claim_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, rut_claim_id, mime, byte_size, created_at FROM receipt "
            "WHERE rut_claim_id=? ORDER BY id", (rut_claim_id,)).fetchall()]

    def next_rut_reference(self) -> str:
        """Suggest the next RUT/ROT begäran reference by continuing the numeric sequence
        of the references already booked (own sequence, independent of faktura numbers /
        makulering). E.g. last "RUT4" -> "RUT5"; none yet -> "RUT1". User-editable."""
        rows = self.conn.execute(
            "SELECT skatteverket_reference FROM rut_claim "
            "WHERE skatteverket_reference IS NOT NULL AND TRIM(skatteverket_reference) != ''"
        ).fetchall()
        best_num, prefix = 0, "RUT"
        for r in rows:
            m = re.match(r"^(.*?)(\d+)\s*$", (r[0] or "").strip())
            if m and int(m.group(2)) >= best_num:
                best_num = int(m.group(2))
                prefix = m.group(1) or "RUT"      # keep the user's own prefix/format
        return f"{prefix}{best_num + 1}"

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

    def transaktion_corrected(self, transaktion_id: int) -> bool:
        """True if this transaktion's booking has been rättat (a rättelse references its
        verifikation). Used to flag it as 'Rättad' in Transaktioner/Inköp/Ordrar."""
        row = self.conn.execute("SELECT verifikation_id FROM transaktion WHERE id=?",
                                (transaktion_id,)).fetchone()
        if not row or row["verifikation_id"] is None:
            return False
        return self.conn.execute("SELECT 1 FROM verifikation WHERE rattelse_of=?",
                                 (row["verifikation_id"],)).fetchone() is not None

    def rebook_transaktion(self, transaktion_id: int, corrections: dict,
                           reason: str = "Rättat baskonto") -> dict:
        """
        Correct the BAS-konton / moms of a booked PLAIN income or expense by re-picking
        the category (and optionally the moms rate) per line, WITHOUT retyping the
        amounts: reverse the current booking (a rättelse) and post a NEW verifikation
        with the SAME amounts but the corrected accounts. The öre-exact settlement side
        (bank / öresavrundning) is carried over untouched, so every verifikation balances.

        `corrections` maps a moms_line id -> {"category_id": int, "rate_code": str} (both
        optional; omitted keeps the current value). Changing the rate keeps the line's
        inc-moms total constant and re-splits ex/moms (so a "momsfri men bokförd med moms"
        line becomes pure income).

        Fakturor and RUT/ROT are refused — those are corrected with a kreditfaktura.
        """
        t = self.conn.execute(
            "SELECT id, direction, category_id, verifikation_id FROM transaktion WHERE id=?",
            (transaktion_id,)).fetchone()
        if t is None:
            raise KeyError(f"No transaktion {transaktion_id}")
        vid = t["verifikation_id"]
        if vid is None:
            raise InvalidState("Transaktionen är inte bokförd ännu")
        if self.conn.execute("SELECT 1 FROM invoice WHERE transaktion_id=?", (transaktion_id,)).fetchone():
            raise InvalidState("Fakturor rättas med en kreditfaktura, inte ombokföring")
        if self.conn.execute("SELECT 1 FROM rut_claim WHERE transaktion_id=?", (transaktion_id,)).fetchone():
            raise InvalidState("RUT/ROT-ärenden rättas med en kreditfaktura")
        if t["category_id"] is None:
            raise InvalidState(
                "Ett inventarieinköp har inget baskonto via kategori — rätta det med en "
                "rättelse och ett manuellt verifikat")
        if self.conn.execute("SELECT 1 FROM verifikation WHERE rattelse_of=?", (vid,)).fetchone():
            raise InvalidState("Verifikatet är redan rättat")

        kind = "income" if t["direction"] == "out" else "expense"
        orig = self.conn.execute(
            "SELECT id, rate_code, category_id, inc_moms_ore FROM moms_line "
            "WHERE transaktion_id=? ORDER BY id", (transaktion_id,)).fetchall()
        corrected = []
        for ln in orig:
            c = corrections.get(ln["id"]) or corrections.get(str(ln["id"])) or {}
            new_cat = c.get("category_id", ln["category_id"])
            new_rate = c.get("rate_code") or ln["rate_code"]
            if new_rate not in S.MOMS_RATES:
                raise ValueError(f"Unknown moms rate {new_rate!r}")
            if new_cat is not None:
                self._check_category(int(new_cat), kind)
            ex, moms, inc = compute_moms_figures(ln["inc_moms_ore"], new_rate, True)
            corrected.append({"category_id": new_cat, "rate_code": new_rate,
                              "ex": ex, "moms": moms, "inc": inc})

        ver_date = self.conn.execute("SELECT ver_date FROM verifikation WHERE id=?",
                                     (vid,)).fetchone()["ver_date"]
        # 1) reverse the current booking (rättelse: negates postings + report moms_lines).
        self.reverse_verifikation(vid, reason)
        # 2) re-book: keep the settlement postings (bank / öresavrundning), rebuild the
        #    income/expense + moms postings from the corrected lines.
        bank = self._sys_account("account_bank")
        ores = self._sys_account("account_ores_kronutjamning")
        keep = [(p["bas_konto"], p["amount_ore"], p["text"]) for p in self.conn.execute(
            "SELECT bas_konto, amount_ore, text FROM posting WHERE verifikation_id=?", (vid,))
            if p["bas_konto"] in (bank, ores)]
        op = 1 if t["direction"] == "in" else -1        # expense debits, income credits
        fb = t["category_id"]
        inc_agg: dict[int, int] = {}
        for cl in corrected:
            cat = cl["category_id"] if cl["category_id"] is not None else fb
            cl["bas_konto"] = self._category_konto(cat)
            inc_agg[cl["bas_konto"]] = inc_agg.get(cl["bas_konto"], 0) + cl["ex"]
        postings = list(keep)
        for konto, ex in sorted(inc_agg.items()):
            postings.append((konto, op * ex, "omkontering"))
        moms_agg: dict[str, int] = {}
        for cl in corrected:
            if cl["moms"]:
                moms_agg[cl["rate_code"]] = moms_agg.get(cl["rate_code"], 0) + cl["moms"]
        for rate, moms in sorted(moms_agg.items()):
            if t["direction"] == "in":
                postings.append((self._sys_account("account_ingaende_moms"), moms,
                                 f"ingående moms {rate}%"))
            elif rate in _UTG_MOMS_KEY:
                postings.append((self._sys_account(_UTG_MOMS_KEY[rate]), -moms,
                                 f"utgående moms {rate}%"))
        with self.conn:
            new_vid, new_num = self._post_verifikation(
                ver_date, _now()[:10], f"Ombokföring (rättat baskonto) av ver {vid}: {reason}",
                postings)
            # Report clone with the CORRECTED lines (positive) so the momsdeklaration +
            # result report attribute the corrected accounts/moms to the new verifikation.
            src = self.conn.execute(
                "SELECT direction, category_id, supplier_id, customer_id FROM transaktion "
                "WHERE id=?", (transaktion_id,)).fetchone()
            cur = self.conn.execute(
                "INSERT INTO transaktion(direction, category_id, supplier_id, customer_id, "
                "trans_date, status, verifikation_id, note, created_at) "
                "VALUES (?,?,?,?,?, 'paid', ?, ?, ?)",
                (src["direction"], src["category_id"], src["supplier_id"], src["customer_id"],
                 ver_date, new_vid, "ombokföring", _now()))
            rid = cur.lastrowid
            for cl in corrected:
                self.conn.execute(
                    "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                    "moms_ore, inc_moms_ore, bas_konto) VALUES (?,?,?,?,?,?,?)",
                    (rid, cl["rate_code"], cl["category_id"], cl["ex"], cl["moms"], cl["inc"],
                     cl["bas_konto"]))
        return {"verifikation_id": new_vid, "ver_number": new_num}

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
            "rattelse_of, egenupprattad, motivering, ext_ref, kommentar FROM verifikation"
            + clause + " ORDER BY ver_number", args)]
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
                                reg_date: Optional[str] = None,
                                egenupprattad: bool = False,
                                motivering: Optional[str] = None,
                                ext_ref: Optional[str] = None,
                                kommentar: Optional[str] = None) -> dict:
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
        if egenupprattad and not (motivering or "").strip():
            raise ValueError(
                "En egenupprättad verifikation behöver en motivering — den ÄR underlaget "
                "(BFL 5 kap.): vad den avser och hur beloppet är bestämt")
        with self.conn:
            vid, number = self._post_verifikation(
                ver_date, reg_date or ver_date, text.strip(), postings,
                egenupprattad=egenupprattad, motivering=motivering,
                ext_ref=ext_ref, kommentar=kommentar)
        return {"verifikation_id": vid, "ver_number": number}

    # ------------------------------------------------------------------
    # Privat tillgång in i verksamheten (tillskott)
    # ------------------------------------------------------------------
    #
    # An enskild näringsidkare and their firma are the same legal person, so bringing
    # privately-owned property into the business is not a purchase — it is a TILLSKOTT:
    # the asset is debited and eget kapital (2018 Egna insättningar) is credited. No money
    # moves and there is NO moms: the acquisition was private, so no avdragsrätt arose,
    # and it does not arise afterwards.
    #
    # Two things decide the booking:
    #   * the VALUE — the lower of what it cost privately and its market value on the day
    #     it starts being used in the business (we never invent it; the user states it and
    #     the motivation records how they arrived at it);
    #   * the AMOUNT vs halva prisbasbeloppet — under it the asset may be expensed
    #     directly (5410), over it it is capitalised (1220/1250) and depreciated.
    #
    # There is no external document behind the entry, so it is an EGENUPPRÄTTAD
    # VERIFIKATION (BFL 5 kap.) and the motivation is the underlag.

    @staticmethod
    def _kr(ore: int) -> str:
        """Swedish money for human-readable text: 1850000 -> '18 500,00 kr'."""
        return f"{ore / 100:,.2f}".replace(",", " ").replace(".", ",") + " kr"

    def _halva_prisbasbeloppet_ore(self) -> int:
        """The direktavdrag threshold: half a prisbasbelopp (config, updated yearly)."""
        return int(self._config("prisbasbelopp_ore")) // 2

    def private_asset_preview(self, amount_ore: int, *, mode: str = "auto",
                              business_pct_centi: int = 10000,
                              konto: Optional[int] = None) -> dict:
        """
        Work out how a private asset would be booked WITHOUT booking it, so the UI can
        explain the treatment (and why) before the user commits.
        """
        amount_ore = int(amount_ore)
        if amount_ore <= 0:
            raise ValueError("Ange tillgångens värde vid överföringen")
        pct = int(business_pct_centi)
        if not 0 < pct <= 10000:
            raise ValueError("Verksamhetsandelen måste vara mellan 0 och 100 %")
        if mode not in ("auto", "direktavdrag", "aktivera"):
            raise ValueError("mode måste vara auto, direktavdrag eller aktivera")

        booked = round(amount_ore * pct / 10000)
        threshold = self._halva_prisbasbeloppet_ore()
        suggested = "direktavdrag" if booked <= threshold else "aktivera"
        chosen = suggested if mode == "auto" else mode
        default_konto = self._sys_account(
            "account_forbrukningsinventarier" if chosen == "direktavdrag"
            else "account_inventarier")
        konto = int(konto) if konto else default_konto
        entry = catalog_entry(konto)
        konto_namn = self._account_name(konto) or (entry["name"] if entry else None)

        notes = []
        if chosen == "aktivera":
            notes.append(
                "Tillgången aktiveras som inventarie och ska skrivas av över sin "
                "nyttjandeperiod — bokför avskrivningen separat vid bokslutet "
                "(7832 mot ackumulerade avskrivningar).")
        if chosen == "direktavdrag" and booked > threshold:
            notes.append(
                f"OBS: beloppet överstiger halva prisbasbeloppet ({self._kr(threshold)}) — "
                "direktavdrag är normalt inte tillåtet.")
        if chosen == "aktivera" and booked <= threshold:
            notes.append(
                "Beloppet ligger under halva prisbasbeloppet, så direktavdrag hade "
                "också varit möjligt. Att aktivera är tillåtet.")
        if pct < 10000:
            notes.append(
                f"Endast verksamhetsandelen ({pct / 100:g} %) bokförs. Motivera "
                "fördelningen i underlaget.")
        return {
            "amount_ore": amount_ore,
            "business_pct_centi": pct,
            "booked_ore": booked,
            "threshold_ore": threshold,
            "suggested": suggested,
            "treatment": chosen,
            "bas_konto": konto,
            "konto_namn": konto_namn,
            "motkonto": self._sys_account("account_egna_insattningar"),
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Inventarieinköp (an asset bought BY the firma)
    # ------------------------------------------------------------------
    #
    # Distinct from `book_private_asset_contribution` (property you already owned
    # privately): this is a normal purchase with a supplier invoice, so the moms IS
    # deductible and money really leaves the firma. What makes it special is that the
    # cost may not belong in the year's result at all — an inventarie over the threshold
    # is capitalised (1220/1250) and written off over its useful life instead.
    #
    # It is created as an ordinary `transaktion` with NO category and the konto frozen
    # straight onto its moms_line, which means every existing mechanism keeps working:
    # receipt attachment, kvitto-/fakturanummer, supplier, paid-now vs leverantörsfaktura,
    # privat insättning, öresavrundning and the whole register_payment booking path.

    def asset_purchase_preview(self, amount_ore: int, *, rate_code: str = "25",
                               inclusive: bool = True, mode: str = "auto",
                               konto: Optional[int] = None,
                               useful_life_years: Optional[int] = None) -> dict:
        """
        Decide how a tool/asset purchase should be booked WITHOUT booking it.

        The threshold is compared against the price EXCLUDING moms (anskaffningsvärdet).
        A useful life of at most three years allows an immediate deduction whatever the
        price (IL 18 kap. 4 §), so it overrides the amount test.
        """
        amount_ore = int(amount_ore)
        if amount_ore <= 0:
            raise ValueError("Ange vad inventarien kostade")
        if rate_code not in S.MOMS_RATES:
            raise ValueError(f"Okänd momssats {rate_code!r}")
        if mode not in ("auto", "direktavdrag", "aktivera"):
            raise ValueError("mode måste vara auto, direktavdrag eller aktivera")
        life = int(useful_life_years) if useful_life_years else None
        if life is not None and life < 1:
            raise ValueError("Livslängden måste vara minst 1 år")

        ex, moms, inc = compute_moms_figures(amount_ore, rate_code, inclusive)
        threshold = self._halva_prisbasbeloppet_ore()
        short_life = life is not None and life <= 3
        suggested = "direktavdrag" if (ex <= threshold or short_life) else "aktivera"
        chosen = suggested if mode == "auto" else mode
        default_konto = self._sys_account(
            "account_forbrukningsinventarier" if chosen == "direktavdrag"
            else "account_inventarier")
        konto = int(konto) if konto else default_konto
        entry = catalog_entry(konto)
        konto_namn = self._account_name(konto) or (entry["name"] if entry else None)

        notes = []
        if chosen == "aktivera":
            notes.append(
                "Inventarien bokförs som en tillgång och kostnadsförs genom avskrivning "
                "över nyttjandeperioden — den belastar alltså INTE årets resultat direkt. "
                "Bokför avskrivningen vid bokslutet (7832 mot 1229).")
        elif short_life and ex > threshold:
            notes.append(
                f"Beloppet överstiger halva prisbasbeloppet ({self._kr(threshold)}), men "
                f"en ekonomisk livslängd på {life} år ger direktavdrag ändå "
                "(IL 18 kap. 4 §). Motivera livslängden i noteringen.")
        elif chosen == "direktavdrag" and ex > threshold:
            notes.append(
                f"OBS: {self._kr(ex)} exkl. moms överstiger halva prisbasbeloppet "
                f"({self._kr(threshold)}). Direktavdrag kräver att den ekonomiska "
                "livslängden är högst tre år.")
        elif chosen == "aktivera" and ex <= threshold:
            notes.append(
                "Beloppet ligger under halva prisbasbeloppet, så direktavdrag hade också "
                "varit möjligt. Att aktivera är tillåtet.")
        notes.append(
            "Hör flera delar ihop och fungerar tillsammans ska de bedömas som EN enhet "
            "mot gränsen, inte var för sig.")
        return {
            "ex_moms_ore": ex, "moms_ore": moms, "inc_moms_ore": inc,
            "rate_code": rate_code, "threshold_ore": threshold,
            "suggested": suggested, "treatment": chosen,
            "bas_konto": konto, "konto_namn": konto_namn,
            "useful_life_years": life, "notes": notes,
        }

    def book_asset_purchase(self, description: str, amount_ore: int, trans_date: str, *,
                            rate_code: str = "25", inclusive: bool = True,
                            mode: str = "auto", konto: Optional[int] = None,
                            useful_life_years: Optional[int] = None,
                            supplier_id: Optional[int] = None,
                            ext_ref: Optional[str] = None,
                            note: Optional[str] = None,
                            receipt_original_format: Optional[str] = None,
                            ores_rounding: bool = False,
                            paid_date: Optional[str] = None,
                            paid_account: str = "bank") -> dict:
        """
        Buy a tool/inventarie for the firma. Books the net to the asset or cost konto and
        the moms as deductible ingående moms, exactly like any other inköp — the only
        difference is that the konto may be a balance-sheet one.

        Leaving `paid_date` out keeps it pending (a leverantörsfaktura to be marked paid
        later); giving it books the payment immediately.
        """
        description = (description or "").strip()
        if not description:
            raise ValueError("Beskriv inventarien (t.ex. modell och serienummer)")
        plan = self.asset_purchase_preview(
            amount_ore, rate_code=rate_code, inclusive=inclusive, mode=mode, konto=konto,
            useful_life_years=useful_life_years)
        self.ensure_account(plan["bas_konto"],
                            plan["konto_namn"] or f"Konto {plan['bas_konto']}")

        life = plan["useful_life_years"]
        full_note = description
        if life:
            full_note += f" (bedömd livslängd {life} år)"
        if note and note.strip():
            full_note += f" – {note.strip()}"

        tid = self._insert_transaktion(
            direction="in", category_id=None, supplier_id=supplier_id, customer_id=None,
            trans_date=trans_date, note=full_note,
            receipt_original_format=receipt_original_format, snapshot_enc=None,
            ext_ref=(ext_ref.strip() if ext_ref and ext_ref.strip() else None),
        )
        with self.conn:
            # No category: the konto is frozen straight onto the line, so every booking
            # path (which groups by moms_line.bas_konto) picks it up unchanged.
            self.conn.execute(
                "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                "moms_ore, inc_moms_ore, bas_konto) VALUES (?,?,NULL,?,?,?,?)",
                (tid, plan["rate_code"], plan["ex_moms_ore"], plan["moms_ore"],
                 plan["inc_moms_ore"], plan["bas_konto"]))
            if ores_rounding:
                self.conn.execute("UPDATE transaktion SET ores_rounding=1 WHERE id=?", (tid,))
        result = {"transaktion_id": tid,
                  **{k: plan[k] for k in ("treatment", "bas_konto", "konto_namn",
                                          "ex_moms_ore", "moms_ore", "inc_moms_ore",
                                          "threshold_ore", "notes")}}
        if plan["treatment"] == "aktivera":
            # Into the anläggningsregister, so the yearly avskrivning can find it.
            result["fixed_asset_id"] = self.add_fixed_asset(
                description, plan["ex_moms_ore"], trans_date,
                asset_konto=plan["bas_konto"], useful_life_years=life,
                transaktion_id=tid, note=(note.strip() if note and note.strip() else None))
        if paid_date:
            result.update(self.register_payment(tid, paid_date, paid_account=paid_account))
        return result

    # ------------------------------------------------------------------
    # Anläggningsregister + avskrivningar
    # ------------------------------------------------------------------
    #
    # Capitalising an asset is only half the job: it has to be written off over its useful
    # life, or the cost never reaches the result at all. The register makes the 1220
    # balance followable (what it consists of, how much is already written off) and the
    # yearly routine turns that into a balanced verifikation: 7832 debet / 1229 kredit.
    #
    # Straight-line over `useful_life_years`, with a FULL year in the year of acquisition
    # (which is how the tax rules treat inventarier — the deduction does not depend on the
    # month you bought it). The final year takes the remainder, so the accumulated
    # depreciation lands exactly on the anskaffningsvärde and never overshoots it.

    @staticmethod
    def _accumulated_konto_for(asset_konto: int) -> int:
        """BAS convention: the group's '9' konto holds the accumulated depreciation
        (1220 -> 1229, 1250 -> 1259)."""
        return (int(asset_konto) // 10) * 10 + 9

    def add_fixed_asset(self, description: str, acquisition_ore: int, acquired_date: str, *,
                        asset_konto: Optional[int] = None,
                        useful_life_years: Optional[int] = None,
                        accumulated_konto: Optional[int] = None,
                        expense_konto: Optional[int] = None,
                        transaktion_id: Optional[int] = None,
                        note: Optional[str] = None) -> int:
        """
        Put an asset in the register. `book_asset_purchase` calls this automatically when
        it capitalises; call it by hand for an asset bought before you started using the
        app (that only registers it — its 1220 balance must already be in the books).
        """
        description = (description or "").strip()
        if not description:
            raise ValueError("Beskriv tillgången")
        acquisition_ore = int(acquisition_ore)
        if acquisition_ore <= 0:
            raise ValueError("Anskaffningsvärdet måste vara större än noll")
        life = int(useful_life_years or self._config("default_avskrivningstid_ar"))
        if life < 1:
            raise ValueError("Avskrivningstiden måste vara minst 1 år")
        asset_konto = int(asset_konto or self._sys_account("account_inventarier"))
        acc_konto = int(accumulated_konto or self._accumulated_konto_for(asset_konto))
        exp_konto = int(expense_konto or self._sys_account("account_avskrivning_inventarier"))
        self.ensure_account(acc_konto, self._account_name(acc_konto)
                            or f"Ackumulerade avskrivningar ({acc_konto})")
        self.ensure_account(exp_konto, self._account_name(exp_konto) or "Avskrivningar")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO fixed_asset(transaktion_id, description, acquired_date, "
                "acquisition_ore, asset_konto, accumulated_konto, expense_konto, "
                "useful_life_years, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (transaktion_id, description, acquired_date, acquisition_ore, asset_konto,
                 acc_konto, exp_konto, life, note, _now()))
        return cur.lastrowid

    def _asset_row(self, row) -> dict:
        d = dict(row)
        acc = self.conn.execute(
            "SELECT COALESCE(SUM(amount_ore), 0) FROM depreciation WHERE fixed_asset_id=?",
            (d["id"],)).fetchone()[0]
        d["accumulated_ore"] = acc
        d["book_value_ore"] = d["acquisition_ore"] - acc
        d["annual_ore"] = round(d["acquisition_ore"] / d["useful_life_years"])
        d["years_booked"] = self.conn.execute(
            "SELECT COUNT(*) FROM depreciation WHERE fixed_asset_id=?", (d["id"],)).fetchone()[0]
        d["fully_depreciated"] = d["book_value_ore"] <= 0
        return d

    @staticmethod
    def _year_amount(asset: dict) -> int:
        """This year's straight-line instalment. The LAST year takes whatever is left, so
        the instalments sum to the anskaffningsvärde exactly — 10 000 kr over 3 years is
        3 333,33 three times, which would otherwise leave an öre stranded forever."""
        remaining = asset["book_value_ore"]
        if asset["years_booked"] + 1 >= asset["useful_life_years"]:
            return remaining
        return min(asset["annual_ore"], remaining)

    def list_fixed_assets(self, include_disposed: bool = True) -> list[dict]:
        """The asset register with accumulated depreciation and remaining book value."""
        where = "" if include_disposed else " WHERE disposed_date IS NULL"
        return [self._asset_row(r) for r in self.conn.execute(
            f"SELECT * FROM fixed_asset{where} ORDER BY acquired_date, id")]

    def update_fixed_asset(self, fixed_asset_id: int, **changes) -> None:
        """Correct the register (description, life, note) — reference data, no ledger
        effect. Already-booked depreciations are verifikationer and never move; a changed
        life only affects the years not yet written off."""
        row = self.conn.execute("SELECT id FROM fixed_asset WHERE id=?",
                                (fixed_asset_id,)).fetchone()
        if row is None:
            raise KeyError(f"No fixed asset {fixed_asset_id}")
        sets = {}
        for f in ("description", "useful_life_years", "note", "disposed_date"):
            if changes.get(f) is not None:
                sets[f] = changes[f]
        if "useful_life_years" in sets and int(sets["useful_life_years"]) < 1:
            raise ValueError("Avskrivningstiden måste vara minst 1 år")
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        with self.conn:
            self.conn.execute(f"UPDATE fixed_asset SET {cols} WHERE id=?",
                              (*sets.values(), fixed_asset_id))

    def delete_fixed_asset(self, fixed_asset_id: int) -> dict:
        """Remove a register entry that has never been depreciated (a mistyped one).
        Once a year is booked the entry is part of the audit trail and stays."""
        if self.conn.execute("SELECT 1 FROM depreciation WHERE fixed_asset_id=? LIMIT 1",
                             (fixed_asset_id,)).fetchone():
            raise InvalidState(
                "Tillgången har bokförda avskrivningar och kan inte tas bort — märk den "
                "som avyttrad istället")
        with self.conn:
            self.conn.execute("DELETE FROM fixed_asset WHERE id=?", (fixed_asset_id,))
        return {"deleted": True, "id": fixed_asset_id}

    def depreciation_proposal(self, fiscal_year_end: str) -> dict:
        """
        What should be written off for the year ending `fiscal_year_end`, WITHOUT booking
        anything. Skips assets acquired after the year end, disposed ones, already fully
        written-off ones, and any year already booked.
        """
        year_start = f"{int(fiscal_year_end[:4])}-01-01"
        items, total = [], 0
        for a in self.list_fixed_assets():
            already = self.conn.execute(
                "SELECT amount_ore FROM depreciation WHERE fixed_asset_id=? AND "
                "fiscal_year_end=?", (a["id"], fiscal_year_end)).fetchone()
            reason = None
            amount = 0
            if a["acquired_date"] > fiscal_year_end:
                reason = "Anskaffad efter räkenskapsårets slut"
            elif a["disposed_date"] and a["disposed_date"] < year_start:
                reason = "Avyttrad före räkenskapsåret"
            elif already is not None:
                reason = "Redan avskriven för det här året"
                amount = already["amount_ore"]
            elif a["fully_depreciated"]:
                reason = "Helt avskriven"
            else:
                amount = self._year_amount(a)
                total += amount
            items.append({**a, "proposed_ore": amount, "skipped": reason is not None,
                          "reason": reason,
                          "already_booked": already is not None})
        return {"fiscal_year_end": fiscal_year_end, "total_ore": total, "items": items}

    def book_depreciations(self, fiscal_year_end: str,
                           overrides: Optional[dict] = None) -> dict:
        """
        Book the year's depreciation as ONE verifikation dated the last day of the year:
        7832 debet for the total, each asset's ackumulerade-avskrivningskonto kredit.

        `overrides` maps fixed_asset_id -> amount in ören, for an asset you want written
        off by a different amount this year (0 skips it). An amount may never exceed the
        remaining book value.
        """
        proposal = self.depreciation_proposal(fiscal_year_end)
        overrides = {int(k): int(v) for k, v in (overrides or {}).items()}
        rows = []
        for it in proposal["items"]:
            if it["already_booked"]:
                continue
            amount = overrides.get(it["id"], it["proposed_ore"] if not it["skipped"] else 0)
            if amount <= 0:
                continue
            if amount > it["book_value_ore"]:
                raise ValueError(
                    f"{it['description']}: {self._kr(amount)} överstiger det bokförda "
                    f"värdet {self._kr(it['book_value_ore'])}")
            rows.append((it, amount))
        if not rows:
            raise InvalidState("Inget att skriva av för det här räkenskapsåret")

        total = sum(a for _, a in rows)
        by_acc: dict[int, int] = {}
        by_exp: dict[int, int] = {}
        for it, amount in rows:
            by_acc[it["accumulated_konto"]] = by_acc.get(it["accumulated_konto"], 0) + amount
            by_exp[it["expense_konto"]] = by_exp.get(it["expense_konto"], 0) + amount
        postings = [(k, v, "avskrivning") for k, v in sorted(by_exp.items())]
        postings += [(k, -v, "ackumulerad avskrivning") for k, v in sorted(by_acc.items())]

        with self.conn:
            vid, number = self._post_verifikation(
                fiscal_year_end, _now()[:10],
                f"Avskrivningar {fiscal_year_end[:4]}", postings,
                egenupprattad=True,
                motivering="Planenlig avskrivning enligt anläggningsregistret: "
                           + "; ".join(f"{it['description']} {self._kr(a)} "
                                       f"({it['useful_life_years']} år)" for it, a in rows))
            for it, amount in rows:
                self.conn.execute(
                    "INSERT INTO depreciation(fixed_asset_id, fiscal_year_end, amount_ore, "
                    "verifikation_id, created_at) VALUES (?,?,?,?,?)",
                    (it["id"], fiscal_year_end, amount, vid, _now()))
        return {"verifikation_id": vid, "ver_number": number, "total_ore": total,
                "count": len(rows)}

    def is_asset_purchase(self, transaktion_id: int) -> bool:
        """An inköp booked straight to a konto instead of a category (no category_id)."""
        row = self.conn.execute(
            "SELECT direction, category_id FROM transaktion WHERE id=?",
            (transaktion_id,)).fetchone()
        return bool(row and row["direction"] == "in" and row["category_id"] is None)

    def book_private_asset_contribution(self, description: str, amount_ore: int, date: str, *,
                                        mode: str = "auto",
                                        business_pct_centi: int = 10000,
                                        konto: Optional[int] = None,
                                        acquired_date: Optional[str] = None,
                                        acquired_amount_ore: Optional[int] = None,
                                        motivering: Optional[str] = None) -> dict:
        """
        Book a privately-owned asset being brought into the business on `date` (the day it
        starts being used in the verksamhet). Debits the asset/cost konto and credits 2018
        Egna insättningar — no moms, no money.

        The result is an EGENUPPRÄTTAD verifikation: `motivering` is the legal underlag, so
        it is required. When the caller does not supply one a complete default is composed
        from the facts given (description, dates, the private cost, the market value and
        the business share) — which is exactly what BFL 5 kap. asks the underlag to state.
        """
        description = (description or "").strip()
        if not description:
            raise ValueError("Beskriv tillgången (t.ex. modell och serienummer)")
        plan = self.private_asset_preview(amount_ore, mode=mode,
                                          business_pct_centi=business_pct_centi, konto=konto)
        booked = plan["booked_ore"]
        if booked <= 0:
            raise ValueError("Det bokförda beloppet blir noll")

        if not (motivering or "").strip():
            bits = [f"{description}."]
            if acquired_date:
                bits.append(f"Förvärvad privat {acquired_date}"
                            + (f" för {self._kr(acquired_amount_ore)}." if acquired_amount_ore
                               else "."))
            bits.append(f"Tas i bruk i verksamheten {date}.")
            bits.append("Marknadsvärde vid överföringen bedöms till "
                        f"{self._kr(plan['amount_ore'])}.")
            if plan["business_pct_centi"] < 10000:
                bits.append(f"Används till {plan['business_pct_centi'] / 100:g} % i "
                            f"verksamheten; bokfört värde {self._kr(booked)}.")
            else:
                bits.append("Används uteslutande i verksamheten.")
            motivering = " ".join(bits)

        # The private acquisition carried no avdragsrätt, so there is no moms leg — and
        # no moms_line either: this never belongs in the momsdeklaration.
        postings = [
            (plan["bas_konto"], booked, description),
            (plan["motkonto"], -booked, "egen insättning"),
        ]
        self.ensure_account(plan["bas_konto"],
                            plan["konto_namn"] or f"Konto {plan['bas_konto']}")
        self.ensure_account(plan["motkonto"],
                            self._account_name(plan["motkonto"]) or "Egna insättningar")
        with self.conn:
            vid, number = self._post_verifikation(
                date, _now()[:10],
                f"Överföring privat tillgång till verksamheten: {description}",
                postings, egenupprattad=True, motivering=motivering)
        return {"verifikation_id": vid, "ver_number": number, "motivering": motivering,
                **{k: plan[k] for k in ("treatment", "bas_konto", "konto_namn",
                                        "booked_ore", "threshold_ore", "notes")}}

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

    # ---- year-end tax estimate (enskild näringsidkare) --------------------------
    def tax_estimate(self, fy_start: str, fy_end: str) -> dict:
        """Estimate the year's tax owed to Skatteverket, broken down per tax."""
        from backend.reports import tax as tax_report
        return tax_report.tax_estimate(self.conn, fy_start, fy_end)

    def get_pay_methods(self) -> list:
        """The user's own list of betalningssätt for inköp (Qliro, Klarna, kort …), used to
        fill the betalsätt-väljaren. Stored as a JSON config list; a fresh book gets sensible
        defaults until the user edits them."""
        row = self.conn.execute(
            "SELECT value FROM config WHERE key='inkop_pay_methods'").fetchone()
        if row is not None:
            try:
                v = json.loads(row[0])
                if isinstance(v, list):
                    return [str(x) for x in v]
            except (ValueError, TypeError):
                pass
        return list(_DEFAULT_PAY_METHODS)

    def set_pay_methods(self, methods: list) -> list:
        """Replace the inköp betalsätt list (trimmed, de-duplicated, order preserved)."""
        clean: list[str] = []
        for m in methods or []:
            s = str(m).strip()[:60]
            if s and s not in clean:
                clean.append(s)
            if len(clean) >= 40:
                break
        with self.conn:
            self.conn.execute(
                "INSERT INTO config(key, value) VALUES ('inkop_pay_methods', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(clean),))
        return clean

    def get_tax_config(self) -> dict:
        """The editable tax rates used by the estimate (config, integer öre / centi-%)."""
        from backend.reports import tax as tax_report
        return {k: int(self._config(k)) for k in tax_report.CONFIG_KEYS}

    def set_tax_config(self, updates: dict) -> dict:
        """Update one or more tax-rate config values (whitelisted, non-negative ints)."""
        from backend.reports import tax as tax_report
        with self.conn:
            for key, val in updates.items():
                if key not in tax_report.CONFIG_KEYS:
                    raise ValueError(f"Unknown tax config key: {key}")
                iv = int(val)
                if iv < 0:
                    raise ValueError(f"{key} cannot be negative")
                self.conn.execute(
                    "INSERT INTO config(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(iv)))
        return self.get_tax_config()

    def get_company(self) -> dict:
        row = self.conn.execute(
            "SELECT id, name, org_nr, vat_nr, address, street, zip_code, city, email, phone, "
            "f_skatt, updated_at, (logo_enc IS NOT NULL) AS has_logo FROM company WHERE id=1"
        ).fetchone()
        if row is None:
            return {"id": 1, "name": None, "org_nr": None, "vat_nr": None,
                    "address": None, "street": None, "zip_code": None, "city": None,
                    "email": None, "phone": None, "f_skatt": 1, "has_logo": False}
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
        allowed = ("name", "org_nr", "vat_nr", "address", "street", "zip_code", "city",
                   "email", "phone", "f_skatt")
        data = {k: v for k, v in fields.items() if k in allowed}
        # Compose the legacy single-line address from the structured parts (used as a
        # fallback), unless an explicit address was given.
        if any(data.get(k) for k in ("street", "zip_code", "city")) and not fields.get("address"):
            data["address"] = _compose_address(data.get("street"), data.get("zip_code"),
                                                data.get("city"), "Sverige")
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

    def create_payment_method(self, label: str, value: str,
                              sort_order: Optional[int] = None) -> int:
        if not label or not value:
            raise ValueError("Payment method needs a label and a value")
        if sort_order is None:            # append at the end of the current order
            sort_order = self.conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM payment_method").fetchone()["n"]
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO payment_method(label, value, sort_order) VALUES (?,?,?)",
                (label, value, sort_order))
        return cur.lastrowid

    def reorder_payment_methods(self, ordered_ids: list) -> None:
        """Set the display order of the payment methods to the given id sequence. The order
        shows on the faktura for every UNPAID invoice (paid ones keep their frozen snapshot)."""
        with self.conn:
            for i, pid in enumerate(ordered_ids):
                self.conn.execute("UPDATE payment_method SET sort_order=? WHERE id=?",
                                  (i, int(pid)))

    def update_payment_method(self, payment_method_id: int, **fields) -> None:
        allowed = ("label", "value", "sort_order", "active")
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        sets = ", ".join(f"{k}=?" for k in data)
        with self.conn:
            self.conn.execute(f"UPDATE payment_method SET {sets} WHERE id=?",
                              (*data.values(), payment_method_id))

    def delete_payment_method(self, payment_method_id: int) -> None:
        """Remove a payment method. Safe: issued invoices carry their own frozen
        payment-method snapshot, so deleting one never changes an existing faktura."""
        with self.conn:
            self.conn.execute("DELETE FROM payment_method WHERE id=?", (payment_method_id,))

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
                       note: Optional[str] = None,
                       license_keys: Optional[list] = None,
                       contact_customer_id: Optional[int] = None,
                       delivery_address: Optional[dict] = None,
                       support_enabled: bool = True,
                       _reuse_number: Optional[int] = None) -> dict:
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
            # Optional stock batch: freeze this line's inköpskostnad (qty × unit cost) so
            # the real margin (revenue − cost) is known, and consume the batch on issue.
            stock_batch_id = ln.get("stock_batch_id")
            cost_ore = 0
            if stock_batch_id is not None:
                batch = self.conn.execute(
                    "SELECT article_id, qty_remaining_centi, unit_cost_ore FROM stock_batch "
                    "WHERE id=?", (int(stock_batch_id),)).fetchone()
                if batch is None:
                    raise KeyError(f"No stock batch {stock_batch_id}")
                if qty_centi > batch["qty_remaining_centi"]:
                    raise InvalidState(
                        f"Batchen har inte tillräckligt i lager "
                        f"(kvar {batch['qty_remaining_centi'] / 100:g}, begärt {qty_centi / 100:g})")
                cost_ore = round(qty_centi * batch["unit_cost_ore"] / 100)
            computed.append({
                "line_no": i, "description": ln["description"], "category_id": line_cat,
                "quantity_centi": qty_centi, "unit": ln.get("unit"),
                "unit_price_ore": unit_price_ore, "rate_code": rate_code,
                "reduction_type": reduction_type, "rut_eligible": 1 if reduction_type else 0,
                "article_id": ln.get("article_id"), "discount_pct_centi": discount_pct_centi,
                "stock_batch_id": int(stock_batch_id) if stock_batch_id is not None else None,
                "cost_ore": cost_ore, "ex_moms_ore": ex, "moms_ore": moms,
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
        # Optional contact person under a business buyer: freeze only the contact's name
        # into the buyer snapshot (the rest of the contact info stays the company's).
        contact_id = int(contact_customer_id) if contact_customer_id else None
        customer = self._apply_contact(customer, contact_id)
        buyer_snapshot_enc = self.session.encrypt_text(json.dumps(customer, default=str))
        clean_delivery = self._clean_delivery(delivery_address)
        delivery_enc = (self.session.encrypt_text(json.dumps(clean_delivery, default=str))
                        if clean_delivery else None)
        seller_snapshot = json.dumps(self.get_company(), default=str)
        pm_snapshot = json.dumps(self.list_payment_methods(active_only=True), default=str)
        # `_reuse_number` re-issues an edited faktura under its existing number (update_invoice);
        # otherwise take the next unbroken number.
        number = _reuse_number if _reuse_number is not None else self._next_invoice_number()

        # "Gratis distanssupport": 15 min per full 1000 kr of the invoice total (round
        # down), valid 36 months — but capped so a customer's balance never exceeds the
        # config maximum (12 h). If they are already at the cap this invoice earns nothing
        # and prints the "cap reached" notice instead of the earned-time block.
        # Per-invoice opt-out: when support is disabled nothing is earned and the note is
        # not printed (support_expiry NULL => the PDF skips the block).
        support_on = 1 if support_enabled else 0
        if support_on:
            support_cap = int(self._config("support_cap_minutes"))
            available_before = self.support_balance(customer_id)["remaining_minutes"]
            support_cap_reached = 1 if available_before >= support_cap else 0
            support_minutes = 0 if support_cap_reached else min(
                support_minutes_earned(inc_total), support_cap - available_before)
            support_expiry = _add_months(invoice_date, 36)
        else:
            support_cap_reached, support_minutes, support_expiry = 0, 0, None
        license_keys_enc = self.session.encrypt_text(
            json.dumps([str(k).strip() for k in (license_keys or []) if str(k).strip()]))

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO invoice(invoice_number, customer_id, transaktion_id, invoice_date, "
                "due_date, delivery_date, payment_terms, buyer_snapshot_enc, seller_snapshot, "
                "payment_methods_snapshot, our_reference, your_reference, note, ex_moms_ore, "
                "moms_ore, inc_moms_ore, rut_total_ore, rot_total_ore, "
                "support_minutes_earned, support_expiry_date, support_cap_reached, "
                "support_enabled, license_keys_enc, contact_customer_id, delivery_address_enc, "
                "created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (number, customer_id, tid, invoice_date, due_date, delivery_date, payment_terms,
                 buyer_snapshot_enc, seller_snapshot, pm_snapshot, our_reference, your_reference,
                 note, ex_total, moms_total, inc_total, rut_total, rot_total,
                 support_minutes, support_expiry, support_cap_reached, support_on, license_keys_enc,
                 contact_id, delivery_enc, _now()))
            invoice_id = cur.lastrowid
            for cl in computed:
                self.conn.execute(
                    "INSERT INTO invoice_line(invoice_id, line_no, description, category_id, "
                    "quantity_centi, unit, unit_price_ore, rate_code, rut_eligible, reduction_type, "
                    "article_id, discount_pct_centi, stock_batch_id, cost_ore, ex_moms_ore, moms_ore) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (invoice_id, cl["line_no"], cl["description"], cl["category_id"],
                     cl["quantity_centi"], cl["unit"], cl["unit_price_ore"], cl["rate_code"],
                     cl["rut_eligible"], cl["reduction_type"], cl["article_id"],
                     cl["discount_pct_centi"], cl["stock_batch_id"], cl["cost_ore"],
                     cl["ex_moms_ore"], cl["moms_ore"]))
                # Consume the picked stock batch (decrement on-hand quantity).
                if cl["stock_batch_id"] is not None:
                    self.conn.execute(
                        "UPDATE stock_batch SET qty_remaining_centi = qty_remaining_centi - ? "
                        "WHERE id=?", (cl["quantity_centi"], cl["stock_batch_id"]))
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
                "husavdrag_ore": husavdrag, "cap_warnings": cap_warnings,
                "support_minutes_earned": support_minutes, "support_expiry_date": support_expiry}

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

    # ---- inköp (purchase) drafts — save an unbooked inköp and continue later ----

    def save_expense_draft(self, payload: dict, draft_id: Optional[int] = None) -> dict:
        """Create or update an inköp draft. The whole form payload is stored encrypted;
        nothing is booked. Returns the draft id + timestamp."""
        enc = self.session.encrypt_text(json.dumps(payload, default=str))
        sid = payload.get("supplier_id")
        now = _now()
        with self.conn:
            if draft_id is not None:
                if self.conn.execute("SELECT 1 FROM expense_draft WHERE id=?",
                                     (draft_id,)).fetchone() is None:
                    raise KeyError(f"No expense draft {draft_id}")
                self.conn.execute(
                    "UPDATE expense_draft SET supplier_id=?, payload_enc=?, updated_at=? WHERE id=?",
                    (sid, enc, now, draft_id))
            else:
                cur = self.conn.execute(
                    "INSERT INTO expense_draft(supplier_id, payload_enc, created_at, updated_at) "
                    "VALUES (?,?,?,?)", (sid, enc, now, now))
                draft_id = cur.lastrowid
        return {"id": draft_id, "updated_at": now}

    def list_expense_drafts(self) -> list[dict]:
        """List inköp drafts with a best-effort summary (row count + estimated inc total)."""
        out = []
        for r in self.conn.execute(
                "SELECT id, supplier_id, payload_enc, updated_at FROM expense_draft "
                "ORDER BY updated_at DESC").fetchall():
            try:
                payload = json.loads(self.session.decrypt_text(r["payload_enc"]))
            except Exception:
                payload = {}
            items = payload.get("items") or []
            total = 0
            for it in items:
                try:
                    ex = round(int(it.get("quantity_centi", 0)) * int(it.get("unit_cost_ore", 0)) / 100)
                    rc = it.get("rate_code")
                    _, _, inc = (compute_moms_figures(ex, rc, inclusive=False)
                                 if rc in S.MOMS_RATES else (ex, 0, ex))
                    total += inc
                except Exception:
                    pass
            out.append({"id": r["id"], "supplier_id": r["supplier_id"],
                        "updated_at": r["updated_at"], "line_count": len(items),
                        "total_ore": total})
        return out

    def get_expense_draft(self, draft_id: int) -> dict:
        row = self.conn.execute(
            "SELECT id, payload_enc, updated_at FROM expense_draft WHERE id=?",
            (draft_id,)).fetchone()
        if row is None:
            raise KeyError(f"No expense draft {draft_id}")
        return {"id": row["id"], "updated_at": row["updated_at"],
                "payload": json.loads(self.session.decrypt_text(row["payload_enc"]))}

    def delete_expense_draft(self, draft_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM expense_draft WHERE id=?", (draft_id,))

    # ---- offerter (quotes: numbered proposal documents, book nothing) ----------
    def _next_offert_number(self) -> int:
        return self.conn.execute(
            "SELECT COALESCE(MAX(offert_number), 0) + 1 AS n FROM offert").fetchone()["n"]

    def _offert_figures(self, customer_id: int, customer: dict, lines: list[dict],
                        recipients: list[dict], category_id: Optional[int]) -> dict:
        """Compute an offert's display figures (no booking, no persistence): per-line
        ex/moms with rabatt, RUT/ROT pots + recipient split, and totals. Mirrors the
        create_invoice computation but is standalone so the booking path is untouched."""
        if not lines:
            raise ValueError("En offert behöver minst en artikelrad")
        computed = []
        for i, ln in enumerate(lines, start=1):
            rate_code = ln["rate_code"]
            if rate_code not in S.MOMS_RATES:
                raise ValueError(f"Unknown moms rate {rate_code!r}")
            qty_centi = int(ln["quantity_centi"])
            unit_price_ore = int(ln["unit_price_ore"])
            discount_pct_centi = int(ln.get("discount_pct_centi") or 0)
            if not 0 <= discount_pct_centi <= 10000:
                raise ValueError("Rabatt måste vara mellan 0 och 100 %")
            gross_ex = round(qty_centi * unit_price_ore / 100)
            ex = gross_ex - round(gross_ex * discount_pct_centi / 10000)
            _, moms, _ = compute_moms_figures(ex, rate_code, inclusive=False)
            reduction_type = ln.get("reduction_type")
            if reduction_type is None and ln.get("rut_eligible"):
                reduction_type = "rut"
            if reduction_type not in (None, "rut", "rot"):
                raise ValueError(f"Unknown reduction_type {reduction_type!r}")
            line_cat = ln.get("category_id") or category_id
            name = bas = None
            if line_cat:
                row = self.conn.execute("SELECT name, bas_konto FROM category WHERE id=?",
                                        (line_cat,)).fetchone()
                if row:
                    name, bas = row["name"], row["bas_konto"]
            computed.append({
                "line_no": i, "description": ln["description"], "category_id": line_cat,
                "category_name": name, "category_bas_konto": bas,
                "quantity_centi": qty_centi, "unit": ln.get("unit"),
                "unit_price_ore": unit_price_ore, "rate_code": rate_code,
                "reduction_type": reduction_type, "rut_eligible": 1 if reduction_type else 0,
                "discount_pct_centi": discount_pct_centi, "ex_moms_ore": ex, "moms_ore": moms,
            })
        rut_pct, rot_pct = self.reduction_pcts()
        rut_pot = sum(round((cl["ex_moms_ore"] + cl["moms_ore"]) * rut_pct / 100)
                      for cl in computed if cl["reduction_type"] == "rut")
        rot_pot = sum(round((cl["ex_moms_ore"] + cl["moms_ore"]) * rot_pct / 100)
                      for cl in computed if cl["reduction_type"] == "rot")
        has_reduction = bool(rut_pot or rot_pot)
        if has_reduction and customer["type"] != "private":
            raise ValueError("RUT/ROT gäller endast privatpersoner")
        rut_total = rot_total = 0
        out_recips = []
        if recipients:
            if customer["type"] != "private":
                raise ValueError("RUT/ROT-mottagare gäller endast privatpersoner")
            resolved = [self._resolve_recipient(r, customer_id, single=len(recipients) == 1)
                        for r in recipients]
            if rut_pot and sum(rc["rut_share_centi"] for rc in resolved) > 10000:
                raise ValueError("RUT-andelarna överstiger 100 %")
            if rot_pot and sum(rc["rot_share_centi"] for rc in resolved) > 10000:
                raise ValueError("ROT-andelarna överstiger 100 %")
            cum_rut = cum_rot = 0
            for rc in resolved:
                br, ar = cum_rut, cum_rut + rc["rut_share_centi"]; cum_rut = ar
                rut_amt = round(rut_pot * ar / 10000) - round(rut_pot * br / 10000)
                bo, ao = cum_rot, cum_rot + rc["rot_share_centi"]; cum_rot = ao
                rot_amt = round(rot_pot * ao / 10000) - round(rot_pot * bo / 10000)
                rut_total += rut_amt; rot_total += rot_amt
                out_recips.append({
                    "customer_id": rc["customer_id"], "first_name": rc["first_name"],
                    "last_name": rc["last_name"], "personnummer": rc["personnummer"],
                    "share_pct": rc["rut_share_centi"] / 100,
                    "rut_share_pct": rc["rut_share_centi"] / 100,
                    "rot_share_pct": rc["rot_share_centi"] / 100,
                    "rut_amount_ore": rut_amt, "rot_amount_ore": rot_amt,
                })
        elif has_reduction:
            raise ValueError("RUT/ROT-rader kräver minst en mottagare")
        ex_total = sum(cl["ex_moms_ore"] for cl in computed)
        moms_total = sum(cl["moms_ore"] for cl in computed)
        return {"lines": computed, "recipients": out_recips,
                "ex_moms_ore": ex_total, "moms_ore": moms_total,
                "inc_moms_ore": ex_total + moms_total,
                "rut_total_ore": rut_total, "rot_total_ore": rot_total}

    def create_offert(self, payload: dict, source_draft_id: Optional[int] = None) -> dict:
        """Create a numbered offert (quote) from a form payload. Books nothing; assigns
        the next offert_number and stores the encrypted render snapshot."""
        customer_id = payload.get("customer_id")
        if not customer_id:
            raise ValueError("Offert kräver en kund")
        customer = self.get_customer(int(customer_id))
        contact_id = payload.get("contact_customer_id")
        buyer = self._apply_contact(customer, contact_id)
        fig = self._offert_figures(int(customer_id), customer, payload.get("lines") or [],
                                   payload.get("recipients") or [], payload.get("category_id"))
        offert_date = payload.get("invoice_date") or _now()[:10]
        valid_until = (payload.get("valid_until") or payload.get("due_date")
                       or (datetime.fromisoformat(offert_date) + timedelta(days=30)).date().isoformat())
        number = self._next_offert_number()
        # The offert advertises the gratis-distanssupport the resulting faktura would give,
        # unless the toggle is off (then no note on the offert either).
        support_on = payload.get("support_enabled", True)
        support_minutes = support_minutes_earned(fig["inc_moms_ore"]) if support_on else 0
        support_expiry = _add_months(offert_date, 36) if support_on else None
        render = {
            "doc_type": "offert", "invoice_number": number, "invoice_date": offert_date,
            "valid_until": valid_until, "buyer": buyer, "seller": self.get_company(),
            "contact_customer_id": int(contact_id) if contact_id else None,
            "delivery_address": self._clean_delivery(payload.get("delivery_address")),
            "payment_methods": [], "payment_terms": payload.get("payment_terms"),
            "your_reference": payload.get("your_reference"),
            "our_reference": payload.get("our_reference"), "note": payload.get("note"),
            "support_enabled": bool(support_on),
            "support_minutes_earned": support_minutes,
            "support_expiry_date": support_expiry,
            **fig,
        }
        snapshot_enc = self.session.encrypt_text(json.dumps(render, default=str))
        husavdrag = fig["rut_total_ore"] + fig["rot_total_ore"]
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO offert(offert_number, customer_id, offert_date, valid_until, "
                "inc_moms_ore, husavdrag_ore, snapshot_enc, source_draft_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (number, int(customer_id), offert_date, valid_until, fig["inc_moms_ore"],
                 husavdrag, snapshot_enc, source_draft_id, _now()))
        return {"offert_id": cur.lastrowid, "offert_number": number,
                "inc_moms_ore": fig["inc_moms_ore"], "husavdrag_ore": husavdrag}

    def create_offert_from_draft(self, draft_id: int) -> dict:
        """Create an offert from a saved draft. The draft is KEPT (not consumed)."""
        draft = self.get_draft(draft_id)
        return self.create_offert(draft["payload"], source_draft_id=draft_id)

    def preview_invoice_render(self, payload: dict) -> dict:
        """Build a FAKTURA render dict from a form payload WITHOUT persisting or booking —
        for a pre-issue PDF preview. No invoice_number is assigned; support minutes/cap and
        licence keys are shown as they would be if issued now. Reuses _offert_figures so the
        booking path is untouched."""
        customer_id = payload.get("customer_id")
        if not customer_id:
            raise ValueError("Välj en kund först")
        customer = self.get_customer(int(customer_id))
        buyer = self._apply_contact(customer, payload.get("contact_customer_id"))
        fig = self._offert_figures(int(customer_id), customer, payload.get("lines") or [],
                                   payload.get("recipients") or [], payload.get("category_id"))
        invoice_date = payload.get("invoice_date") or _now()[:10]
        inc_total = fig["inc_moms_ore"]
        support_on = payload.get("support_enabled", True)
        if support_on:
            support_cap = int(self._config("support_cap_minutes"))
            available_before = self.support_balance(int(customer_id))["remaining_minutes"]
            support_cap_reached = available_before >= support_cap
            support_minutes = 0 if support_cap_reached else min(
                support_minutes_earned(inc_total), support_cap - available_before)
            support_expiry = _add_months(invoice_date, 36)
        else:
            support_cap_reached, support_minutes, support_expiry = False, 0, None
        return {
            "doc_type": "faktura_preview", "invoice_number": None,
            "invoice_date": invoice_date, "due_date": payload.get("due_date"),
            "delivery_date": payload.get("delivery_date"),
            "buyer": buyer, "seller": self.get_company(),
            "delivery_address": self._clean_delivery(payload.get("delivery_address")),
            "payment_methods": self.list_payment_methods(active_only=True),
            "payment_terms": payload.get("payment_terms"),
            "your_reference": payload.get("your_reference"),
            "our_reference": payload.get("our_reference"), "note": payload.get("note"),
            "support_enabled": bool(support_on),
            "support_minutes_earned": support_minutes,
            "support_expiry_date": support_expiry,
            "support_cap_reached": 1 if support_cap_reached else 0,
            "license_keys": [str(k).strip() for k in (payload.get("license_keys") or []) if str(k).strip()],
            **fig,
        }

    def list_offerter(self) -> list[dict]:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT o.id, o.offert_number, o.customer_id, o.offert_date, o.valid_until, "
            "o.inc_moms_ore, o.husavdrag_ore, o.source_draft_id, o.invoice_id, o.version, "
            "o.root_offert_id, i.invoice_number AS invoice_number, o.created_at FROM offert o "
            "LEFT JOIN invoice i ON i.id = o.invoice_id "
            "ORDER BY o.offert_number").fetchall()]
        for r in rows:
            r["display_number"] = self._offert_display_number(
                r["offert_number"], r["version"], r["root_offert_id"])
        return rows

    def get_offert(self, offert_id: int) -> dict:
        """Return the offert's decrypted render dict (for the PDF)."""
        row = self.conn.execute("SELECT snapshot_enc FROM offert WHERE id=?",
                                (offert_id,)).fetchone()
        if row is None:
            raise KeyError(f"No offert {offert_id}")
        return json.loads(self.session.decrypt_text(row["snapshot_enc"]))

    def _offert_display_number(self, offert_number: int, version: int, root_id) -> str:
        """The document number shown on an offert: 'N' for an original, 'N-v' for a
        revised version (N = the original's number)."""
        if not version:
            return str(offert_number)
        root_num = self.conn.execute("SELECT offert_number FROM offert WHERE id=?",
                                     (root_id,)).fetchone()
        base = root_num["offert_number"] if root_num else offert_number
        return f"{base}-{version}"

    def create_offert_version(self, offert_id: int) -> dict:
        """Create a NEW revised version of an existing offert, KEEPING the original (and
        any earlier versions). The new document is numbered '<original>-<n>' (e.g. 5-1,
        5-2). Books nothing — offerter never touch the ledger. Returns the new offert."""
        base = self.conn.execute(
            "SELECT id, offert_number, customer_id, offert_date, valid_until, inc_moms_ore, "
            "husavdrag_ore, snapshot_enc, version, root_offert_id FROM offert WHERE id=?",
            (offert_id,)).fetchone()
        if base is None:
            raise KeyError(f"No offert {offert_id}")
        root_id = base["root_offert_id"] or base["id"]
        # Next version number within this family (original + all its versions).
        next_version = (self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM offert WHERE id=? OR root_offert_id=?",
            (root_id, root_id)).fetchone()[0]) + 1
        number = self._next_offert_number()          # consumes a unique internal number
        # The display number is <original>-<next_version> (e.g. 5-1).
        root_num = self.conn.execute("SELECT offert_number FROM offert WHERE id=?",
                                     (root_id,)).fetchone()["offert_number"]
        display = f"{root_num}-{next_version}"
        render = json.loads(self.session.decrypt_text(base["snapshot_enc"]))
        render["invoice_number"] = display           # the PDF shows this as Offertnr
        snapshot_enc = self.session.encrypt_text(json.dumps(render, default=str))
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO offert(offert_number, customer_id, offert_date, valid_until, "
                "inc_moms_ore, husavdrag_ore, snapshot_enc, source_draft_id, version, "
                "root_offert_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (number, base["customer_id"], base["offert_date"], base["valid_until"],
                 base["inc_moms_ore"], base["husavdrag_ore"], snapshot_enc, None,
                 next_version, root_id, _now()))
        return {"offert_id": cur.lastrowid, "offert_number": display, "version": next_version,
                "inc_moms_ore": base["inc_moms_ore"], "husavdrag_ore": base["husavdrag_ore"]}

    def create_invoice_from_offert(self, offert_id: int, invoice_date: Optional[str] = None,
                                   due_date: Optional[str] = None) -> dict:
        """Issue a real faktura from an accepted offert. Reconstructs the invoice inputs
        from the offert's snapshot and calls create_invoice (so booking + numbering are
        unchanged). An offert can be invoiced only once (guarded)."""
        row = self.conn.execute("SELECT customer_id, invoice_id FROM offert WHERE id=?",
                                (offert_id,)).fetchone()
        if row is None:
            raise KeyError(f"No offert {offert_id}")
        if row["invoice_id"] is not None:
            raise InvalidState("Offerten är redan fakturerad")
        render = self.get_offert(offert_id)
        lines = [{
            "description": l["description"], "category_id": l.get("category_id"),
            "quantity_centi": l["quantity_centi"], "unit": l.get("unit"),
            "unit_price_ore": l["unit_price_ore"], "rate_code": l["rate_code"],
            "reduction_type": l.get("reduction_type"),
            "discount_pct_centi": l.get("discount_pct_centi") or 0,
        } for l in render.get("lines", [])]
        recipients = [{
            "customer_id": r.get("customer_id"), "personnummer": r.get("personnummer"),
            "first_name": r.get("first_name"), "last_name": r.get("last_name"),
            "rut_share_pct": r.get("rut_share_pct"), "rot_share_pct": r.get("rot_share_pct"),
        } for r in render.get("recipients", [])]
        idate = invoice_date or _now()[:10]
        ddate = due_date or (datetime.fromisoformat(idate) + timedelta(days=30)).date().isoformat()
        res = self.create_invoice(
            customer_id=row["customer_id"], category_id=None, invoice_date=idate,
            due_date=ddate, lines=lines, recipients=recipients,
            payment_terms=render.get("payment_terms"),
            your_reference=render.get("your_reference"),
            our_reference=render.get("our_reference"), note=render.get("note"),
            contact_customer_id=render.get("contact_customer_id"),
            delivery_address=render.get("delivery_address"),
            support_enabled=render.get("support_enabled", True))
        with self.conn:
            self.conn.execute("UPDATE offert SET invoice_id=? WHERE id=?",
                             (res["invoice_id"], offert_id))
        res["offert_id"] = offert_id
        return res

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
        # RUT/ROT: after the customer has paid, Skatteverket still owes the husavdrag part
        # until the claim reaches 'skatteverket_paid' — the invoice is then awaiting RUT/ROT.
        rut_claim_state = None
        if husavdrag > 0 and inv["transaktion_id"]:
            cr = self.conn.execute("SELECT state FROM rut_claim WHERE transaktion_id=?",
                                   (inv["transaktion_id"],)).fetchone()
            rut_claim_state = cr["state"] if cr else None
        return {
            "inc_moms_ore": inv["inc_moms_ore"], "rut_total_ore": inv["rut_total_ore"],
            "rot_total_ore": inv["rot_total_ore"], "husavdrag_ore": husavdrag,
            "customer_total_ore": customer_total, "credited_ore": credits,
            "customer_due_ore": customer_due, "paid_ore": payments - refunds,
            "refunded_ore": refunds,
            "outstanding_ore": customer_due - paid,
            "state": self._invoice_state(inv, customer_total, credits, paid,
                                         husavdrag, rut_claim_state),
        }

    @staticmethod
    def _invoice_state(inv_row, customer_total, credits, paid,
                       husavdrag=0, rut_claim_state=None) -> str:
        if inv_row["cancelled_at"]:
            return "cancelled"
        if inv_row["credited_at"] or (customer_total > 0 and credits >= customer_total):
            return "credited"
        if paid <= 0:
            return "pending"
        if paid >= customer_total - credits:
            # Customer part settled. A RUT/ROT invoice is not fully settled until
            # Skatteverket has paid the husavdrag part (rut_claim -> 'skatteverket_paid').
            if husavdrag > 0 and rut_claim_state and rut_claim_state != "skatteverket_paid":
                return "awaiting_rut"
            return "paid"
        return "partial"

    def list_invoices(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, invoice_number, invoice_date, due_date, customer_id, transaktion_id, "
            "ex_moms_ore, inc_moms_ore, rut_total_ore, rot_total_ore, parent_invoice_id, "
            "husavdrag_shortfall_ore, relation_note FROM invoice ORDER BY invoice_number"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.update(self._invoice_balances(r["id"]))
            # Real margin from picked stock batches (revenue ex moms − frozen cost). Keyed
            # on the frozen cost so it survives the batch being pruned after payment.
            crow = self.conn.execute(
                "SELECT COALESCE(SUM(cost_ore), 0) AS cost_ore, "
                "COUNT(NULLIF(cost_ore, 0)) AS with_cost FROM invoice_line WHERE invoice_id=?",
                (r["id"],)).fetchone()
            d["cost_ore"] = crow["cost_ore"]
            d["has_cost"] = bool(crow["with_cost"])
            d["margin_ore"] = r["ex_moms_ore"] - crow["cost_ore"]
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
        # An UNPAID invoice (customer still owes) shows the CURRENT active payment methods in
        # their current order — so newly added / reordered betalsätt appear when the PDF is
        # regenerated. A settled invoice keeps its frozen snapshot (historical accuracy).
        if inv.get("state") in ("pending", "partial"):
            inv["payment_methods"] = self.list_payment_methods(active_only=True)
        lk_enc = inv.pop("license_keys_enc", None)
        inv["license_keys"] = json.loads(self.session.decrypt_text(lk_enc)) if lk_enc else []
        da_enc = inv.pop("delivery_address_enc", None)
        inv["delivery_address"] = json.loads(self.session.decrypt_text(da_enc)) if da_enc else None
        inv["lines"] = [dict(r) for r in self.conn.execute(
            "SELECT il.line_no, il.description, il.category_id, c.name AS category_name, "
            "c.bas_konto AS category_bas_konto, il.quantity_centi, il.unit, il.unit_price_ore, "
            "il.rate_code, il.rut_eligible, il.reduction_type, il.discount_pct_centi, "
            "il.article_id, il.stock_batch_id, sb.batch_number, il.cost_ore, il.ex_moms_ore, il.moms_ore "
            "FROM invoice_line il LEFT JOIN category c ON c.id = il.category_id "
            "LEFT JOIN stock_batch sb ON sb.id = il.stock_batch_id "
            "WHERE il.invoice_id=? ORDER BY il.line_no", (invoice_id,)).fetchall()]
        # Real margin from any picked stock batches (revenue ex moms − frozen cost).
        # `has_cost` says whether any line carried a cost (else margin is unknown).
        inv["cost_ore"] = sum(ln["cost_ore"] for ln in inv["lines"])
        # Margin is known once any line carries a frozen cost — this survives the batch
        # being pruned after payment (the link is nulled but cost_ore stays frozen).
        inv["has_cost"] = any(ln["cost_ore"] for ln in inv["lines"])
        inv["margin_ore"] = inv["ex_moms_ore"] - inv["cost_ore"]
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
            # Return any consumed stock to its batch (the goods were never sold).
            for ln in self.conn.execute(
                    "SELECT stock_batch_id, quantity_centi FROM invoice_line "
                    "WHERE invoice_id=? AND stock_batch_id IS NOT NULL", (invoice_id,)).fetchall():
                self.conn.execute(
                    "UPDATE stock_batch SET qty_remaining_centi = qty_remaining_centi + ? "
                    "WHERE id=?", (ln["quantity_centi"], ln["stock_batch_id"]))
            self.conn.execute(
                "UPDATE invoice_line SET stock_batch_id=NULL, cost_ore=0 WHERE invoice_id=?",
                (invoice_id,))
            # Detach the invoice first so deleting the transaktion can't trip the FK.
            self.conn.execute(
                "UPDATE invoice SET cancelled_at=?, transaktion_id=NULL WHERE id=?",
                (_now(), invoice_id))
            if tid:
                self.conn.execute("DELETE FROM moms_line WHERE transaktion_id=?", (tid,))
                self.conn.execute("DELETE FROM rut_claim WHERE transaktion_id=?", (tid,))
                self.conn.execute("DELETE FROM transaktion WHERE id=?", (tid,))
        return {"invoice_id": invoice_id, "cancelled": True}

    def update_invoice(self, invoice_id: int, *, customer_id: int,
                       category_id: Optional[int] = None, invoice_date: str, due_date: str,
                       lines: list[dict], recipients: Optional[list[dict]] = None,
                       delivery_date: Optional[str] = None, payment_terms: Optional[str] = None,
                       our_reference: Optional[str] = None, your_reference: Optional[str] = None,
                       note: Optional[str] = None, license_keys: Optional[list] = None,
                       contact_customer_id: Optional[int] = None,
                       delivery_address: Optional[dict] = None,
                       support_enabled: bool = True) -> dict:
        """Adjust an UNPAID, UNBOOKED faktura in place, keeping its fakturanummer. Allowed
        only while nothing has hit the ledger (kontantmetod: the underlying income is still
        pending, no payments/credits) — a booked or paid faktura must be corrected with a
        kreditfaktura instead. The old lines/recipients/stock consumption + the pending
        transaktion are reversed and the faktura is rebuilt from the new payload under the
        SAME number. The whole new payload is validated BEFORE anything is removed, so a
        bad edit can never destroy the existing faktura."""
        inv = self.conn.execute("SELECT * FROM invoice WHERE id=?", (invoice_id,)).fetchone()
        if inv is None:
            raise KeyError(f"No invoice {invoice_id}")
        if inv["cancelled_at"] or inv["credited_at"]:
            raise InvalidState("Fakturan är makulerad/krediterad och kan inte ändras")
        if inv["parent_invoice_id"] or inv["husavdrag_shortfall_ore"]:
            raise InvalidState("En följdfaktura kan inte ändras")
        tid = inv["transaktion_id"]
        t = self.conn.execute("SELECT verifikation_id FROM transaktion WHERE id=?",
                              (tid,)).fetchone() if tid else None
        if t is None or t["verifikation_id"] is not None:
            raise InvalidState(
                "Bara obetalda, obokförda fakturor kan ändras — kreditera fakturan i stället")
        if self.conn.execute("SELECT 1 FROM invoice_event WHERE invoice_id=? LIMIT 1",
                             (invoice_id,)).fetchone():
            raise InvalidState(
                "Fakturan har betalningar/krediteringar — kreditera/återbetala i stället")

        # --- Pre-validate the NEW payload up front (no side effects), so the reversal +
        #     recreate below can only run on input that is guaranteed to succeed. ---
        if not lines:
            raise ValueError("En faktura behöver minst en artikelrad")
        customer = self.get_customer(customer_id)          # raises if the customer is gone
        if category_id is not None:
            self._check_category(category_id, "income")
        # Batch availability must account for restocking THIS invoice's current consumption.
        restock: dict[int, int] = {}
        for r in self.conn.execute(
                "SELECT stock_batch_id, quantity_centi FROM invoice_line "
                "WHERE invoice_id=? AND stock_batch_id IS NOT NULL", (invoice_id,)).fetchall():
            restock[r["stock_batch_id"]] = restock.get(r["stock_batch_id"], 0) + r["quantity_centi"]
        has_reduction_line = False
        for ln in lines:
            if ln["rate_code"] not in S.MOMS_RATES:
                raise ValueError(f"Unknown moms rate {ln['rate_code']!r}")
            line_cat = ln.get("category_id") or category_id
            if line_cat is None:
                raise ValueError("Varje rad behöver en kategori (eller en standardkategori)")
            self._check_category(line_cat, "income")
            disc = int(ln.get("discount_pct_centi") or 0)
            if not 0 <= disc <= 10000:
                raise ValueError("Rabatt måste vara mellan 0 och 100 %")
            rt = ln.get("reduction_type") or ("rut" if ln.get("rut_eligible") else None)
            if rt not in (None, "rut", "rot"):
                raise ValueError(f"Unknown reduction_type {rt!r}")
            has_reduction_line = has_reduction_line or bool(rt)
            sb = ln.get("stock_batch_id")
            if sb is not None:
                batch = self.conn.execute("SELECT qty_remaining_centi FROM stock_batch WHERE id=?",
                                          (int(sb),)).fetchone()
                if batch is None:
                    raise KeyError(f"No stock batch {sb}")
                if int(ln["quantity_centi"]) > batch["qty_remaining_centi"] + restock.get(int(sb), 0):
                    raise InvalidState("Batchen har inte tillräckligt i lager")
        if has_reduction_line:
            if customer["type"] != "private":
                raise ValueError("RUT/ROT gäller endast privatpersoner")
            if not (recipients or []):
                raise ValueError("RUT/ROT-rader kräver minst en mottagare")
            if not customer["personnummer"]:
                raise ValueError("RUT/ROT kräver kundens personnummer")

        number = inv["invoice_number"]
        src_offert = self.conn.execute("SELECT id FROM offert WHERE invoice_id=?",
                                       (invoice_id,)).fetchone()
        with self.conn:
            # Return this invoice's consumed stock to its batches (the goods are un-sold).
            for r in self.conn.execute(
                    "SELECT stock_batch_id, quantity_centi FROM invoice_line "
                    "WHERE invoice_id=? AND stock_batch_id IS NOT NULL", (invoice_id,)).fetchall():
                self.conn.execute(
                    "UPDATE stock_batch SET qty_remaining_centi = qty_remaining_centi + ? "
                    "WHERE id=?", (r["quantity_centi"], r["stock_batch_id"]))
            self.conn.execute("DELETE FROM invoice_line WHERE invoice_id=?", (invoice_id,))
            self.conn.execute("DELETE FROM rut_recipient WHERE invoice_id=?", (invoice_id,))
            if src_offert:                       # break the FK before the row is deleted
                self.conn.execute("UPDATE offert SET invoice_id=NULL WHERE invoice_id=?",
                                  (invoice_id,))
            self.conn.execute("UPDATE invoice SET transaktion_id=NULL WHERE id=?", (invoice_id,))
            if tid:
                self.conn.execute("DELETE FROM moms_line WHERE transaktion_id=?", (tid,))
                self.conn.execute("DELETE FROM rut_claim WHERE transaktion_id=?", (tid,))
                self.conn.execute("DELETE FROM transaktion WHERE id=?", (tid,))
            self.conn.execute("DELETE FROM invoice WHERE id=?", (invoice_id,))
        res = self.create_invoice(
            customer_id=customer_id, category_id=category_id, invoice_date=invoice_date,
            due_date=due_date, lines=lines, recipients=recipients, delivery_date=delivery_date,
            payment_terms=payment_terms, our_reference=our_reference, your_reference=your_reference,
            note=note, license_keys=license_keys, contact_customer_id=contact_customer_id,
            delivery_address=delivery_address, support_enabled=support_enabled,
            _reuse_number=number)
        if src_offert:                           # re-point the offert at the rebuilt faktura
            with self.conn:
                self.conn.execute("UPDATE offert SET invoice_id=? WHERE id=?",
                                  (res["invoice_id"], src_offert["id"]))
        return res

    # ---- settlement subledger: partial payments / refunds / credits ----------

    def pay_invoice(self, invoice_id: int, amount_ore: Optional[int] = None,
                    date: Optional[str] = None, note: Optional[str] = None) -> dict:
        """
        Register a customer payment against an invoice (partial or full). Books the
        cash and records an invoice_event; the invoice's outstanding balance + state
        follow from the events. `amount_ore` defaults to the full outstanding amount.
        RUT invoices use the full register_payment + Skatteverket flow instead.

        `note` is a free reference/comment appended to the verifikation text.
        """
        inv, bal = self._require_open_invoice(invoice_id)
        if inv["husavdrag_shortfall_ore"]:
            return self._pay_husavdrag_shortfall(inv, bal, amount_ore, date, note=note)
        if inv["rut_total_ore"] or inv["rot_total_ore"]:
            raise InvalidState("RUT/ROT invoices: use the full payment + Skatteverket flow")
        amount = bal["outstanding_ore"] if amount_ore is None else int(amount_ore)
        if amount <= 0:
            raise ValueError("Payment amount must be > 0")
        if amount > bal["outstanding_ore"]:
            raise InvalidState("Payment exceeds the outstanding amount")
        date = date or _now()[:10]
        tid = inv["transaktion_id"]
        text = f"Betalning faktura {inv['invoice_number']}" + (
            f" – {note.strip()}" if note and note.strip() else "")
        # Öresavrundning: when this payment settles the invoice in full, the customer pays
        # a whole-krona summa att betala. The öre difference (never the underlag/moms) is
        # shaved off the bank into 3740; a partly-paid invoice books exact until it closes.
        closes = amount == bal["outstanding_ore"]
        ores = inv["inc_moms_ore"] - _round_to_krona(inv["inc_moms_ore"]) if closes else 0

        if self.get_accounting_method() == "fakturametod":
            postings = [(self._sys_account("account_bank"), amount - ores, "inbetalning"),
                        (self._sys_account("account_kundfordran"), -amount, "kvitta kundfordran")]
            if ores:
                postings.append((self._sys_account("account_ores_kronutjamning"), ores, "öresavrundning"))
            with self.conn:
                vid, num = self._post_verifikation(date, date, text, postings)
                self._record_invoice_event(invoice_id, "payment", amount, date, vid)
                self._sync_invoice_paid(invoice_id, tid, date)
        else:  # kontantmetod: recognise income + moms proportionally as cash arrives
            vid, num = self._book_kontant_recognition(tid, bal["paid_ore"], amount,
                                                      inv["inc_moms_ore"], date, +1, text, ores_ore=ores)
            with self.conn:
                self._record_invoice_event(invoice_id, "payment", amount, date, vid)
                self._sync_invoice_paid(invoice_id, tid, date)
        return {"invoice_id": invoice_id, "verifikation_id": vid, "ver_number": num,
                "amount_ore": amount, "outstanding_ore": self._invoice_balances(invoice_id)["outstanding_ore"]}

    def _pay_husavdrag_shortfall(self, inv, bal, amount_ore, date, note=None) -> dict:
        """Settle a husavdrag follow-up invoice: pure receivable collection (bank ←
        1510), no income/moms recognition (already booked at the original sale)."""
        amount = bal["outstanding_ore"] if amount_ore is None else int(amount_ore)
        if amount <= 0:
            raise ValueError("Payment amount must be > 0")
        if amount > bal["outstanding_ore"]:
            raise InvalidState("Payment exceeds the outstanding amount")
        date = date or _now()[:10]
        text = f"Betalning faktura {inv['invoice_number']} (kvarstående husavdrag)" + (
            f" – {note.strip()}" if note and note.strip() else "")
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
        """Mark the underlying transaktion paid once the invoice is fully settled. When the
        invoice is fully paid, prune its now-empty stock batches (the reserved stock is
        permanently gone)."""
        bal = self._invoice_balances(invoice_id)
        if bal["outstanding_ore"] <= 0:
            if transaktion_id:
                self.conn.execute("UPDATE transaktion SET status='paid', payment_date=? WHERE id=?",
                                  (date, transaktion_id))
            self._prune_invoice_empty_batches(invoice_id)
        elif transaktion_id:
            self.conn.execute("UPDATE transaktion SET status='pending' WHERE id=?", (transaktion_id,))

    def _recognition_slice(self, transaktion_id, recognized_before, amount, total_inc) -> list:
        """Per-line (category_id, rate, ex, moms) to recognise for `amount` of cash,
        using cumulative rounding so a sequence of partials reconciles exactly to the
        öre. One entry per moms_line (an invoice carries one line per category×rate)."""
        before, after = recognized_before, recognized_before + amount
        # Freeze the konton on the first recognition so every later partial (and any
        # credit) lands on the same konto even if the category is edited in between.
        self._freeze_line_konton(transaktion_id)
        out = []
        for ln in self.conn.execute(
                "SELECT category_id, rate_code, ex_moms_ore, moms_ore, bas_konto FROM moms_line "
                "WHERE transaktion_id=?", (transaktion_id,)):
            ex_s = (round(ln["ex_moms_ore"] * after / total_inc)
                    - round(ln["ex_moms_ore"] * before / total_inc))
            moms_s = (round(ln["moms_ore"] * after / total_inc)
                      - round(ln["moms_ore"] * before / total_inc))
            out.append({"category_id": ln["category_id"], "rate_code": ln["rate_code"],
                        "bas_konto": ln["bas_konto"], "ex_s": ex_s, "moms_s": moms_s})
        return out

    def _income_splits(self, transaktion_id) -> list:
        """[(bas_konto, sum_ex), …] — the full ex-moms of a transaktion grouped by each
        moms_line's category konto (fallback the transaktion category). Used when
        booking the whole amount at once (cash sale/purchase, fakturametod issue).
        Freezes the konto on the lines, so this booking is what the reports keep showing."""
        self._freeze_line_konton(transaktion_id)
        agg: dict[int, int] = {}
        for ln in self.conn.execute(
                "SELECT bas_konto, ex_moms_ore FROM moms_line WHERE transaktion_id=?",
                (transaktion_id,)):
            konto = ln["bas_konto"]
            agg[konto] = agg.get(konto, 0) + ln["ex_moms_ore"]
        return sorted(agg.items())

    def _group_income(self, transaktion_id, slices) -> dict:
        """{bas_konto: sum_ex} — slice ex grouped by each line's frozen konto (resolved in
        `_recognition_slice`, falling back to the transaktion's category)."""
        fb = self.conn.execute("SELECT category_id FROM transaktion WHERE id=?",
                               (transaktion_id,)).fetchone()["category_id"]
        agg: dict[int, int] = {}
        for s in slices:
            konto = s.get("bas_konto")
            if konto is None:
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
                    "moms_ore, inc_moms_ore, bas_konto) VALUES (?,?,?,?,?,?,?)",
                    (rid, s["rate_code"], s["category_id"], sign * s["ex_s"],
                     sign * s["moms_s"], sign * (s["ex_s"] + s["moms_s"]), s.get("bas_konto")))

    def _book_kontant_recognition(self, transaktion_id, recognized_before, amount, total_inc,
                                  date, sign, text, ores_ore=0) -> tuple[int, int]:
        """Kontantmetod: book bank +/- amount against income + moms recognised for the
        proportional slice (income split per line category), plus the report-clone.
        `ores_ore` (öresavrundning) shaves that many öre off the bank into 3740 so the
        customer pays whole kronor while income + moms stay exact. Returns (vid, number)."""
        slices = self._recognition_slice(transaktion_id, recognized_before, amount, total_inc)
        postings = [(self._sys_account("account_bank"), sign * (amount - ores_ore), "inbetalning")]
        for konto, ex in sorted(self._group_income(transaktion_id, slices).items()):
            postings.append((konto, -sign * ex, "försäljning"))
        for rate_code, moms in sorted(self._group_moms(slices).items()):
            postings.append((self._sys_account(_UTG_MOMS_KEY[rate_code]), -sign * moms,
                             f"utgående moms {rate_code}%"))
        if ores_ore:
            postings.append((self._sys_account("account_ores_kronutjamning"),
                             sign * ores_ore, "öresavrundning"))
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
        # The receivable is booked EXACT at issue (fakturametod moms lands in the
        # invoice's period). Öresavrundning happens at payment: the bank gets the
        # whole-krona amount and the öre difference goes to 3740 (see register_payment).
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
                       original_format: Optional[str] = None,
                       rut_claim_id: Optional[int] = None) -> dict:
        """
        Store a receipt photo for a transaktion: the bytes are encrypted with the
        book DEK and written as a file in `<db>.photos/`; a `receipt` row indexes it.
        The plaintext never touches disk. Returns the new receipt's metadata.
        `rut_claim_id` (set only by attach_rut_receipt) tags a Skatteverket kvittens.
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
                "INSERT INTO receipt(transaktion_id, rut_claim_id, filename, mime, "
                "original_format, byte_size, sha256, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (transaktion_id, rut_claim_id, filename, mime, original_format,
                 len(data), hashlib.sha256(enc).hexdigest(), _now()),
            )
        return {"id": cur.lastrowid, "transaktion_id": transaktion_id,
                "rut_claim_id": rut_claim_id,
                "filename": filename, "mime": mime, "byte_size": len(data)}

    def list_receipts(self, transaktion_id: int) -> list[dict]:
        # Only the transaktion's own receipts; a Skatteverket kvittens (rut_claim_id set)
        # is listed via list_rut_receipts instead.
        return [dict(r) for r in self.conn.execute(
            "SELECT id, transaktion_id, mime, original_format, byte_size, created_at "
            "FROM receipt WHERE transaktion_id=? AND rut_claim_id IS NULL ORDER BY id",
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

    def _freeze_line_konton(self, transaktion_id: int) -> None:
        """
        Stamp every not-yet-frozen moms_line of `transaktion_id` with the BAS-konto it is
        being booked to (the line's category, falling back to the transaktion's).

        Reference data is editable: a category's BAS-konto may be corrected afterwards.
        The postings already carry the konto as a frozen number, so huvudbok/SIE/årsbokslut
        are immune — but the result report resolves the category. Freezing the konto on
        the moms_line at booking makes that report immune too: a later edit only affects
        what is booked from then on. MUST run inside a `with self.conn` block.
        """
        fb_row = self.conn.execute(
            "SELECT category_id FROM transaktion WHERE id=?", (transaktion_id,)).fetchone()
        fb = fb_row["category_id"] if fb_row else None
        for ln in self.conn.execute(
                "SELECT id, category_id FROM moms_line "
                "WHERE transaktion_id=? AND bas_konto IS NULL", (transaktion_id,)).fetchall():
            cat = ln["category_id"] if ln["category_id"] is not None else fb
            if cat is None:
                continue
            self.conn.execute("UPDATE moms_line SET bas_konto=? WHERE id=?",
                              (self._category_konto(cat), ln["id"]))

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
        # The clone mirrors an already-booked entry, so it must carry the SAME frozen
        # konto — otherwise a rättelse/återföring would not net out in the result report
        # if the category's konto has been edited in the meantime.
        self._freeze_line_konton(src_transaktion_id)
        lines = self.conn.execute(
            "SELECT rate_code, category_id, ex_moms_ore, moms_ore, inc_moms_ore, bas_konto "
            "FROM moms_line WHERE transaktion_id=?", (src_transaktion_id,),
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
                "moms_ore, inc_moms_ore, bas_konto) VALUES (?,?,?,?,?,?,?)",
                (rid, ln["rate_code"], ln["category_id"], sign * ln["ex_moms_ore"],
                 sign * ln["moms_ore"], sign * ln["inc_moms_ore"], ln["bas_konto"]),
            )
        return rid

    def _insert_transaktion(self, *, direction, category_id, supplier_id, customer_id,
                            trans_date, note, receipt_original_format, snapshot_enc,
                            ext_ref=None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO transaktion(direction, category_id, supplier_id, customer_id, "
                "trans_date, status, customer_snapshot_enc, receipt_original_format, note, "
                "ext_ref, created_at) VALUES (?,?,?,?,?, 'pending', ?,?,?,?,?)",
                (direction, category_id, supplier_id, customer_id, trans_date,
                 snapshot_enc, receipt_original_format, note, ext_ref, _now()),
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
                rc = _clean_reverse_charge(rate_code, ln.get("reverse_charge"))
                # Reverse charge: the supplier invoiced WITHOUT moms, so the amount on the
                # receipt is always the beskattningsunderlag — the moms is computed on top
                # of it (and then reported on both sides), never extracted from it.
                inclusive = False if rc else ln.get("inclusive", True)
                ex, moms, inc = compute_moms_figures(ln["amount_ore"], rate_code, inclusive)
                self.conn.execute(
                    "INSERT INTO moms_line(transaktion_id, rate_code, category_id, ex_moms_ore, "
                    "moms_ore, inc_moms_ore, reverse_charge) VALUES (?,?,?,?,?,?,?)",
                    (transaktion_id, rate_code, ln.get("category_id"), ex, moms, inc, rc),
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

    def _reverse_charge_moms(self, transaktion_id: int) -> dict[str, int]:
        """{rate_code: moms} for the transaktion's omvänd-betalningsskyldighet lines only
        (the part you owe as utgående moms and simultaneously deduct as ingående)."""
        agg: dict[str, int] = {}
        for r in self.conn.execute(
                "SELECT rate_code, moms_ore FROM moms_line "
                "WHERE transaktion_id=? AND reverse_charge IS NOT NULL", (transaktion_id,)):
            if r["moms_ore"]:
                agg[r["rate_code"]] = agg.get(r["rate_code"], 0) + r["moms_ore"]
        return agg

    def _next_ver_number(self, series: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(ver_number) FROM verifikation WHERE series=?", (series,)
        ).fetchone()
        return (row[0] or 0) + 1

    def _post_verifikation(self, ver_date: str, reg_date: str, text: str,
                           postings: list[tuple[int, int, str | None]],
                           series: str = "A",
                           rattelse_of: Optional[int] = None,
                           egenupprattad: bool = False,
                           motivering: Optional[str] = None,
                           ext_ref: Optional[str] = None,
                           kommentar: Optional[str] = None) -> tuple[int, int]:
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
            "text, posted, rattelse_of, egenupprattad, motivering, ext_ref, kommentar, "
            "created_at) VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
            (series, number, ver_date, reg_date, text, rattelse_of,
             int(bool(egenupprattad)), (motivering or None),
             ((ext_ref or "").strip() or None), ((kommentar or "").strip() or None), _now()),
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
