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
- **Both methods now selectable per book** (config `bokforingsmetod`, default
  kontantmetod; 2026-06). **Fakturametoden** books an invoice at issue (1510
  Kundfordringar + income + utgående moms, dated the invoice date → moms reported in
  the invoice's period) and again at payment (1930 Bank / 1510 settling the
  receivable). The issue posting is the transaktion's verifikation so the reports
  attribute moms to the invoice date; payment carries no moms_lines. register_payment
  is method-aware; year-end accrual + makulera skip issue-booked invoices; kreditera
  reverses the issue posting. Plain (non-invoice) incomes/expenses still book at
  payment. See `create_invoice`/`_book_invoice_issue`/`register_payment`.

## Moms (VAT) model

- Per transaction store THREE figures: ex-moms (beskattningsunderlag), moms amount,
  inc-moms total. Split by rate because the momsdeklaration has separate boxes.
- Purchases = ingående moms (deductible). Sales = utgående moms (owed). Model the sign.
- Moms states (not just on/off): **25% / 12% / 6% / 0% / momsfri / ej avdragsgill**.
- Per company/supplier: editable default moms rate (defaults to 25% before editing).

## RUT / ROT (husavdrag — private customers only)

- Lifecycle state machine: **pending → customer paid → Skatteverket paid**.
  RUT/ROT fields/buttons only appear for income carrying a husavdrag amount.
- Track the date Skatteverket pays separately from the customer payment date
  (they land in different months in the books).
- RUT cap is per customer per year and shared with ROT — make the cap a CONFIG value,
  not hardcoded (rules change). Warn as a customer approaches the cap.
- RUT/ROT requires the customer's personnummer (so storing it is necessary & legitimate).
- **Per-line RUT/ROT + household split (2026-06, schema v10).** Each invoice article
  line is marked `reduction_type` = RUT / ROT / none; the eligible lines' **labour cost
  INCL moms × the config percentage** (`rut_reduction_pct` 50 %, `rot_reduction_pct`
  30 %) form **two separate pots** (the whole eligible line counts as labour — material
  goes on its own non-eligible lines). **Recipients** are household members who split
  each pot by a **share %** (cumulative öre-exact rounding); per-person RUT and ROT
  amounts are frozen on the invoice (`rut_recipient.rut_amount_ore`/`rot_amount_ore`/
  `share_pct_centi`). A recipient is a **customer** (`rut_recipient.customer_id`) linked
  to the invoice customer via a symmetric **`customer_relation`** (household) — auto-
  created on invoice issue, manageable in the Kunder tab, and the recipient's
  personnummer is saved onto their customer record. The booking treats
  `husavdrag = rut_total + rot_total` as the 1513 receivable (customer pays
  inc − husavdrag); the RUT/ROT lifecycle (register_payment + Skatteverket) is unchanged.
  Customers need only first+last name; personnummer is added when used as a recipient.

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
      SUBLEDGER (schema v7, 2026-06): `invoice_event` (payment|refund|credit) — an
      invoice's outstanding/state are derived from it, so settlements can be PARTIAL.
      `pay_invoice`/`refund_invoice`/`credit_invoice(amount=None)`: fakturametod
      moves cash against 1510; kontantmetod recognises income+moms PROPORTIONALLY per
      payment (per-rate slices, cumulative öre-exact rounding) via hidden report-
      clones (`fakturabetalning`/`kreditering` notes). A credit on a paid invoice
      makes 1510 negative (owed back) → refund pays it out. RUT invoices keep the full
      register_payment + Skatteverket flow (subledger guarded). API POST
      /invoices/{id}/pay|refund|credit; Fakturor UI shows "Kvar" + Delbetald + the
      pay/refund/credit actions.
      CREDIT NOTE (schema v8, 2026-06): a numbered **kreditfaktura** document. Every
      credit event reserves a number from the SAME faktura series
      (`_next_invoice_number` = max of `invoice.invoice_number` and
      `invoice_event.credit_note_number`, +1 → unbroken across both document kinds);
      stored on `invoice_event.credit_note_number`. `get_credit_note(invoice_id,
      event_id)` builds a render dict reusing the original's frozen buyer/seller
      snapshots, with the credited slice as NEGATIVE lines and `credit_of` = the
      original number. `render_invoice_pdf` renders it in credit-note mode
      (KREDITFAKTURA title, "Avser faktura" reference, "Att återfå"). API GET
      /invoices/{id}/credit-notes/{event_id}/pdf; `list_invoices` exposes
      `credit_notes[]` and Fakturor UI shows a "Kreditnota N" download per credit.
