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
- **Separate RUT/ROT shares + per-recipient cap (schema v11, 2026-06).** A recipient
  can take a **different % of RUT vs ROT** (`rut_recipient.share_pct_centi` = RUT share,
  `rot_share_pct_centi` = ROT share; each pot split by its own share sequence, validated
  independently only when that pot > 0). `husavdrag_cap_status(customer_id, year)` sums a
  person's RUT+ROT across the year's non-cancelled invoice recipients vs the config
  per-person cap (75 000 kr); `create_invoice` returns non-blocking **`cap_warnings`**
  for over/near-cap recipients (Skatteverket reduces the real payout — we only flag).
  API `GET /customers/{id}/husavdrag-cap/{year}`. UI: per-recipient RUT % and ROT %
  fields with live "kvar i år" cap notes (red when over), and a toast on issue.

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
2. **Simplified deklaration** building block (NE-blankett direction). DONE as the
   **Förenklat årsbokslut (SKV 2150)** tab — see build log.
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
      RUT/ROT caps are editable config (VERIFIED 2026-06: combined RUT+ROT
      75 000 kr/person/year AND a ROT-only sub-cap of 50 000 kr/person/year —
      `rut_rot_cap_ore_per_customer_year` + `rot_cap_ore_per_customer_year`).
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
      (pending/paid/cancelled/credited; **`awaiting_rut`** when a RUT/ROT invoice's
      customer part is paid but Skatteverket has not yet paid the husavdrag — i.e. the
      linked `rut_claim` is `customer_paid`, not `skatteverket_paid`; UI pill "Inväntar
      RUT/ROT"); booking itself happens at payment
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
- [x] **Separate RUT/ROT shares + per-recipient annual cap (schema v11, 2026-06).** A
      recipient can take a different % of RUT vs ROT (`rot_share_pct_centi`; each pot
      split by its own share sequence, validated per-pot). `husavdrag_cap_status` +
      `create_invoice` `cap_warnings` (non-blocking) flag a recipient over/near the
      75 000 kr/person/year cap; `GET /customers/{id}/husavdrag-cap/{year}`. UI: RUT %/
      ROT % per recipient + live "kvar i år" cap notes + toast on issue. Tests pass (259).
- [x] **ROT sub-cap (schema v12, 2026-06).** Husavdrag has TWO per-person/year limits:
      the combined RUT+ROT cap (75 000 kr) AND a lower ROT-only sub-cap (50 000 kr,
      config `rot_cap_ore_per_customer_year`). `husavdrag_cap_status` now reports both
      (`rot_used_ore`/`rot_cap_ore`/`rot_over_cap`/`rot_near_cap`) so a ROT-only breach
      is flagged even when the combined total is under 75 000; `create_invoice`
      `cap_warnings` + the recipient editor's "kvar i år" note + the issue toast cover
      both caps. Tests pass (260).
- [x] **Invoice drafts + recipient personnummer prefill (schema v13, 2026-06).** An
      unissued faktura can be saved and continued later: `invoice_draft` stores the whole
      form payload **encrypted** (DEK; it may carry recipient personnummer) with NO
      number and nothing booked. `save_draft`/`list_drafts`/`get_draft`/`delete_draft`;
      API `GET/POST/PUT/DELETE /invoice-drafts[/{id}]`. UI: "Spara utkast" on the faktura
      form, an "Utkast" list (Fortsätt/Ta bort) in the Fakturor tab, prefill the editor
      (lines + recipients) from a draft, and drop the draft when the invoice is issued.
      Also: the recipient editor **prefills personnummer from the picked customer's
      kundkort** when it is already stored (backend `_resolve_recipient` already falls
      back to the customer's pnr). Tests pass (262).
- [x] **Article catalog (schema v14, 2026-06).** Reusable products/services for invoice
      lines. `article` table: number **xxxx-xxxx** (user picks the 4-digit prefix, suffix
      random + unique via `_next_article_number`), description, unit, default
      `unit_price_ore`, `rate_code`, `reduction_type`, `category_id` (NULL =
      uncategorised), `active`. `create_article`/`list_articles`/`update_article`
      (categorise/reprice/rename in the list)/`delete_article` (issued invoice lines keep
      their frozen values; `invoice_line.article_id` link is nulled on delete). API
      `GET/POST/PATCH/DELETE /articles[/{id}]`. UI: an **"Artiklar"** tab (list +
      create/categorise/delete) and, in the faktura line editor, an **article picker**
      that prefills the whole row (price always editable) plus a **★ "spara som artikel"**
      that prompts for the prefix. Tests pass (266).
- [x] **Article-price parse fix + search + invoice-from-customer (frontend, 2026-06).**
      `toOre` now strips grouping spaces (regular + non-breaking) before parsing, so a
      price shown as "1 438,40" no longer round-trips to ~1,4 when saved/reused as an
      article. Added a reusable `searchTable` (free-text filter over a list) and a
      `searchableSelect` (filter input over a `<select>`): the **Kunder** tab and
      **Fakturor** tab each gained a search box (Fakturor also shows a **Kund** column,
      searchable by customer name/nr/date), and the faktura form's customer picker is now
      filterable. The Kunder tab has a **"Ny faktura"** button per customer that jumps to
      Fakturor and opens a fresh invoice form with that customer preselected (via
      `state.pendingInvoiceCustomer` → `newInvoiceForCustomer`). Frontend-only; all 266
      tests pass, live browser smoke-tested.
- [x] **Import migrates older `.buyn` bundles forward (2026-06).** `bundle.import_` now
      accepts a bundle whose `schema_version` is *older* than the app: after restoring the
      db it runs `schema.migrate()` to bring it up to `SCHEMA_VERSION` (migrations are pure
      DDL, so they run on the restored file without the passphrase/DEK), then the book opens
      normally with its original passphrase. A *newer* bundle is still refused. The result
      dict gains `schema_version`. Lets a book backed up on an older build restore on a
      newer one without manual surgery.
- [x] **Skatteverket husavdrag payout: manual amount + rounding + partial payout
      (schema v15, 2026-06).** `register_rut_skatteverket_payment` now takes the amount
      Skatteverket *actually* paid (`received_ore`, defaults to the claimed husavdrag) and
      interprets the difference vs the claim: within ±`rut_skv_rounding_tolerance_ore`
      (config, 0,49 kr) it is **rounding** → the diff books to **3740 Öres- och
      kronutjämning** (config `account_ores_kronutjamning`) so 1513 clears exactly; a larger
      underpayment is a **partial payout** (quota/cap-driven) → the remainder is reclassified
      **1513 → 1510** (now owed by the customer) and a linked **follow-up invoice** documents
      it. The follow-up carries **no moms** (income+moms were fully booked at the original
      sale — there is a single moms on the full labour price; Skatteverket's payment is not a
      taxable supply), is numbered from the **same unbroken faktura series**, links to the
      original via `invoice.parent_invoice_id` + an editable `relation_note`, and is settled
      via `pay_invoice` (bank ← 1510, `husavdrag_shortfall_ore` marks it so no income is
      re-recognised; credit/refund refused). `mode` (auto|rounding|partial) lets the user
      confirm/override — a >tolerance underpayment **requires** an explicit `partial` so a
      quota shortfall is never silently swallowed as rounding. `skatteverket_payment_preview`
      + API `POST …/rut/{id}/skatteverket-preview` classify the amount (exact|rounding|
      partial|overpaid) so the UI can confirm before booking; `rut_claim` gained
      `skatteverket_received_ore`/`shortfall_invoice_id`. UI `rutSkvPayFlow`: amount field →
      preview → 3740 rounding note or a partial-confirmation dialog with editable reference
      text (creates + downloads the follow-up). Every verifikation balances. Tests pass (276).
- [x] **Per-line percentage discount (rabatt) on invoice lines (schema v16, 2026-06).**
      Each invoice article line carries a `discount_pct_centi` (% × 100, e.g. 15 % → 1500);
      `create_invoice` applies it to the line total ex moms BEFORE moms so moms + the
      RUT/ROT husavdrag pots follow the discounted amount (validated 0–100 %). The list
      à-pris (`unit_price_ore`) is kept unchanged; only `ex_moms_ore` is stored discounted.
      `get_invoice` exposes it and the faktura PDF annotates the line "(−15 % rabatt)"
      (à-pris stays list price, Belopp is discounted). API `InvoiceLineReq.discount_pct_centi`
      (the Pydantic model must carry it or it is silently dropped). UI: a "% rabatt" input
      per row in the line editor (feeds the live RUT/ROT pot preview). Tests pass (280);
      browser-smoke-tested end-to-end through the faktura form.
- [x] **Searchable RUT/ROT recipient picker (frontend, 2026-06).** The faktura form's
      top **Kund** picker was already a `searchableSelect`; the **RUT/ROT recipient**
      person picker (`recipientsEditor.peopleOptions`) now is too — a "Sök person…" filter
      over the invoice customer + linked household members, kept in sync as rows/relations
      reload (`row._whoWrap` tracks the wrapper for `replaceWith`). Frontend-only;
      browser-smoke-tested (both pickers) and web tests pass.
- [x] **Huvudbok/grundbok preview + manual journal entry (2026-06).** A new **"Huvudbok"**
      tab previews the bookkeeping: **grundbok** (`GET /verifikationer-full` →
      `verifikationer_full`, each verifikation with its konteringar) and **huvudbok**
      (`GET /huvudbok` → `huvudbok`, per BAS-konto with running saldo + debit/credit sums),
      both with an optional ver_date range. **Manual verifikationer** can be posted directly
      from the page (`POST /verifikationer/manual` → `add_manual_verifikation`), independent
      of invoices/transaktioner — for correcting something by hand (e.g. after a code bug).
      A manual entry gets the next unbroken number, must balance (debet = kredit, validated
      → 400), respects period locks (→ 409), auto-creates unknown konton (prompting for a
      name), and is immutable once posted (rätta with a rättelse). The UI (`manualVerForm`)
      is a debit/credit row editor with a live balance indicator; the API takes debit/credit
      columns and stores signed amounts. Tests pass (283); browser-smoke-tested (both views +
      posting a balanced manual verifikation).
- [x] **Förenklat årsbokslut (SKV 2150) tab (2026-06).** A new **"Årsbokslut"** tab renders
      the enskild-näringsidkare simplified year-end form: the ledger's BAS-konto balances
      are mapped into the blankett's boxes — balansräkning **B1–B16**, resultaträkning
      **R1–R11**, and **U1–U4** upplysningar. `backend/reports/arsbokslut.py`
      (`forenklat_arsbokslut`) reads the raw `posting` table (so it captures invoices,
      manual verifikationer, moms and rättelser alike): balansräkning uses the CUMULATIVE
      saldo up to the fiscal-year end, resultaträkning the year's movement. Sign-aware
      (assets/costs debit-positive; equity/liabilities/income credit-positive); moms
      accounts (2600–2669) are netted and placed by sign (skuld→B14 / fordran→B8); **B10
      eget kapital includes årets resultat (R11)** so the two summa boxes reconcile exactly
      (`balanserar`, guaranteed because every verifikation balances). Each box lists its
      contributing konton (hover tooltip) for transparency — it's a **help/preview**, not
      filed. API `GET /reports/arsbokslut?start=&end=`. The BAS→box ranges follow the
      standard kopplingstabell (4xxx→R5, 5xxx–6xxx→R6, 7xxx→R7, 78xx→R9/R10, 8xxx→R4/R8;
      1xxx assets, 2xxx equity/skuld). Tests pass (284); browser-smoke-tested.
- [x] **RUT/ROT payout: begäran-referens + Skatteverket-kvittens (schema v17, 2026-06).**
      When booking the Skatteverket husavdrag payout the user now enters a **reference**
      (the RUT/ROT begäran name, e.g. "RUT1" — stored on `rut_claim.skatteverket_reference`
      and appended to the verifikation text) and can **upload Skatteverket's kvittens**
      (image/PDF), stored **DEK-encrypted** like any receipt. `receipt` gained a nullable
      `rut_claim_id` tag: `attach_rut_receipt` files the kvittens under the claim's sale
      transaktion (so it travels in the `.buyn` bundle) but `list_receipts` excludes it, and
      `list_rut_receipts` lists it. API: `register_rut_skatteverket_payment` takes
      `reference`; `POST /rut/{id}/receipt` + `GET /rut/{id}/receipts`; `rut-claims` exposes
      the reference. UI: `rutSkvPayFlow` gained a reference field + a kvittens file input
      (modal() now supports `type:"file"` returning the File); the RUT tab shows the Begäran
      column + a "📎 Kvittens" view/upload (`rutKvittensFlow`). Tests pass (285);
      browser-smoke-tested (reference + PDF kvittens through the real modal).
- [x] **Edit/delete payment methods in Inställningar (2026-06).** The Betalsätt list was
      read-only; each row now has **Ändra** (edit name + number/länk), **Aktivera/Inaktivera**,
      and **Ta bort**. Backend already had `update_payment_method`; added
      `delete_payment_method` + `DELETE /payment-methods/{id}` (safe — issued invoices carry
      their own frozen payment-method snapshot, so editing/removing never changes an existing
      faktura). Tests pass (286); browser-smoke-tested editing a betalsätt's name + number.
- [x] **Gratis distanssupport — support-time bank (schema v18, 2026-06).** Each invoice
      earns free remote-support time: **15 min per full 500 kr of the invoice total**
      (round down; remainder under 500 kr earns nothing — 1 249 kr → 30 min), valid **36
      months** from the invoice date. `create_invoice` computes + stores
      `invoice.support_minutes_earned` + `support_expiry_date` (`support_minutes_earned()`
      + `_add_months()` helpers; husavdrag follow-up invoices earn 0). **Per-customer
      balance** `support_balance(customer_id)` = Σ earned from that customer's still-valid
      invoices − net used, where used = deductions − additions from a new **`support_ledger`**
      table (kind deduction|addition, minutes, note, timestamp). `record_support_entry` +
      `list_support_ledger`. API `GET/POST /customers/{id}/support`. The faktura **PDF**
      renders the fixed magIT support text block at the bottom (`_support_text`, with the
      earned minutes + expiry substituted; skipped when no expiry, i.e. follow-ups). UI: a
      **"Support"** button per customer opens the support profile — remaining time, three
      quick-deduct buttons (15/30/60), a free-value add field + note, the full history log,
      and a per-invoice earned breakdown. Migration backfills existing (non-follow-up)
      invoices. Tests pass (290); browser-smoke-tested (deduct + add + history).
- [x] **Per-line rabatt shown on the faktura PDF (2026-07).** A discounted invoice line
      now renders a red sub-line "Rabatt N % (ord. X) −Y kr" (ordinary line total,
      discount %, and rabatt amount); the à-pris stays the list price and Belopp is the
      net. A red **"Total rabatt"** row appears in the summary (before moms), only when at
      least one line carries a discount. Display-only — booking unchanged (`invoices/
      pdf.py`).
- [x] **Öresavrundning enligt avrundningslagen (2026-07).** A faktura's *summa att betala*
      is rounded to whole kronor (1–49 öre down, 50–99 up; `_round_to_krona`). Per
      Skatteverkets ställningstagande the avrundning may **never** touch the
      beskattningsunderlag or momsbeloppet — those stay exact; the öre difference is booked
      to **3740 Öres- och kronutjämning** (config `account_ores_kronutjamning`), matching
      Skatteverket's own example (527 kr ex → 1510/1930 659, 3001 −527, 2610 −131,75, 3740
      −0,25). **Kontantmetod** rounds at payment (bank = whole kronor, income+moms exact,
      3740 = diff — `_book_kontant_recognition(..., ores_ore=)` shaves the öre; applied when
      a payment closes the invoice, so partials stay exact). **Fakturametod** books the
      kundfordran incl. öre at issue and realises the öresutjämning at payment (same end
      state). **RUT/ROT**: only the *kundens del* (inc − husavdrag) is rounded; 1513
      Skatteverkets del stays exact and the register_payment/SKV-payout flow is otherwise
      unchanged. Öresavrundning applies to **fakturor only** — plain (non-invoice) incomes
      book exact. The faktura PDF shows an **"Öresavrundning"** row + the whole-krona *Att
      betala* (`_pay_block`). Every verifikation balances. Tests pass (297).
- [x] **Skatt-flik: uppskattad årsskatt till Skatteverket (schema v20, 2026-07).** A
      **"Skatt"** tab estimates, for a fiscal year, what an **enskild näringsidkare** should
      set aside for Skatteverket: **moms** (net from the momsdeklaration), **egenavgifter**
      and **inkomstskatt**. `reports/tax.py` (`tax_estimate`) reads the year's moms + the
      **årets resultat** (förenklat årsbokslut's `arets_resultat_ore`, off the raw posting
      table). Model (verified against Skatteverkets "Räkna ut din skatt" for 2026 to within
      a few kronor across three reference calcs): **egenavgifter** = 28,97 % of the överskott
      − generell nedsättning (7,5 %, max 15 000 kr), **only on the firma** (never on salary);
      **income tax** = grundavdrag (piecewise in prisbasbelopp, rounded up) → kommunal +
      statlig skatt + begravnings-/public service-avgift − **jobbskatteavdrag** − skatte-
      reduktion för förvärvsinkomst, on the TOTAL förvärvsinkomst. Grundavdrag +
      jobbskatteavdrag use the EXACT 2026 formulas from regeringens Beräkningskonventioner
      2026 (Tabell 2.2 + 2.10; PDF in `docs/2026/`) — reproduces SKV's three reference calcs
      to the krona (30 616 / 138 481 / 87 628). No high-income jobbskatteavdrag phase-out in
      the 2026 construction. The **jobbskatteavdrag coefficients themselves are editable
      config** (`jsa_break1/2/3_centi`, `jsa_c2/c3_centi`, `jsa_b3_base/b4_level_centi`;
      fraction × 10000) alongside a `tax_values_year` label — the stored values (2026
      defaults) stay in effect until changed, so the estimate never requires a yearly update;
      a future year's Tabell 2.10 can be entered without a code change (schema v21).
      **Employment salary is an input** (`ovrig_forvarvsinkomst_ore`): the
      firma's own income-tax liability is the **marginal** amount its income adds on top of
      the salary (`income_tax(överskott+lön) − income_tax(lön)`), so the salary correctly
      pushes the firma income into higher brackets / statlig skatt. **Every annual figure is
      editable config** (prisbasbelopp, skiktgräns, kommunalskatt, begravning, egenavgifts-
      nedsättning, public service, skattereduktioner …; 2026 defaults, migration 20 seeds
      existing books) — update yearly from Skatteverkets "Belopp och procent" + regeringens
      "Beräkningskonventioner" (SKV 152 was discontinued 2015). `BookOps.tax_estimate/
      get_tax_config/set_tax_config`; API `GET /reports/tax`, `GET/PUT /tax-config`. UI shows
      "att sätta undan (firman)", a per-tax breakdown, a total-överblick (firma + lön) when a
      salary is entered, and a collapsible rate editor. A **hjälpmedel/uppskattning**, not a
      deklaration (verify against Skatteverket; high-income jobbskatteavdrag phase-out + AB
      are possible follow-ups). Tests pass (302); browser-smoke-tested (salary + marginal
      split).
- [x] **Inköp-tabb + Kundfakturor-omdöp + live-fakturatotal + RUT-worklist/nummer
      (schema v22, 2026-07).** (1) New **"Inköp"** tab: record purchases/expenses to the
      firma with a **kvitto-/fakturanummer** (`transaktion.ext_ref`), an attached receipt,
      supplier + BAS-category + multi-rate moms lines, and paid-now **or** as an incoming
      **leverantörsfaktura** booked pending and marked paid later (`register_payment`).
      `record_expense(ext_ref=)`; the transaktioner listing exposes `ext_ref` + `amount_ore`
      (Σ inc_moms). UI reuses momsLinesEditor/receiptPicker/payFlow/receiptsFlow.
      (2) The **"Fakturor"** tab is renamed **"Kundfakturor"**. (3) The invoice builder shows
      a **live "Summering (preliminär)"** (rabatt/ex-moms/moms/husavdrag → att betala, whole
      krona) updated per line change — display only, drafts unaffected. (4) RUT tab gains an
      **"Inväntar husavdrag från Skatteverket"** worklist (invoices in state `awaiting_rut`)
      + `next_rut_reference()` which auto-suggests the next begäran ref by continuing the
      numeric sequence of booked references (own sequence, independent of faktura numbers /
      makulering — last "RUT4" → "RUT5"; API `GET /rut-next-reference`; prefilled + editable
      in the Skatteverket-payment flow). Tests pass (305); browser-smoke-tested.
- [x] **Offert (quote) från utkast (schema v23, 2026-07).** A numbered **offert** document
      that **books nothing** (no ledger/moms impact), with its own sequential
      `offert_number` series. Created from a saved **utkast** (the draft is KEPT, not
      consumed) or straight from the faktura-form's current payload. `create_offert`/
      `create_offert_from_draft`/`list_offerter`/`get_offert` + `_offert_figures` (standalone
      line/moms/RUT-pot computation mirroring create_invoice so the booking path is
      untouched); the full render snapshot (buyer incl. pnr, seller, lines, recipients,
      totals) is stored **DEK-encrypted** in the `offert` table. The PDF reuses
      `render_invoice_pdf` in an **offert mode** (title OFFERT, Offertnr/Offertdatum/Giltig
      till, "Uppskattat pris", a "detta är en offert"-disclaimer; no payment methods / support
      text). API `GET/POST /offerter`, `GET /offerter/{id}/pdf`. UI: "Skapa offert" on each
      draft row **and** on the faktura form (keeps the draft, opens the PDF) + an "Offerter"
      list in Kundfakturor. Tests pass (307); browser-smoke-tested (offert from draft + PDF).
- [x] **Skapa faktura från offert (schema v24, 2026-07).** An accepted offert can be turned
      into a real faktura: `create_invoice_from_offert` reconstructs the invoice inputs from
      the offert's snapshot (lines incl. per-line category/rabatt/RUT-ROT + recipients incl.
      pnr) and calls `create_invoice` (booking + numbering unchanged), then links
      `offert.invoice_id`. **Guarded to once** (`InvalidState`→409 if already invoiced). API
      `POST /offerter/{id}/create-invoice` (optional invoice/förfallodatum). UI: a "Skapa
      faktura" button per non-converted offert (date modal → issues the faktura + opens its
      PDF); a converted offert shows a "Faktura N" pill instead. Tests pass (308);
      browser-smoke-tested.
