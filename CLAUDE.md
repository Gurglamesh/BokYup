# BokYup — Swedish bookkeeping application

A legal-grade bookkeeping (bokföring) application for Swedish small businesses.
Built to support **multiple separate legal entities** (e.g. an enskild firma and an
aktiebolag), each as its own **encrypted database**, switched between like browser tabs.

> This file is the source of truth for design decisions. Read it fully before
> making changes. The decisions here were made deliberately with the user; do not
> silently override them.

---

## Architecture (decided)

- **Backend-first, single source of truth in Python.** All legal/crypto logic lives
  in the Python backend so bookkeeping rules are written and verified **once**.
- **API boundary via FastAPI.** UI talks to the backend over HTTP. This makes the
  same logic reusable by future phone clients.
- **UI is a web frontend** (HTML/CSS/JS) — the only UI layer that is truly universal
  across Windows / macOS / Linux / Android / iOS.
- **Desktop today:** backend runs locally, UI served into a native window
  (pywebview) or the browser.
- **Phone later:** same web frontend wrapped (e.g. Capacitor) against the same API.
  Receipt capture via the phone camera through web media APIs.
- **OS-agnostic, pure-pip stack.** No native build steps. (This is why we do NOT use
  SQLCipher — see Encryption.)

## Multi-database / "tabs" model (decided)

- One database = one legal entity's complete books = one tab.
- Entities must never be commingled (Swedish law). Physical DB separation enforces this.
- Each database has its **own** encryption (own DEK, own salt, own passphrase).
- **Per-database passphrase only.** There is NO app-level password. The app opens to a
  list of known databases; each is unlocked individually with its passphrase.
- The app keeps a **registry** of known databases: display name + file path + last opened.
  The registry stores NO passphrases and NO keys.
- New book / open existing / import all behave like new-tab / open-file.

## Encryption (decided + implemented — `core/crypto.py`)

Envelope encryption, pure-Python (`argon2-cffi` + `cryptography`):

    passphrase --Argon2id(salt)--> KEK --wraps--> DEK --AES-256-GCM--> data

- **DEK** generated once per DB, NEVER changes. Encrypts DB fields + photo blobs.
- **KEK** derived from passphrase via Argon2id; only wraps/unwraps the DEK.
- **Change passphrase = re-wrap the same DEK.** No bulk re-encryption (fast, crash-safe).
  PROVEN by tests: same DEK survives passphrase change, old data still readable.
- **Recovery key**: optional second wrapping slot so a forgotten passphrase does not
  destroy a 7-year legal archive. Store offline.
- AES-256-GCM everywhere (authenticated; detects tampering).
- We deliberately do **application-level field/blob encryption** instead of SQLCipher
  to keep the stack pip-installable on every OS including phone wrappers.
- **Argon2id `parallelism` is fixed at 1 (decided 2026-06, do NOT raise it).** The same
  Python crypto core runs on the phone as WebAssembly (Pyodide), which has no pthreads:
  any parallelism > 1 raises "Threading failure" there, and a single lane makes the KEK
  derivation bit-for-bit deterministic across PC and phone. This is what lets a book
  exported on a PC unlock unchanged on the phone (verified: native CPython and Pyodide
  314 produce identical KEKs — frozen as a contract in `tests/test_crypto_vectors.py`).
  Strengthen the KEK via `time_cost`/`memory_cost`, never `parallelism`.

## Auto-lock (decided)

- Setting. Default **15 min** inactivity. Editable up to **60 min**. Can be turned off.
- On lock: DEK wiped from memory. On return: re-enter that database's passphrase.

## Legal requirements (this IS a real legal book — build strict)

- **Verifikationsnummer**: unbroken, sequential, per database. Never reused or skipped.
  Auto-assigned. This is the single most important integrity rule.
- **Immutability**: a posted verifikation may NOT be altered or deleted. Corrections are
  done via a **rättelse** (correcting entry that references the original); both remain
  visible. The user's "edit history" UX (approve/decline/cancel) must, on approve,
  create a rättelse — NOT mutate the original row.
- **Reference data** (categories, customer details, company/supplier defaults) MAY be
  edited freely. But edits must NOT rewrite already-issued invoices/verifikationer.
