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

SCHEMA_VERSION = 28

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
    default_rate_code TEXT CHECK (default_rate_code IN
                       ('25','12','6','0','momsfri','ej_avdragsgill')),  -- default moms
    prefix     TEXT UNIQUE,   -- unique 4-digit article-number prefix (articles: <prefix>-XXXX)
    parent_id  INTEGER REFERENCES category(id),  -- NULL = top-level; else a subcategory
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
    address          TEXT,               -- legacy single-line billing address (composed from parts)
    shipping_address TEXT,               -- leveransadress (if different)
    -- structured billing address (CLAUDE.md > Customers); country defaults to Sverige
    street           TEXT,
    zip_code         TEXT,
    city             TEXT,
    country          TEXT,
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
    ext_ref                TEXT,        -- supplier's kvitto-/fakturanummer (purchases)
    created_at             TEXT NOT NULL
);

-- ----- per-rate moms split (the THREE figures, one row per rate) -----------
CREATE TABLE moms_line (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaktion_id INTEGER NOT NULL REFERENCES transaktion(id),
    rate_code      TEXT NOT NULL CHECK (rate_code IN
                       ('25','12','6','0','momsfri','ej_avdragsgill')),
    category_id    INTEGER REFERENCES category(id),  -- per-line income/expense account
                                                     -- (NULL = transaktion.category_id)
    ex_moms_ore    INTEGER NOT NULL,    -- beskattningsunderlag
    moms_ore       INTEGER NOT NULL,    -- ingående (purchase) / utgående (sale)
    inc_moms_ore   INTEGER NOT NULL     -- total
);

-- ----- RUT lifecycle (private-customer income only) -----------------------
CREATE TABLE rut_claim (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    transaktion_id            INTEGER NOT NULL REFERENCES transaktion(id),
    customer_id               INTEGER REFERENCES customer(kundnummer),  -- NULL if the customer card was deleted
    rut_amount_ore            INTEGER NOT NULL,
    state                     TEXT NOT NULL DEFAULT 'pending'
                              CHECK (state IN ('pending','customer_paid','skatteverket_paid')),
    customer_payment_date     TEXT,     -- lands in a different month than ...
    skatteverket_payment_date TEXT,     -- ... the Skatteverket payment
    -- the Skatteverket payment is booked as its OWN verifikation (the customer
    -- payment is booked via transaktion.verifikation_id).
    skatteverket_verifikation_id INTEGER REFERENCES verifikation(id),
    skatteverket_received_ore INTEGER,  -- actual amount paid out (may differ from claimed)
    shortfall_invoice_id      INTEGER REFERENCES invoice(id),  -- follow-up if SKV underpaid
    skatteverket_reference    TEXT,     -- name of the RUT/ROT begäran (e.g. "RUT1")
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
    rut_claim_id    INTEGER REFERENCES rut_claim(id),  -- set: Skatteverket kvittens for a claim
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
    rut_total_ore            INTEGER NOT NULL DEFAULT 0,  -- RUT skattereduktion total
    rot_total_ore            INTEGER NOT NULL DEFAULT 0,  -- ROT skattereduktion total
    cancelled_at             TEXT,                  -- makulerad (voided before booking)
    credited_at              TEXT,                  -- krediterad (booking reversed)
    credit_verifikation_id   INTEGER REFERENCES verifikation(id),
    -- husavdrag follow-up: when Skatteverket pays less than the claimed RUT/ROT, the
    -- shortfall becomes a receivable on the customer documented as this linked invoice.
    parent_invoice_id        INTEGER REFERENCES invoice(id),
    relation_note            TEXT,                  -- free-text reference to the original
    husavdrag_shortfall_ore  INTEGER NOT NULL DEFAULT 0,  -- >0 marks a follow-up; settles 1510, no moms
    -- "gratis distanssupport": earned support time per invoice (magIT support offer).
    support_minutes_earned   INTEGER NOT NULL DEFAULT 0,  -- 15 min per full 500 kr of the total
    support_expiry_date      TEXT,                  -- invoice_date + 36 months
    created_at               TEXT NOT NULL
);

