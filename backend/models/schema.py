"""
schema.py — Layer 3: SQLite schema for one legal entity's books.

This is the legal core of the application. The tables here encode the rules from
CLAUDE.md > "Legal requirements", "Moms model", "RUT", "Customers", "Suppliers",
"Categories <-> BAS-konto". The *operations* that write these tables (assigning
verifikationsnummer, creating rättelser, RUT transitions) are Layer 4 — this layer
only defines the structure, the constraints, and the DB-level integrity guards.

Key design decisions made here (deliberate, legal-grade):

- **Money is stored as integer ören (1/100 kr), never floats.** Floating point
  cannot represent money exactly and a legal book must balance to the öre. All
  amount columns end in `_ore`. Use `kronor_to_ore` / `ore_to_kronor` at the edges.

- **Immutability is enforced in the database, not just in code.** Triggers ABORT
  any UPDATE/DELETE of a *posted* verifikation or its postings. Corrections must go
  through a rättelse (a new verifikation referencing the original via `rattelse_of`).
  This is the backstop for "the single most important integrity rule".

- **Verifikationsnummer is UNIQUE per series.** A draft verifikation has
  `ver_number = NULL`; Layer 4 assigns the next unbroken number atomically at the
  moment of posting. (SQLite allows many NULLs under a UNIQUE constraint, so drafts
  don't collide.)

- **Moms is split per rate.** One business transaction can carry several rates, so
  the three figures (ex-moms / moms / inc-moms) live in `moms_line`, one row per
  rate — matching the separate boxes on the momsdeklaration.

- **SIE-compatibility from the start.** `account` (= #KONTO), `verifikation`
  (= #VER) and `posting` (= #TRANS, balanced double-entry) mirror the SIE model so
  the Layer 6 exporter falls out naturally.

- **Sensitive fields are stored encrypted (b64 ciphertext).** Columns ending in
  `_enc` hold output of `core.crypto.encrypt_text` (personnummer, the on-invoice
  customer snapshot). Layer 4 does the encrypting; this layer just reserves them.
"""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------------------
# Versioning (also written to PRAGMA user_version for migrations / import checks)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 5

# ---------------------------------------------------------------------------
# Domain enumerations (kept in sync with the CHECK constraints in the DDL)
# ---------------------------------------------------------------------------

# Moms states — not just on/off (CLAUDE.md > Moms model).
MOMS_RATES: dict[str, Decimal | None] = {
    "25": Decimal("0.25"),
    "12": Decimal("0.12"),
    "6": Decimal("0.06"),
    "0": Decimal("0"),          # 0 % (e.g. exports) — still reported
    "momsfri": None,            # outside the VAT system
    "ej_avdragsgill": None,     # moms exists but is not deductible (e.g. representation)
}

CUSTOMER_TYPES = ("private", "business")
DIRECTIONS = ("in", "out")               # in = purchase/ingående, out = sale/utgående
TRANSACTION_STATES = ("pending", "paid")  # kontantmetod: book when money moves
RUT_STATES = ("pending", "customer_paid", "skatteverket_paid")
RECEIPT_FORMATS = ("paper", "digital")