- [x] **"Ordrar"-tabb (Kundfakturor omdöpt) med 3 under-flikar + kundfilter + kund-
      spenderat (2026-07).** The "Kundfakturor" tab is renamed **"Ordrar"** and split into
      three sub-tabs — **Fakturor / Offerter / Utkast** (with counts) — driven by
      `state.ordersTab`/`state.ordersCustomer` (persist across re-renders). A **"Filtrera på
      kund"** dropdown filters the active sub-tab to one customer (default: all, sequential);
      the Fakturor sub-tab keeps its text search. Frontend-only restructure of the invoices
      renderer (drawFakturor/drawOfferter/drawUtkast + renderContent). The **Kunder** tab
      gains a **"Spenderat"** column: `GET /customers` now returns `invoiced_ore` = Σ
      inc_moms of the customer's non-makulerade invoices. Tests pass (309); browser-smoke-
      tested (sub-tabs, filter persists across tabs, spenderat totals). FUTURE (noted, not
      built): import supplier invoices → suggest creating articles from the lines → book the
      cost so order margins (what the customer later buys for builds) can be derived.
- [x] **Distribution + in-app updates via GitHub Releases (2026-07).** `.github/workflows/
      release.yml` builds the Windows one-folder app (`bokyup.spec`) + the external updater
      (`updater.spec` → `BokYupUpdater.exe`) on a `v*` tag, zips them, and builds the Android
      APK (Capacitor), attaching all to a GitHub Release (version stamped from the tag).
      `backend/updater.py`: `check_for_update` (GitHub `releases/latest`, pure
      `evaluate_release`/`is_newer` + stdlib fetch; soft-fails offline) and `apply_update`
      (frozen desktop only). **No auto-install** — the home screen checks (optionally on
      startup; a checkbox stores the pref in localStorage) and shows a banner; the user
      clicks **"Uppdatera nu"** → `POST /update-apply` downloads the zip, launches a temp
      copy of `BokYupUpdater.exe` (`packaging/updater.py`: waits for the app PID to exit,
      backs up the install to `.bak`, `swap_zip` extracts the new build, relaunches) and
      exits. Books live outside the install dir so updates never touch them; `schema.migrate`
      handles a schema bump. API `GET /update-check` + `POST /update-apply`. **Android: use
      Obtainium** now (tracks the releases; APK named `bokyup-<version>.apk`, signed via repo
      keystore secrets); **Capgo OTA** for web/WASM updates noted for later (`docs/updates.md`).
      Tests pass (323; version-compare/release-eval/swap_zip/verify_sha256).