- **Snapshot-on-invoice**: when income/invoice is created, snapshot the customer details
  ONTO the record, independent of the live customer row (an invoice is frozen at issue).
- **Period locking**: once a momsdeklaration for a period is filed, lock the period so
  nothing back-dates into it.
- **Retention 7 years.** Receipts: store a flag for original format (paper vs digital);
  a photo of a digital receipt is not a valid substitute for the digital original.
- **Method**: kontantmetod (book when money moves) fits the pending→paid flow; at
  year-end even kontantmetod must book unpaid invoices.

## Moms (VAT) model

- Per transaction store THREE figures: ex-moms (beskattningsunderlag), moms amount,
  inc-moms total. Split by rate because the momsdeklaration has separate boxes.
- Purchases = ingående moms (deductible). Sales = utgående moms (owed). Model the sign.
- Moms states (not just on/off): **25% / 12% / 6% / 0% / momsfri / ej avdragsgill**.
- Per company/supplier: editable default moms rate (defaults to 25% before editing).

## RUT (private customers only)

- Lifecycle state machine: **pending → customer paid → Skatteverket paid**.
  RUT fields/buttons only appear for income flagged with a RUT amount.
- Track the date Skatteverket pays separately from the customer payment date
  (they land in different months in the books).
- RUT cap is per customer per year and shared with ROT — make the cap a CONFIG value,
  not hardcoded (rules change). Warn as a customer approaches the cap.
- RUT requires the customer's personnummer (so storing it is necessary & legitimate).

## Customers

- Type flag: **private** or **business**.
  - Private: first name, last name, personnummer, address, email, phone.
  - Business: company name, organisationsnummer, optional contact person, address,
    email, phone, VAT-nummer (if abroad/EU).
- Stable internal **kundnummer** (survives name/address edits).
- Personnummer: validate format + Luhn checksum on entry. Sensitive data (GDPR) —
  another reason the DB is encrypted at rest and backups are encrypted.
- RUT only applies to private customers.

## Suppliers/companies (expense counterparties)

- e.g. Inet, Webhallen, Kraüta, Electrokit. Minimum: name + default moms rate.
- OPEN QUESTION: whether to also carry org.nr/address for suppliers (user to decide).

## Categories ↔ BAS-konto

- Each expense/income categorized; can create new categories at entry time.
- Each category carries a BAS-konto number (user-entered).
- BAS-konto numbers feed the reports (momsdeklaration, then simplified deklaration).
- Categories editable after the fact; editing old transactions' category follows the
  approve(→rättelse)/decline/cancel rule above.

## Export / import (decided)

- Per-database. Export = a single bundle (`.buyn`, really a zip) containing:
  encrypted DB + encrypted photos + the wrapped-DEK envelope + a manifest
  (app version, schema version, export timestamp, checksum).
- **Option A** (default): the wrapped DEK travels in the bundle; unlock on the new
  device with the SAME passphrase. (Option B — separate export passphrase — later.)
- The export bundle doubles as the **encrypted backup** mechanism.
- Import semantics: **full restore / replace**, never merge (merging breaks the
  verifikationsnummer sequence — explicitly out of scope). Single authoritative device.
- Import must: verify checksum, check schema version (migrate or refuse), and
  auto-backup current state before overwriting.

## Reports (build order)

1. **Momsdeklaration helper** (most frequent — quarterly/monthly). Falls almost
   directly out of the BAS-konto + moms-rate data.
2. **Simplified deklaration** building block (NE-blankett direction).
3. **SIE export** (Swedish standard; every accounting program + revisor accepts it).
   Design data to be SIE-compatible from the start even before the exporter exists.

---

## Build status

- [x] **Layer 1 — Crypto core** (`core/crypto.py`) + tests (`tests/test_crypto.py`). DONE, all tests pass.
- [x] **Layer 2 — Database manager** (`db/manager.py`) + tests (`tests/test_manager.py`).
      Registry of books, create/open/lock sessions, recovery-key unlock, per-DB DEK in
      memory only. DONE, all tests pass. (Import/export bundle deferred to Layer 5.)