-- ----- invoice settlement subledger: each payment / refund / credit is an event
-- that books its own verifikation; the invoice's outstanding balance + state are
-- derived from these (supports partial payments, partial credits, and refunds).
CREATE TABLE invoice_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER NOT NULL REFERENCES invoice(id),
    kind            TEXT NOT NULL CHECK (kind IN ('payment','refund','credit')),
    amount_ore      INTEGER NOT NULL,        -- inc-moms amount of this event (positive)
    date            TEXT NOT NULL,
    verifikation_id INTEGER REFERENCES verifikation(id),
    credit_note_number INTEGER,              -- kreditfaktura number (credit events; shares the faktura series)
    note            TEXT,
    created_at      TEXT NOT NULL
);

-- ----- invoice drafts: an unissued, editable faktura saved to continue later. The
-- whole form payload is stored encrypted (it may carry recipient personnummer); a
-- draft has NO number and books nothing until it is issued via create_invoice.
CREATE TABLE invoice_draft (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,                          -- for the list view (nullable while editing)
    payload_enc TEXT NOT NULL,                    -- AES-256-GCM(DEK) JSON of the form inputs
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ----- invoice line items (articles) -------------------------------------
CREATE TABLE invoice_line (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id     INTEGER NOT NULL REFERENCES invoice(id),
    line_no        INTEGER NOT NULL,
    description    TEXT NOT NULL,
    category_id    INTEGER REFERENCES category(id),   -- income account this line books to
    quantity_centi INTEGER NOT NULL,                  -- quantity * 100 (1.50 -> 150)
    unit           TEXT,                              -- "h", "st", ...
    unit_price_ore INTEGER NOT NULL,                  -- ex moms, per unit
    rate_code      TEXT NOT NULL CHECK (rate_code IN
                       ('25','12','6','0','momsfri','ej_avdragsgill')),
    rut_eligible   INTEGER NOT NULL DEFAULT 0,        -- derived: 1 when reduction_type set
    reduction_type TEXT CHECK (reduction_type IN ('rut','rot')),  -- husavdrag kind (NULL = none)
    article_id     INTEGER REFERENCES article(id),    -- catalog article this line came from
    discount_pct_centi INTEGER NOT NULL DEFAULT 0,    -- per-line % rabatt * 100 (15 % -> 1500)
    stock_batch_id INTEGER REFERENCES stock_batch(id),-- picked stock batch (for real margin)
    cost_ore       INTEGER NOT NULL DEFAULT 0,        -- frozen inköpskostnad for this line (margin)
    ex_moms_ore    INTEGER NOT NULL,                  -- line total ex moms, AFTER discount
    moms_ore       INTEGER NOT NULL
);

-- ----- article catalog: reusable products/services for invoice lines. The number is
-- xxxx-xxxx (user picks the 4-digit prefix; the suffix is random + unique). Picking an
-- article prefills a line; the price (and everything else) stays editable on the line.
CREATE TABLE article (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    article_number TEXT NOT NULL UNIQUE,             -- xxxx-xxxx
    description    TEXT NOT NULL,
    unit           TEXT,
    unit_price_ore INTEGER NOT NULL DEFAULT 0,       -- default à-pris ex moms (editable on use)
    rate_code      TEXT NOT NULL DEFAULT '25' CHECK (rate_code IN
                       ('25','12','6','0','momsfri','ej_avdragsgill')),
    reduction_type TEXT CHECK (reduction_type IN ('rut','rot')),
    category_id    INTEGER REFERENCES category(id),  -- NULL = uncategorised
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- ----- stock (lager): each buy-in of an article creates a BATCH with its own cost.
-- Buying the same article again adds a new batch, so one article can carry several
-- costs. The batch_number is a global sequential label shown in the Lager tab. When an
-- invoice line picks a batch, qty_remaining_centi is decremented so on-hand stock and
-- the real margin (revenue − batch cost) fall out. Pure tracking — no ledger impact.
CREATE TABLE stock_batch (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_number            INTEGER NOT NULL,           -- sequential WITHIN the article
    article_id              INTEGER NOT NULL REFERENCES article(id),
    qty_in_centi            INTEGER NOT NULL,           -- quantity bought in * 100
    qty_remaining_centi     INTEGER NOT NULL,           -- decremented as it is sold
    unit_cost_ore           INTEGER NOT NULL,           -- ex-moms inköpspris per unit
    supplier_id             INTEGER REFERENCES supplier(id),        -- optional source supplier
    purchase_transaktion_id INTEGER REFERENCES transaktion(id),     -- optional linked inköp
    received_date           TEXT NOT NULL,
    note                    TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE (article_id, batch_number)   -- full batch id = article_number + batch_number
);

-- ----- RUT/ROT recipients: a household can split the skattereduktion across ----
-- several people, each their own name + personnummer (encrypted), linked customer,
-- a share percentage, and their resulting RUT and ROT amounts (frozen on the invoice).
CREATE TABLE rut_recipient (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id       INTEGER NOT NULL REFERENCES invoice(id),
    customer_id      INTEGER REFERENCES customer(kundnummer),  -- the household member
    first_name       TEXT NOT NULL,
    last_name        TEXT NOT NULL,
    personnummer_enc TEXT NOT NULL,
    share_pct_centi  INTEGER NOT NULL DEFAULT 10000,   -- RUT share, percent * 100 (100 % = 10000)
    rot_share_pct_centi INTEGER,                       -- ROT share (NULL = same as RUT share)
    rut_amount_ore   INTEGER NOT NULL DEFAULT 0,       -- this person's share of the RUT pot
    rot_amount_ore   INTEGER NOT NULL DEFAULT 0        -- this person's share of the ROT pot
);

-- ----- customer relations: symmetric household links between customers. The pair
-- is stored ordered (customer_a < customer_b) and unique so the link is undirected.
CREATE TABLE customer_relation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_a  INTEGER NOT NULL REFERENCES customer(kundnummer),
    customer_b  INTEGER NOT NULL REFERENCES customer(kundnummer),
    created_at  TEXT NOT NULL,
    CHECK (customer_a < customer_b),
    UNIQUE (customer_a, customer_b)
);

-- ----- free remote-support time bank (per customer): manual deductions when the
-- customer uses support, and manual additions outside the automatic invoice logic.
-- Earned time lives on the invoice (support_minutes_earned); this logs consumption.
CREATE TABLE support_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customer(kundnummer),
    minutes     INTEGER NOT NULL,        -- always positive; `kind` gives the direction
    kind        TEXT NOT NULL CHECK (kind IN ('deduction','addition')),
    note        TEXT,
    created_at  TEXT NOT NULL
);