- [x] **Import: överskriv befintlig bok med samma namn eller skapa ny (2026-07).** When
      restoring a `.buyn`, if a book with the same display name already exists the UI now
      asks: **skapa ny** (keep both) or **skriv över** the existing one. Frontend
      orchestration (`resolveImportConflict`): overwrite targets the existing book's
      `db_path` with `overwrite: true` — `register_existing` dedups by path (same registry
      record, no duplicate) and `bundle.import_` auto-exports a timestamped `.buyn` backup
      of the old book before replacing it. Backend already supported it (import `overwrite`
      param). Tests pass (324; `test_import_overwrite_existing_book_in_place`).
- [x] **App-ikon (2026-07).** A green ledger-with-tick icon (user-supplied SVG). Sources
      `packaging/icon/bokyup-icon.svg` (+ `bokyup-foreground.svg` padded for the Android
      adaptive safe zone); `packaging/icon/generate.py` rasterises (Playwright) to the
      committed assets: `bokyup.ico` (wired in `bokyup.spec`), `backend/api/static/icons/*`
      + `manifest.webmanifest` (linked in `index.html`, theme-color #A9D3B7), and
      `phone/assets/icon-only|foreground|background.png` → `@capacitor/assets generate
      --android` in the release workflow. This is the app/brand icon, separate from the
      per-book company logo on invoices.
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