- [x] **Per-line invoice categories + split booking, BAS-konto view, structured
      customer address** (schema v9, 2026-06). Each **invoice article line** now carries
      its own income category (`invoice_line.category_id`); booking splits the income
      across those categories' BAS-konton. The split is carried on
      `moms_line.category_id` (NULL → the transaktion's category, so plain
      income/expense is unchanged) and threaded through every booking path:
      register_payment, `_book_invoice_issue` (fakturametod), kontant recognition
      (`_recognition_slice` now per line; `_income_splits`/`_group_income`/`_group_moms`
      helpers), credit/kreditnota, and `_clone_transaktion_for_report`. The **result
      report** groups by `COALESCE(moms_line.category_id, transaktion.category_id)` so a
      multi-category invoice splits across konton (momsdeklaration unchanged — moms is
      per-rate). `create_invoice.category_id` is now an optional per-invoice fallback.
      **Categories carry a `default_rate_code`** (e.g. "Försäljning IT-tjänster, 25 %");
      the line editor pre-fills moms from it. The UI tab "Kategorier" became
      **"BAS-konton"**: it lists the user categories *and* the engine's system konton
      (bank/moms/fordringar) via `GET /accounts` + `BookOps.system_accounts()`.
      **Customers gained a structured address** (street/zip_code/city/country, country
      defaults to Sverige); the legacy single-line `address` is composed from the parts
      and the faktura PDF renders them as separate lines. Invoice creation in the web UI
      is article-by-article with a per-line category picker. **httpx → httpx2** (the
      starlette TestClient dep; deprecation warning gone). All tests pass (237).
- [x] **Delete/edit unused BAS-konton + activate toggle** (2026-06). A category that
      has not yet touched the books (no `transaktion`/`moms_line`/`invoice_line` points
      at it) can be deleted; `BookOps.delete_category` refuses (`InvalidState`→409) a
      used one (inactivate instead — legal traceability) and cleans up the orphaned,
      never-posted, non-system `account` row. `category_in_use` + a `used` flag on
      `GET /categories` drive the UI; `DELETE /categories/{id}`. The BAS-konton tab now
      shows Aktiv/Inaktiv status with Ändra / Inaktivera / Ta bort (delete only when
      unused); editing a *used* konto no longer lets its BAS number change (it would
      retroactively remap booked entries in the reports). Remove-book button also added
      (forget-from-list vs. permanent file deletion behind a typed confirmation;
      `DELETE /books/{id}?delete_files=`).
- [x] **Per-line RUT/ROT + household recipients** (schema v10, 2026-06). Each invoice
      line is marked RUT/ROT/none; eligible lines' labour-incl-moms × config pct
      (`rut_reduction_pct` 50, `rot_reduction_pct` 30) form two separate pots that
      recipients split by share % (öre-exact). Recipients are customers linked via a
      symmetric `customer_relation` (household) — auto-linked on issue, managed in the
      Kunder tab ("Hushåll"), personnummer saved onto the member. `rut_recipient` gained
      `customer_id`/`share_pct_centi`/`rot_amount_ore`; `invoice.rot_total_ore`,
      `invoice_line.reduction_type`. Booking uses husavdrag = rut+rot on 1513. PDF shows
      RUT+ROT pots, per-recipient RUT/ROT boxes, RUT/ROT-tagged lines. API:
      `/customers/{id}/relations` (link/list/unlink), `/reduction-config`. UI: per-line
      RUT/ROT selector + recipient editor with the invoice customer / linked members /
      "+ Ny person", live per-recipient kr from the pots. All tests pass (249).
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