# Default supplier moms rate before the user edits it (CLAUDE.md > Suppliers).
DEFAULT_MOMS_RATE = "25"


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
-- ----- app metadata -------------------------------------------------------
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ----- per-book configuration (CLAUDE.md: caps are config, not hardcoded) --
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ----- chart of accounts (SIE #KONTO). BAS-konto numbers are user-entered. -
CREATE TABLE account (
    bas_konto  INTEGER PRIMARY KEY,     -- e.g. 1930, 2640, 3001
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ----- categories <-> BAS-konto (reference data, freely editable) ----------
CREATE TABLE category (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('income','expense')),
    bas_konto  INTEGER NOT NULL REFERENCES account(bas_konto),
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- ----- customers (private or business). kundnummer is stable & immutable. --
CREATE TABLE customer (
    kundnummer      INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL CHECK (type IN ('private','business')),
    -- private
    first_name      TEXT,
    last_name       TEXT,
    personnummer_enc TEXT,              -- encrypted (GDPR-sensitive)
    -- business
    company_name    TEXT,
    org_nr          TEXT,
    contact_person  TEXT,
    vat_nr          TEXT,
    -- shared
    address          TEXT,               -- billing / faktureringsadress
    shipping_address TEXT,               -- leveransadress (if different)
    email           TEXT,
    phone           TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- ----- suppliers / expense counterparties ---------------------------------
-- org_nr / address are nullable (CLAUDE.md OPEN QUESTION) so they may be used
-- without being required.
CREATE TABLE supplier (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    default_moms_rate TEXT NOT NULL DEFAULT '25'
                      CHECK (default_moms_rate IN
                             ('25','12','6','0','momsfri','ej_avdragsgill')),
    org_nr            TEXT,
    address           TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

-- ----- verifikation (legal journal entry; SIE #VER). Immutable once posted. -
CREATE TABLE verifikation (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    series            TEXT NOT NULL DEFAULT 'A',
    ver_number        INTEGER,          -- NULL while draft; assigned at posting
    ver_date          TEXT NOT NULL,    -- bokföringsdatum (YYYY-MM-DD)
    registration_date TEXT NOT NULL,    -- when it was recorded
    text              TEXT NOT NULL,
    posted            INTEGER NOT NULL DEFAULT 0,
    rattelse_of       INTEGER REFERENCES verifikation(id),  -- correcting entry -> original
    created_at        TEXT NOT NULL,
    UNIQUE (series, ver_number)         -- unbroken sequence guard (NULLs allowed)
);

-- ----- posting (double-entry line; SIE #TRANS). Must balance per verifikation.
CREATE TABLE posting (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verifikation_id INTEGER NOT NULL REFERENCES verifikation(id),
    bas_konto       INTEGER NOT NULL REFERENCES account(bas_konto),
    amount_ore      INTEGER NOT NULL,   -- signed: debit > 0, credit < 0
    text            TEXT
);

-- ----- business transaction (the moms/RUT-bearing event) ------------------
CREATE TABLE transaktion (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    verifikation_id        INTEGER REFERENCES verifikation(id),  -- set when booked
    direction              TEXT NOT NULL CHECK (direction IN ('in','out')),
    category_id            INTEGER REFERENCES category(id),
    supplier_id            INTEGER REFERENCES supplier(id),        -- purchases
    customer_id            INTEGER REFERENCES customer(kundnummer),-- sales
    trans_date             TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','paid')),
    payment_date           TEXT,
    customer_snapshot_enc  TEXT,        -- frozen invoice customer details (encrypted)
    receipt_original_format TEXT CHECK (receipt_original_format IN ('paper','digital')),
    note                   TEXT,
    created_at             TEXT NOT NULL
);

-- ----- per-rate moms split (the THREE figures, one row per rate) -----------
CREATE TABLE moms_line (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaktion_id INTEGER NOT NULL REFERENCES transaktion(id),
    rate_code      TEXT NOT NULL CHECK (rate_code IN
                       ('25','12','6','0','momsfri','ej_avdragsgill')),
    ex_moms_ore    INTEGER NOT NULL,    -- beskattningsunderlag
    moms_ore       INTEGER NOT NULL,    -- ingående (purchase) / utgående (sale)
    inc_moms_ore   INTEGER NOT NULL     -- total
);

-- ----- RUT lifecycle (private-customer income only) -----------------------
CREATE TABLE rut_claim (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    transaktion_id            INTEGER NOT NULL REFERENCES transaktion(id),
    customer_id               INTEGER NOT NULL REFERENCES customer(kundnummer),
    rut_amount_ore            INTEGER NOT NULL,
    state                     TEXT NOT NULL DEFAULT 'pending'
                              CHECK (state IN ('pending','customer_paid','skatteverket_paid')),
    customer_payment_date     TEXT,     -- lands in a different month than ...
    skatteverket_payment_date TEXT,     -- ... the Skatteverket payment
    -- the Skatteverket payment is booked as its OWN verifikation (the customer
    -- payment is booked via transaktion.verifikation_id).
    skatteverket_verifikation_id INTEGER REFERENCES verifikation(id),
    claim_year                INTEGER NOT NULL,  -- for the per-customer/year cap
    created_at                TEXT NOT NULL
);

-- ----- period locking (after a momsdeklaration is filed) ------------------
CREATE TABLE period_lock (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,         -- inclusive YYYY-MM-DD
    period_end   TEXT NOT NULL,         -- inclusive YYYY-MM-DD
    kind         TEXT NOT NULL DEFAULT 'moms',
    locked_at    TEXT NOT NULL
);

-- ----- receipt photos (encrypted; stored as files in <db>.photos/) ---------
-- The file content is AES-256-GCM ciphertext (book DEK); this row is the index.
-- A transaktion may carry several (e.g. multi-page) receipts.
CREATE TABLE receipt (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaktion_id  INTEGER NOT NULL REFERENCES transaktion(id),
    filename        TEXT NOT NULL,      -- name inside <db>.photos/ (ciphertext file)
    mime            TEXT NOT NULL,
    original_format TEXT CHECK (original_format IN ('paper','digital')),
    byte_size       INTEGER NOT NULL,   -- plaintext size, for display
    sha256          TEXT NOT NULL,      -- of the ciphertext file (integrity)
    created_at      TEXT NOT NULL
);

-- ----- seller/company profile (single row id=1) — frozen onto each invoice ---
CREATE TABLE company (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT,
    org_nr      TEXT,
    vat_nr      TEXT,                                  -- momsregistreringsnummer
    address     TEXT,
    email       TEXT,
    phone       TEXT,
    f_skatt     INTEGER NOT NULL DEFAULT 1,            -- godkänd för F-skatt
    logo_enc    BLOB,                                  -- AES-GCM(DEK) PNG logo, on all documents
    updated_at  TEXT
);

-- ----- payment methods (Swish / Bankgiro / IBAN / ...) — label + number/link --
CREATE TABLE payment_method (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    value      TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1
);

-- ----- invoice (faktura). invoice_number is an UNBROKEN sequential series, ----
-- assigned at issue (legal, like verifikationsnummer). The buyer block (which may
-- hold a personnummer) is encrypted; the seller + payment-method blocks are frozen
-- as JSON so later edits never change an already-issued faktura.
CREATE TABLE invoice (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number           INTEGER UNIQUE,
    customer_id              INTEGER REFERENCES customer(kundnummer),
    transaktion_id           INTEGER REFERENCES transaktion(id),
    invoice_date             TEXT NOT NULL,
    due_date                 TEXT NOT NULL,
    delivery_date            TEXT,
    payment_terms            TEXT,
    buyer_snapshot_enc       TEXT,
    seller_snapshot          TEXT,
    payment_methods_snapshot TEXT,
    our_reference            TEXT,
    your_reference           TEXT,
    note                     TEXT,
    ex_moms_ore              INTEGER NOT NULL DEFAULT 0,
    moms_ore                 INTEGER NOT NULL DEFAULT 0,
    inc_moms_ore             INTEGER NOT NULL DEFAULT 0,
    rut_total_ore            INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL
);

-- ----- invoice line items (articles) -------------------------------------
CREATE TABLE invoice_line (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id     INTEGER NOT NULL REFERENCES invoice(id),
    line_no        INTEGER NOT NULL,
    description    TEXT NOT NULL,
    quantity_centi INTEGER NOT NULL,                  -- quantity * 100 (1.50 -> 150)
    unit           TEXT,                              -- "h", "st", ...
    unit_price_ore INTEGER NOT NULL,                  -- ex moms, per unit
    rate_code      TEXT NOT NULL CHECK (rate_code IN
                       ('25','12','6','0','momsfri','ej_avdragsgill')),
    rut_eligible   INTEGER NOT NULL DEFAULT 0,
    ex_moms_ore    INTEGER NOT NULL,                  -- line total ex moms
    moms_ore       INTEGER NOT NULL
);

-- ----- RUT recipients: a household can split RUT across several people, each ---
-- with their own name + personnummer (encrypted) and share of the skattereduktion.
CREATE TABLE rut_recipient (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id       INTEGER NOT NULL REFERENCES invoice(id),
    first_name       TEXT NOT NULL,
    last_name        TEXT NOT NULL,
    personnummer_enc TEXT NOT NULL,
    rut_amount_ore   INTEGER NOT NULL
);

-- ----- indexes ------------------------------------------------------------
CREATE INDEX idx_verifikation_date   ON verifikation(ver_date);
CREATE INDEX idx_posting_ver         ON posting(verifikation_id);
CREATE INDEX idx_transaktion_date    ON transaktion(trans_date);
CREATE INDEX idx_transaktion_status  ON transaktion(status);
CREATE INDEX idx_moms_line_trans     ON moms_line(transaktion_id);
CREATE INDEX idx_rut_state           ON rut_claim(state);
CREATE INDEX idx_customer_type       ON customer(type);
CREATE INDEX idx_receipt_trans       ON receipt(transaktion_id);
CREATE INDEX idx_invoice_number      ON invoice(invoice_number);
CREATE INDEX idx_invoice_line_inv    ON invoice_line(invoice_id);
CREATE INDEX idx_rut_recipient_inv   ON rut_recipient(invoice_id);
"""

# ---------------------------------------------------------------------------
# Immutability triggers — the legal backstop. A posted verifikation and its
# postings can never be altered or deleted; corrections go through a rättelse.
# Flipping posted 0 -> 1 (and assigning ver_number) is still allowed because at
# that moment OLD.posted = 0.
# ---------------------------------------------------------------------------

_TRIGGERS = """
CREATE TRIGGER trg_verifikation_no_update
BEFORE UPDATE ON verifikation
FOR EACH ROW WHEN OLD.posted = 1
BEGIN
    SELECT RAISE(ABORT, 'posted verifikation is immutable; create a rättelse');
END;

CREATE TRIGGER trg_verifikation_no_delete
BEFORE DELETE ON verifikation
FOR EACH ROW WHEN OLD.posted = 1
BEGIN
    SELECT RAISE(ABORT, 'posted verifikation cannot be deleted');
END;

CREATE TRIGGER trg_posting_no_update
BEFORE UPDATE ON posting
FOR EACH ROW
WHEN (SELECT posted FROM verifikation WHERE id = OLD.verifikation_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'postings of a posted verifikation are immutable');
END;

CREATE TRIGGER trg_posting_no_delete
BEFORE DELETE ON posting
FOR EACH ROW
WHEN (SELECT posted FROM verifikation WHERE id = OLD.verifikation_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'postings of a posted verifikation cannot be deleted');
END;
"""

# Per-book config defaults. The RUT/ROT cap is shared per customer per year and
# the rules change over time, so it lives here as editable config — NOT hardcoded
# in logic (CLAUDE.md > RUT). VERIFY the current amount against Skatteverket; this
# is only a starting default the user can change.
# Combined ROT+RUT cap, shared per customer per year. VERIFIED 2026-06 against
# Skatteverket: RUT max 75 000 kr/person/year; ROT 50 000 kr but ROT+RUT together
# capped at 75 000 kr/person/year — so this single shared cap is correct. Still
# config because the rules change. (ROT's subsidy rate is not modelled here; the
# RUT/ROT amount is taken as user input.)
_DEFAULT_CONFIG = {
    "rut_rot_cap_ore_per_customer_year": "7500000",  # 75 000 kr (verified 2026-06)
    # System BAS-konton used by the booking engine (Layer 4). Editable so a
    # revisor can map them to the entity's chart. Defaults follow standard BAS.
    "account_bank": "1930",                 # Företagskonto / bank
    "account_ingaende_moms": "2640",        # Ingående moms (deductible)
    "account_utgaende_moms_25": "2610",     # Utgående moms 25 %
    "account_utgaende_moms_12": "2620",     # Utgående moms 12 %
    "account_utgaende_moms_6": "2630",      # Utgående moms 6 %
    "account_rut_fordran": "1513",          # Kundfordran husavdrag (verified 2026-06)
    "account_kundfordran": "1510",          # Kundfordringar (year-end accrual)
    "account_leverantorsskuld": "2440",     # Leverantörsskulder (year-end accrual)
}


# ---------------------------------------------------------------------------
# Schema lifecycle
# ---------------------------------------------------------------------------

def initialize_schema(conn: sqlite3.Connection) -> None:
    """
    Create the full schema on a fresh database connection.

    Sets PRAGMA user_version to SCHEMA_VERSION and seeds meta + default config.
    Safe to call once on a new book; raises if the tables already exist.
    """
    conn.executescript(_DDL)
    conn.executescript(_TRIGGERS)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.executemany(
        "INSERT INTO config(key, value) VALUES (?, ?)",
        list(_DEFAULT_CONFIG.items()),
    )
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stored in PRAGMA user_version (0 if unset)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


# Forward migrations for already-created books, keyed by the version they bring the
# database UP TO. Each step is frozen (carries its own DDL) and idempotent.
_MIGRATIONS: dict[int, str] = {
    2: """
        CREATE TABLE IF NOT EXISTS receipt (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transaktion_id  INTEGER NOT NULL REFERENCES transaktion(id),
            filename        TEXT NOT NULL,
            mime            TEXT NOT NULL,
            original_format TEXT CHECK (original_format IN ('paper','digital')),
            byte_size       INTEGER NOT NULL,
            sha256          TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_receipt_trans ON receipt(transaktion_id);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS company (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT, org_nr TEXT, vat_nr TEXT, address TEXT, email TEXT, phone TEXT,
            f_skatt INTEGER NOT NULL DEFAULT 1, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payment_method (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, value TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS invoice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number INTEGER UNIQUE,
            customer_id INTEGER REFERENCES customer(kundnummer),
            transaktion_id INTEGER REFERENCES transaktion(id),
            invoice_date TEXT NOT NULL, due_date TEXT NOT NULL, delivery_date TEXT,
            payment_terms TEXT, buyer_snapshot_enc TEXT, seller_snapshot TEXT,
            payment_methods_snapshot TEXT, our_reference TEXT, your_reference TEXT, note TEXT,
            ex_moms_ore INTEGER NOT NULL DEFAULT 0, moms_ore INTEGER NOT NULL DEFAULT 0,
            inc_moms_ore INTEGER NOT NULL DEFAULT 0, rut_total_ore INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoice_line (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoice(id),
            line_no INTEGER NOT NULL, description TEXT NOT NULL,
            quantity_centi INTEGER NOT NULL, unit TEXT, unit_price_ore INTEGER NOT NULL,
            rate_code TEXT NOT NULL CHECK (rate_code IN
                ('25','12','6','0','momsfri','ej_avdragsgill')),
            rut_eligible INTEGER NOT NULL DEFAULT 0,
            ex_moms_ore INTEGER NOT NULL, moms_ore INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rut_recipient (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoice(id),
            first_name TEXT NOT NULL, last_name TEXT NOT NULL,
            personnummer_enc TEXT NOT NULL, rut_amount_ore INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_invoice_number   ON invoice(invoice_number);
        CREATE INDEX IF NOT EXISTS idx_invoice_line_inv ON invoice_line(invoice_id);
        CREATE INDEX IF NOT EXISTS idx_rut_recipient_inv ON rut_recipient(invoice_id);
    """,
    4: """
        ALTER TABLE company ADD COLUMN logo_enc BLOB;
    """,
    5: """
        ALTER TABLE customer ADD COLUMN shipping_address TEXT;
    """,
}


def migrate(conn: sqlite3.Connection) -> int:
    """
    Bring an existing book's schema up to SCHEMA_VERSION, running any missing
    forward migrations in order. No-op on a fresh/current database. Returns the
    resulting schema version.
    """
    # Migrations bring an EXISTING book up to date; a brand-new/empty database is
    # created at the current version by initialize_schema, not migrated into one.
    if not is_initialized(conn):
        return get_schema_version(conn)
    current = get_schema_version(conn)
    for target in sorted(_MIGRATIONS):
        if current < target:
            _run_migration(conn, _MIGRATIONS[target])
            conn.execute(f"PRAGMA user_version = {target}")
            current = target
    conn.commit()
    return current


def _run_migration(conn: sqlite3.Connection, script: str) -> None:
    """
    Execute a forward migration statement-by-statement, tolerating an already-applied
    `ALTER TABLE ... ADD COLUMN` (SQLite has no IF NOT EXISTS for it). This keeps
    migrations idempotent so a partially-migrated book can be re-run safely. The
    statements here contain no semicolons inside literals, so a naive split is safe.
    """
    for stmt in script.split(";"):
        sql = stmt.strip()
        if not sql:
            continue
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def is_initialized(conn: sqlite3.Connection) -> bool:
    """True if the schema has been created on this database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='verifikation'"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Money helpers — keep all arithmetic in integer ören.
# ---------------------------------------------------------------------------

def kronor_to_ore(value: str | int | float | Decimal) -> int:
    """Convert a kronor amount to integer ören (rounded half-up). 12.50 -> 1250."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ore_to_kronor(ore: int) -> Decimal:
    """Convert integer ören back to a kronor Decimal with 2 places. 1250 -> 12.50."""
    return (Decimal(ore) / 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Personnummer validation (format + Luhn). Required for RUT / private customers.
# ---------------------------------------------------------------------------

def normalize_personnummer(pnr: str) -> str:
    """Return the 10 significant digits (YYMMDDNNNN) of a personnummer, or ''."""
    digits = re.sub(r"\D", "", pnr)
    if len(digits) == 12:        # drop century prefix (e.g. 1981...)
        digits = digits[2:]
    return digits if len(digits) == 10 else ""


def is_valid_personnummer(pnr: str) -> bool:
    """
    Validate a Swedish personnummer: 10 or 12 digits and a correct Luhn check
    digit. Accepts optional separators (e.g. '811218-9876', '19811218-9876').
    Does not verify that the date itself is a real calendar date.
    """
    ten = normalize_personnummer(pnr)
    if not ten:
        return False
    return _luhn_ok(ten)


def _luhn_ok(ten_digits: str) -> bool:
    """Luhn (mod-10) over the first 9 digits, checked against the 10th."""
    total = 0
    for i, ch in enumerate(ten_digits[:9]):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    check = (10 - (total % 10)) % 10
    return check == int(ten_digits[9])


# ---------------------------------------------------------------------------
# Moms helpers
# ---------------------------------------------------------------------------

def moms_rate(rate_code: str) -> Decimal | None:
    """Return the decimal rate for a code, or None for momsfri/ej_avdragsgill."""
    if rate_code not in MOMS_RATES:
        raise ValueError(f"Unknown moms rate code: {rate_code!r}")
    return MOMS_RATES[rate_code]