-- Offert (quote): a numbered proposal document. Books NOTHING (no ledger/moms impact);
-- its own sequential offert_number series. The full render snapshot (buyer incl. pnr,
-- seller, lines, recipients, totals) is stored encrypted so the PDF can be regenerated.
CREATE TABLE offert (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    offert_number  INTEGER NOT NULL UNIQUE,
    customer_id    INTEGER REFERENCES customer(kundnummer),
    offert_date    TEXT NOT NULL,
    valid_until    TEXT,
    inc_moms_ore   INTEGER NOT NULL DEFAULT 0,
    husavdrag_ore  INTEGER NOT NULL DEFAULT 0,
    snapshot_enc   TEXT NOT NULL,
    source_draft_id INTEGER,             -- the draft it was created from (kept, not consumed)
    invoice_id     INTEGER REFERENCES invoice(id),  -- set once converted to a faktura
    created_at     TEXT NOT NULL
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
CREATE INDEX idx_invoice_event_inv   ON invoice_event(invoice_id);
CREATE INDEX idx_support_ledger_cust ON support_ledger(customer_id);
CREATE INDEX idx_stock_batch_article ON stock_batch(article_id);
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
# Husavdrag caps per customer per year. VERIFIED 2026-06 against Skatteverket:
# RUT up to 75 000 kr/year, ROT up to 50 000 kr/year (its own lower sub-cap), and
# RUT+ROT TOGETHER capped at 75 000 kr/year. So there are two limits: the shared
# combined cap AND the ROT-only sub-cap. Both config because the rules change.
_DEFAULT_CONFIG = {
    "rut_rot_cap_ore_per_customer_year": "7500000",  # combined RUT+ROT, 75 000 kr
    "rot_cap_ore_per_customer_year": "5000000",      # ROT sub-cap, 50 000 kr
    # Skattereduktion percentages on labour cost INCLUDING moms (rules change ->
    # config). RUT 50 %, ROT 30 % (verified 2026-06 against Skatteverket).
    "rut_reduction_pct": "50",
    "rot_reduction_pct": "30",
    # Bookkeeping method for invoices (per book): 'kontantmetod' (book at payment +
    # year-end accruals) or 'fakturametod' (book kundfordran/income/moms at issue,
    # then bank/kundfordran at payment). Default kontantmetod.
    "bokforingsmetod": "kontantmetod",
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
    "account_ores_kronutjamning": "3740",   # Öres- och kronutjämning (rounding)
    # When Skatteverket's husavdrag payout differs from the claimed amount by no more
    # than this many ören, treat it as pure rounding and book the diff to 3740. A
    # larger underpayment is a partial payout (a follow-up receivable on the customer).
    "rut_skv_rounding_tolerance_ore": "49",  # 0,49 kr
    # Year-end tax estimate for an ENSKILD NÄRINGSIDKARE (the "Skatt"-tab). Every annual
    # figure is editable config — update once a year from Skatteverkets "Belopp och
    # procent" and regeringens "Beräkningskonventioner 20XX". Percentages are stored as
    # centi-percent (28,97 % -> 2897); money as öre. Defaults are 2026 values.
    "prisbasbelopp_ore": "5920000",              # prisbasbelopp 2026 (59 200 kr)
    "kommunal_skattesats_pct_centi": "3237",     # kommunalskatt (SET YOUR KOMMUN)
    "begravningsavgift_pct_centi": "0",          # begravningsavgift (set your församling, t.ex. 7 = 0,07 %)
    "egenavgift_pct_centi": "2897",              # egenavgifter 28,97 %
    "egenavgift_nedsattning_pct_centi": "750",   # generell nedsättning 7,5 %
    "egenavgift_nedsattning_max_ore": "1500000", #   … max 15 000 kr/år
    "egenavgift_nedsattning_threshold_ore": "4000000",  # kräver överskott > 40 000 kr
    "public_service_pct_centi": "100",           # public service-avgift 1 %
    "public_service_max_ore": "118400",          #   … max 1 184 kr (2026)
    "statlig_skatt_pct_centi": "2000",           # statlig inkomstskatt 20 %
    "statlig_skiktgrans_ore": "64300000",        # skiktgräns 643 000 kr (2026)
    "skattered_forvarv_pct_centi": "75",         # skattereduktion förvärvsinkomst 0,75 %
    "skattered_forvarv_floor_ore": "4000000",    #   … på inkomst över 40 000 kr
    "skattered_forvarv_max_ore": "150000",       #   … max 1 500 kr
    "ovrig_forvarvsinkomst_ore": "0",            # lön/annan förvärvsinkomst (för totalöverblick)
    # Which year the tax figures above are for (informational; shown in the UI so you know
    # the vintage). If nothing is updated, the estimate keeps using these values.
    "tax_values_year": "2026",
    # Jobbskatteavdrag formula coefficients (Beräkningskonventioner 2026, Tabell 2.10),
    # stored as fraction × 10000 (0,3874 -> 3874; 0,91 PBB -> 9100). Editable so a new
    # year's construction can be entered without a code change.
    "jsa_break1_centi": "9100",                  # bracket edge 0,91 PBB
    "jsa_break2_centi": "32400",                 # bracket edge 3,24 PBB
    "jsa_break3_centi": "80800",                 # bracket edge 8,08 PBB
    "jsa_c2_centi": "3874",                       # slope in bracket 2 (0,3874)
    "jsa_c3_centi": "2510",                        # slope in bracket 3 (0,251)
    "jsa_b3_base_centi": "18130",                 # belopp at start of bracket 3 (1,813 PBB)
    "jsa_b4_level_centi": "30270",                # belopp in bracket 4 (3,027 PBB)
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
    6: """
        ALTER TABLE invoice ADD COLUMN cancelled_at TEXT;
        ALTER TABLE invoice ADD COLUMN credited_at TEXT;
        ALTER TABLE invoice ADD COLUMN credit_verifikation_id INTEGER;
    """,
    7: """
        CREATE TABLE IF NOT EXISTS invoice_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoice(id),
            kind TEXT NOT NULL CHECK (kind IN ('payment','refund','credit')),
            amount_ore INTEGER NOT NULL, date TEXT NOT NULL,
            verifikation_id INTEGER REFERENCES verifikation(id),
            note TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_invoice_event_inv ON invoice_event(invoice_id);
    """,
    8: """
        ALTER TABLE invoice_event ADD COLUMN credit_note_number INTEGER;
    """,
    9: """
        ALTER TABLE customer ADD COLUMN street TEXT;
        ALTER TABLE customer ADD COLUMN zip_code TEXT;
        ALTER TABLE customer ADD COLUMN city TEXT;
        ALTER TABLE customer ADD COLUMN country TEXT;
        ALTER TABLE category ADD COLUMN default_rate_code TEXT;
        ALTER TABLE moms_line ADD COLUMN category_id INTEGER;
        ALTER TABLE invoice_line ADD COLUMN category_id INTEGER;
    """,
    10: """
        ALTER TABLE invoice ADD COLUMN rot_total_ore INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE invoice_line ADD COLUMN reduction_type TEXT;
        ALTER TABLE rut_recipient ADD COLUMN customer_id INTEGER;
        ALTER TABLE rut_recipient ADD COLUMN share_pct_centi INTEGER NOT NULL DEFAULT 10000;
        ALTER TABLE rut_recipient ADD COLUMN rot_amount_ore INTEGER NOT NULL DEFAULT 0;
        INSERT OR IGNORE INTO config(key, value) VALUES ('rut_reduction_pct', '50');
        INSERT OR IGNORE INTO config(key, value) VALUES ('rot_reduction_pct', '30');
        CREATE TABLE IF NOT EXISTS customer_relation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_a INTEGER NOT NULL REFERENCES customer(kundnummer),
            customer_b INTEGER NOT NULL REFERENCES customer(kundnummer),
            created_at TEXT NOT NULL,
            CHECK (customer_a < customer_b),
            UNIQUE (customer_a, customer_b)
        );
    """,
    11: """
        ALTER TABLE rut_recipient ADD COLUMN rot_share_pct_centi INTEGER;
    """,
    12: """
        INSERT OR IGNORE INTO config(key, value) VALUES ('rot_cap_ore_per_customer_year', '5000000');
    """,
    13: """
        CREATE TABLE IF NOT EXISTS invoice_draft (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            payload_enc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """,
    14: """
        CREATE TABLE IF NOT EXISTS article (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_number TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            unit TEXT,
            unit_price_ore INTEGER NOT NULL DEFAULT 0,
            rate_code TEXT NOT NULL DEFAULT '25',
            reduction_type TEXT,
            category_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        ALTER TABLE invoice_line ADD COLUMN article_id INTEGER;
    """,
    15: """
        ALTER TABLE invoice ADD COLUMN parent_invoice_id INTEGER;
        ALTER TABLE invoice ADD COLUMN relation_note TEXT;
        ALTER TABLE invoice ADD COLUMN husavdrag_shortfall_ore INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE rut_claim ADD COLUMN skatteverket_received_ore INTEGER;
        ALTER TABLE rut_claim ADD COLUMN shortfall_invoice_id INTEGER;
        INSERT OR IGNORE INTO config(key, value) VALUES ('account_ores_kronutjamning', '3740');
        INSERT OR IGNORE INTO config(key, value) VALUES ('rut_skv_rounding_tolerance_ore', '49');
    """,
    16: """
        ALTER TABLE invoice_line ADD COLUMN discount_pct_centi INTEGER NOT NULL DEFAULT 0;
    """,
    17: """
        ALTER TABLE rut_claim ADD COLUMN skatteverket_reference TEXT;
        ALTER TABLE receipt ADD COLUMN rut_claim_id INTEGER;
    """,
    18: """
        ALTER TABLE invoice ADD COLUMN support_minutes_earned INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE invoice ADD COLUMN support_expiry_date TEXT;
        CREATE TABLE IF NOT EXISTS support_ledger (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customer(kundnummer),
            minutes     INTEGER NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('deduction','addition')),
            note        TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_support_ledger_cust ON support_ledger(customer_id);
        UPDATE invoice SET
            support_minutes_earned = (inc_moms_ore / 50000) * 15,
            support_expiry_date = date(invoice_date, '+36 months')
            WHERE husavdrag_shortfall_ore = 0;
    """,
    19: """
        INSERT OR IGNORE INTO config(key, value) VALUES ('egenavgift_pct_centi', '2897');
        INSERT OR IGNORE INTO config(key, value) VALUES ('egenavgift_schablon_pct_centi', '2500');
        INSERT OR IGNORE INTO config(key, value) VALUES ('kommunal_skattesats_pct_centi', '3237');
        INSERT OR IGNORE INTO config(key, value) VALUES ('statlig_skatt_pct_centi', '2000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('statlig_brytpunkt_ore', '64310000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('grundavdrag_ore', '1650000');
    """,
    20: """
        INSERT OR IGNORE INTO config(key, value) VALUES ('prisbasbelopp_ore', '5920000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('begravningsavgift_pct_centi', '0');
        INSERT OR IGNORE INTO config(key, value) VALUES ('egenavgift_nedsattning_pct_centi', '750');
        INSERT OR IGNORE INTO config(key, value) VALUES ('egenavgift_nedsattning_max_ore', '1500000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('egenavgift_nedsattning_threshold_ore', '4000000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('public_service_pct_centi', '100');
        INSERT OR IGNORE INTO config(key, value) VALUES ('public_service_max_ore', '118400');
        INSERT OR IGNORE INTO config(key, value) VALUES ('statlig_skiktgrans_ore', '64300000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('skattered_forvarv_pct_centi', '75');
        INSERT OR IGNORE INTO config(key, value) VALUES ('skattered_forvarv_floor_ore', '4000000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('skattered_forvarv_max_ore', '150000');
        INSERT OR IGNORE INTO config(key, value) VALUES ('ovrig_forvarvsinkomst_ore', '0');
    """,
    21: """
        INSERT OR IGNORE INTO config(key, value) VALUES ('tax_values_year', '2026');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_break1_centi', '9100');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_break2_centi', '32400');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_break3_centi', '80800');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_c2_centi', '3874');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_c3_centi', '2510');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_b3_base_centi', '18130');
        INSERT OR IGNORE INTO config(key, value) VALUES ('jsa_b4_level_centi', '30270');
    """,
    22: """
        ALTER TABLE transaktion ADD COLUMN ext_ref TEXT;
    """,
    23: """
        CREATE TABLE IF NOT EXISTS offert (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            offert_number  INTEGER NOT NULL UNIQUE,
            customer_id    INTEGER REFERENCES customer(kundnummer),
            offert_date    TEXT NOT NULL,
            valid_until    TEXT,
            inc_moms_ore   INTEGER NOT NULL DEFAULT 0,
            husavdrag_ore  INTEGER NOT NULL DEFAULT 0,
            snapshot_enc   TEXT NOT NULL,
            source_draft_id INTEGER,
            created_at     TEXT NOT NULL
        );
    """,
    24: """
        ALTER TABLE offert ADD COLUMN invoice_id INTEGER REFERENCES invoice(id);
    """,
    25: """
        CREATE TABLE IF NOT EXISTS stock_batch (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_number            INTEGER NOT NULL UNIQUE,
            article_id              INTEGER NOT NULL REFERENCES article(id),
            qty_in_centi            INTEGER NOT NULL,
            qty_remaining_centi     INTEGER NOT NULL,
            unit_cost_ore           INTEGER NOT NULL,
            supplier_id             INTEGER REFERENCES supplier(id),
            purchase_transaktion_id INTEGER REFERENCES transaktion(id),
            received_date           TEXT NOT NULL,
            note                    TEXT,
            created_at              TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stock_batch_article ON stock_batch(article_id);
        ALTER TABLE invoice_line ADD COLUMN stock_batch_id INTEGER;
        ALTER TABLE invoice_line ADD COLUMN cost_ore INTEGER NOT NULL DEFAULT 0;
    """,
    26: """
        ALTER TABLE category ADD COLUMN prefix TEXT;
        UPDATE category SET prefix = printf('%04d',
            (SELECT COUNT(*) FROM category c2 WHERE c2.id <= category.id) - 1)
            WHERE prefix IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_category_prefix ON category(prefix);
        DROP INDEX IF EXISTS idx_stock_batch_article;
        ALTER TABLE stock_batch RENAME TO stock_batch_old;
        CREATE TABLE stock_batch (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_number            INTEGER NOT NULL,
            article_id              INTEGER NOT NULL REFERENCES article(id),
            qty_in_centi            INTEGER NOT NULL,
            qty_remaining_centi     INTEGER NOT NULL,
            unit_cost_ore           INTEGER NOT NULL,
            supplier_id             INTEGER REFERENCES supplier(id),
            purchase_transaktion_id INTEGER REFERENCES transaktion(id),
            received_date           TEXT NOT NULL,
            note                    TEXT,
            created_at              TEXT NOT NULL,
            UNIQUE (article_id, batch_number)
        );
        INSERT INTO stock_batch (id, batch_number, article_id, qty_in_centi,
            qty_remaining_centi, unit_cost_ore, supplier_id, purchase_transaktion_id,
            received_date, note, created_at)
            SELECT o.id,
                (SELECT COUNT(*) FROM stock_batch_old b2
                     WHERE b2.article_id = o.article_id AND b2.id <= o.id),
                o.article_id, o.qty_in_centi, o.qty_remaining_centi, o.unit_cost_ore,
                o.supplier_id, o.purchase_transaktion_id, o.received_date, o.note, o.created_at
            FROM stock_batch_old o;
        DROP TABLE stock_batch_old;
        CREATE INDEX IF NOT EXISTS idx_stock_batch_article ON stock_batch(article_id);
    """,
    27: """
        ALTER TABLE category ADD COLUMN parent_id INTEGER REFERENCES category(id);
    """,
    28: """
        DROP INDEX IF EXISTS idx_rut_state;
        ALTER TABLE rut_claim RENAME TO rut_claim_old;
        CREATE TABLE rut_claim (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            transaktion_id            INTEGER NOT NULL REFERENCES transaktion(id),
            customer_id               INTEGER REFERENCES customer(kundnummer),
            rut_amount_ore            INTEGER NOT NULL,
            state                     TEXT NOT NULL DEFAULT 'pending'
                                      CHECK (state IN ('pending','customer_paid','skatteverket_paid')),
            customer_payment_date     TEXT,
            skatteverket_payment_date TEXT,
            skatteverket_verifikation_id INTEGER REFERENCES verifikation(id),
            skatteverket_received_ore INTEGER,
            shortfall_invoice_id      INTEGER REFERENCES invoice(id),
            skatteverket_reference    TEXT,
            claim_year                INTEGER NOT NULL,
            created_at                TEXT NOT NULL
        );
        INSERT INTO rut_claim (id, transaktion_id, customer_id, rut_amount_ore, state,
            customer_payment_date, skatteverket_payment_date, skatteverket_verifikation_id,
            skatteverket_received_ore, shortfall_invoice_id, skatteverket_reference,
            claim_year, created_at)
            SELECT id, transaktion_id, customer_id, rut_amount_ore, state,
                customer_payment_date, skatteverket_payment_date, skatteverket_verifikation_id,
                skatteverket_received_ore, shortfall_invoice_id, skatteverket_reference,
                claim_year, created_at FROM rut_claim_old;
        DROP TABLE rut_claim_old;
        CREATE INDEX IF NOT EXISTS idx_rut_state ON rut_claim(state);
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