- [x] **Layer 3 — Schema** (`models/schema.py`) + tests (`tests/test_schema.py`).
      All tables (verifikation/posting, transaktion/moms_line, customer, supplier,
      category↔BAS, rut_claim, period_lock, rättelse via `verifikation.rattelse_of`),
      DB-level immutability triggers, personnummer Luhn + money (öre) helpers.
      DONE, all tests pass. NOTE: money stored as integer ören everywhere; the
      RUT/ROT cap is editable config (default 75 000 kr — VERIFIED 2026-06: combined
      ROT+RUT cap is 75 000 kr/person/year).
- [x] **Layer 4 — Core operations** (`db/operations.py`) + tests (`tests/test_operations.py`).
      Reference CRUD, kontantmetod pending→paid booking with balanced double-entry +
      unbroken verifikationsnummer, RUT state machine (2 verifikationer), rättelse via
      mirror postings, snapshot-on-invoice, period-lock enforcement. Also: year-end
      accrual of unpaid invoices (`book_year_end_accruals`, bokslut vändning — accrual
      on year-end + auto-reversal on Jan 1, so income+moms land in the closing year and
      the later cash payment isn't double-counted). DONE, all tests pass. NOTE: all
      system BAS-konton are config; `account_rut_fordran` (1513) VERIFIED 2026-06,
      kundfordran/leverantörsskuld (1510/2440) added for accruals.
- [x] **Layer 5 — Export/import** (`db/bundle.py`) + tests (`tests/test_bundle.py`).
      `.buyn` zip bundle (manifest + db snapshot via backup API + wrapped-DEK envelope
      + optional `<db>.photos/`), SHA-256 per-file checksums, schema-version gate,
      full-restore/replace with auto-backup-before-overwrite. Doubles as encrypted
      backup. Wired into DatabaseManager.export_book/import_book. DONE, all tests pass.
- [x] **Layer 6 — Reports** (`reports/vat.py`, `reports/result.py`, `reports/sie.py`)
      + tests (`tests/test_reports.py`). Momsdeklaration (boxes 05/10/11/12/48/49),
      result/NE building block (income/expense/result by category), SIE type-4 export
      (#KONTO + #VER/#TRANS, cp437/PC8; #IB/#UB/#RES emitted when a fiscal year is
      given). All filter on verifikation date of posted entries (kontantmetod). DONE,
      all tests pass. Rättelse and year-end accruals are netted in the moms/result
      reports via mirrored moms_lines (see Layer 4).
- [x] **Layer 7 — FastAPI API layer** (`api/app.py`, `api/schemas.py`) + tests
      (`tests/test_api.py`). create_app factory over DatabaseManager; per-DB unlock/
      lock endpoints, auto-lock (on-access check + background sweeper, default 15 min),
      reference + bookkeeping + reports + export/import routes, domain→HTTP error
      mapping (401/404/409/423). DONE, all tests pass. NOTE: localhost single-user
      backend — db/bundle paths in requests are trusted; do not expose on a network.
- [x] **Layer 8 — Web frontend** (`api/static/{index.html,styles.css,app.js}`,
      `desktop.py`) + tests (`tests/test_web.py`). Vanilla-JS tabbed SPA (multi-book
      tabs, unlock/lock, record income/expense, pending→paid, customers/suppliers/
      categories, momsdeklaration + SIE download), served at /app by the same FastAPI
      server; pywebview desktop launcher (`python -m backend.desktop`). DONE; JS
      `node --check`'d, server wiring tested, full stack live-smoke-tested.
      UI surfacing (2026-06): added RUT section (claim lifecycle + Skatteverket-payment,
      backed by new `GET /rut-claims`), Verifikat section (rättelse/reverse), Bokslut
      section (period locking + year-end accruals), SIE export with company/org/fiscal
      year, and the RUT-cap warning on record-income.
- [x] **Receipt capture + encrypted storage** (2026-06). New `receipt` table
      (SCHEMA_VERSION→2, `schema.migrate()` upgrades existing books on open). Photos are
      AES-256-GCM-encrypted with the book DEK and stored as files in `<db>.photos/`
      (already carried by the `.buyn` bundle); `BookOps.attach_receipt/list_receipts/
      get_receipt/delete_receipt` (delete only while the transaktion is still pending —
      once booked the receipt is part of the immutable record). API: base64 upload +
      list + raw-image GET + delete (no new server deps). Web UI: expense form now takes
      **multiple moms lines** (a receipt can mix 6/12/25 %) and a receipt picker (file
      import + `capture` for phone camera + live `getUserMedia` "Ta foto" on desktop);
      Transaktioner has a 📎 viewer. DONE, all tests pass, live-smoke-tested.
- [x] **Invoices (faktura) + PDF** (2026-06). Fully compliant Swedish faktura on PC
      AND phone. Schema v3 (company/payment_method/invoice/invoice_line/rut_recipient);
      `create_invoice` numbers sequentially (unbroken), snapshots buyer(enc)/seller/
      payment-methods, splits RUT across household recipients (each name + encrypted
      personnummer + share of the skattereduktion), and issues the underlying PENDING
      income so booking + reports are unchanged. PDF via **fpdf2** (`backend/invoices/
      pdf.py`) — pure-pip and verified under Pyodide, so it renders on the phone too
      (Pillow + fpdf2/defusedxml/fonttools vendored; pure wheels listed in
      `vendor/pure_wheels.json`). API `/company` `/payment-methods` `/invoices(/pdf)`;
      "Fakturor" UI with line-item + RUT-recipient editors; company + betalsätt in
      Inställningar. Verified end-to-end in a real browser on desktop and the phone
      WASM bundle. NOTE: per-recipient RUT *cap* tracking still uses the customer's
      claim (recipients captured for the document + future per-person cap).
      LOGO (schema v4): an editable per-book logo (`company.logo_enc`) shown on every
      document. Any PNG/JPG/WEBP is normalised to a size-bounded PNG via Pillow,
      DEK-encrypted, and drawn top-right on the faktura (current logo at render time).
      `set/get/delete_logo`, API `PUT/GET/DELETE /logo`, uploader in Inställningar.
      LAYOUT (schema v5): buyer block on top (Faktureras till + Leveransadress, with
      `customer.shipping_address`), seller details moved to a footer under the payment
      methods. LIFECYCLE (schema v6): driven from the Fakturor tab — bokför betalning
      (pay), makulera (void an UNBOOKED invoice; number stays reserved, pending
      transaktion removed, nothing hits the ledger), kreditera (reverse a BOOKED
      invoice via rättelse). `cancel_invoice`/`credit_invoice` + derived `state`
      (pending/paid/cancelled/credited); booking itself happens at payment
      (kontantmetod) — verified: balanced double-entry + moms into the
      momsdeklaration, RUT books the 1513 receivable.
- [ ] Later — **OCR** to auto-extract total + per-rate moms and prefill the lines editor
      (DEFERRED by decision: clashes with pure-pip/offline/privacy). Drop in behind a
      provider seam — `backend/ocr/` + `POST …/receipts/ocr-suggest` returning the same
      `{lines,total}` shape the form already edits. Engine choice still open.
- [~] **Phone = same backend as WASM (Pyodide), fully local** (2026-06). Decided NOT to
      run a server or talk to the PC: the phone runs the SAME Python backend compiled to
      WebAssembly inside the WebView, so legal logic stays written-once. Proven end-to-end
      (`tools/wasm-smoke/`): real `crypto.py` + sqlite3 run in WASM and `.buyn` export/
      import round-trips BOTH ways across native CPython ↔ WASM (encrypted fields + receipt
      photos intact). Enabler: Argon2 `parallelism=1` (see Encryption). Shipped: transport-
      independent `api/facade.py` (Phase 1), `api/phone.py` JSON boundary + `static/
      pyodide-boot.js` + `app.js` native branch (Phase 2), key-mgmt/backup/reference-edit
      UI (Phase 4). Scaffolded, build on a dev machine: `phone/` (Capacitor) + `packaging/`
      (PyInstaller). Remaining: run device/OS builds; phone `.buyn` file-bridge
      (`@capacitor/filesystem`+`share`). Camera receipt capture already works in the WebView.

## Working agreement

- Deliver in **working increments**. Each legal-critical layer must be testable and
  verified before the next sits on top of it.
- Keep all legal rules in the backend. Never reimplement them per platform.
- When in doubt about a Swedish tax/bookkeeping rule, verify it — do not guess on a
  legal book.

## Run / test

    python -m pip install -r requirements.txt
    python -m pytest            # or: python tests/test_crypto.py
