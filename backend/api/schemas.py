"""
schemas.py — Pydantic request models for the API (Layer 7).

Money is integer ören everywhere (matching the backend); the UI formats kronor.
Responses are returned as plain dicts from the backend operations, so only request
bodies need models here.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


# ----- books / registry ---------------------------------------------------

class CreateBookReq(BaseModel):
    display_name: str
    db_path: str
    passphrase: str


class UnlockReq(BaseModel):
    passphrase: str


class RecoveryUnlockReq(BaseModel):
    recovery_key: str


class RenameReq(BaseModel):
    display_name: str


class ChangePassphraseReq(BaseModel):
    old_passphrase: str
    new_passphrase: str


class RecoveryKeyReq(BaseModel):
    passphrase: str
    recovery_key: Optional[str] = None    # None => generate a strong random one


class ExportReq(BaseModel):
    out_path: str


class ImportReq(BaseModel):
    bundle_path: str
    dest_db_path: str
    display_name: Optional[str] = None
    overwrite: bool = False


# ----- reference data -----------------------------------------------------

class CategoryReq(BaseModel):
    name: str
    kind: Optional[str] = None    # 'income' | 'expense' (inherited from parent for a subcategory)
    bas_konto: Optional[int] = None   # inherited from parent when a parent_id is given
    default_rate_code: Optional[str] = None   # default moms for lines on this category
    account_name: Optional[str] = None
    prefix: Optional[str] = None  # unique 4-digit article-number prefix (auto if omitted)
    parent_id: Optional[int] = None   # makes this a subcategory of parent_id


class BasCatalogAddReq(BaseModel):
    """BAS-konton to add from the preset kontoplan (income/expense become categories,
    balance-sheet konton are added to the chart only)."""
    konton: list[int] = []


class CategoryUpdateReq(BaseModel):
    name: Optional[str] = None
    bas_konto: Optional[int] = None
    default_rate_code: Optional[str] = None
    active: Optional[bool] = None
    account_name: Optional[str] = None
    prefix: Optional[str] = None
    parent_id: Optional[int] = None   # reparent (0 = make top-level)


class CustomerReq(BaseModel):
    type: str                     # 'private' | 'business'
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    personnummer: Optional[str] = None
    company_name: Optional[str] = None
    org_nr: Optional[str] = None
    contact_person: Optional[str] = None
    vat_nr: Optional[str] = None
    address: Optional[str] = None              # legacy single-line (composed from parts)
    shipping_address: Optional[str] = None     # leveransadress (if different)
    street: Optional[str] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None              # defaults to Sverige server-side
    email: Optional[str] = None
    phone: Optional[str] = None


class CustomerUpdateReq(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    personnummer: Optional[str] = None
    company_name: Optional[str] = None
    org_nr: Optional[str] = None
    contact_person: Optional[str] = None
    vat_nr: Optional[str] = None
    address: Optional[str] = None
    shipping_address: Optional[str] = None
    street: Optional[str] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    active: Optional[bool] = None


class SupplierReq(BaseModel):
    name: str
    default_moms_rate: str = "25"
    org_nr: Optional[str] = None
    address: Optional[str] = None


class SupplierUpdateReq(BaseModel):
    name: Optional[str] = None
    default_moms_rate: Optional[str] = None
    org_nr: Optional[str] = None
    address: Optional[str] = None
    active: Optional[bool] = None


# ----- bookkeeping --------------------------------------------------------

class MomsLineReq(BaseModel):
    rate_code: str
    amount_ore: int
    inclusive: bool = True
    # Konto for THIS line, overriding the entry's category. None = use the entry's, so a
    # single-konto post is unchanged. Must be the same kind (income/expense) as the entry.
    category_id: Optional[int] = None
    # Omvänd betalningsskyldighet (purchases): 'eu_vara' | 'eu_tjanst' | 'utanfor_eu' |
    # 'sv_vara' | 'sv_tjanst'. None = normal moms. The amount is then always the
    # beskattningsunderlag (the seller invoiced without moms).
    reverse_charge: Optional[str] = None


class RecurringLineReq(MomsLineReq):
    """Same shape as a moms line; `category_id` (from the base) is the per-line konto."""


class RecurringReq(BaseModel):
    kind: str = "expense"                  # 'expense' | 'income'
    name: str
    category_id: int
    lines: list[RecurringLineReq]
    start_date: str                        # the first occurrence to confirm
    interval_unit: str = "month"           # 'month' | 'year'
    interval_count: int = 1
    supplier_id: Optional[int] = None
    customer_id: Optional[int] = None
    note: Optional[str] = None
    ext_ref: Optional[str] = None
    paid_account: str = "bank"             # 'bank' (1930) | 'privat' (2018)
    end_date: Optional[str] = None


class RecurringUpdateReq(BaseModel):
    """Any field given changes the series GOING FORWARD (already-confirmed
    occurrences are booked verifikationer and never move)."""
    name: Optional[str] = None
    category_id: Optional[int] = None
    lines: Optional[list[RecurringLineReq]] = None
    supplier_id: Optional[int] = None
    customer_id: Optional[int] = None
    note: Optional[str] = None
    ext_ref: Optional[str] = None
    paid_account: Optional[str] = None
    interval_unit: Optional[str] = None
    interval_count: Optional[int] = None
    next_date: Optional[str] = None
    end_date: Optional[str] = None
    active: Optional[bool] = None


class RecurringConfirmReq(BaseModel):
    date: Optional[str] = None             # occurrence date (default: the one due)
    lines: Optional[list[RecurringLineReq]] = None   # override THIS time only
    paid_date: Optional[str] = None        # given => booked now; omitted => stays pending
    note: Optional[str] = None
    ext_ref: Optional[str] = None
    paid_account: Optional[str] = None


class RecurringSkipReq(BaseModel):
    date: Optional[str] = None


class ExpenseItemReq(BaseModel):
    # One inköp line: qty × à-cost (ex moms) at a rate. A non-empty `description` makes it
    # a stocked article (find-or-created under its product category) → a batch on booking.
    quantity_centi: int
    unit_cost_ore: int                 # à-pris ex moms per unit
    rate_code: str
    description: Optional[str] = None   # article name (blank = pure cost line, no stock)
    category_id: Optional[int] = None  # the article's product (income) category
    reduction_type: Optional[str] = None
    unit: Optional[str] = None
    to_stock: bool = True
    note: Optional[str] = None
    reverse_charge: Optional[str] = None   # omvänd betalningsskyldighet (see MomsLineReq)
    # Kostnadskonto for THIS line, overriding the purchase's default. None = use the
    # default, so a receipt with one konto behaves exactly as before.
    expense_category_id: Optional[int] = None


class RecordExpenseReq(BaseModel):
    category_id: int
    trans_date: str
    lines: Optional[list[MomsLineReq]] = None     # legacy: raw moms lines
    items: Optional[list[ExpenseItemReq]] = None  # new: article line-items (create batches)
    supplier_id: Optional[int] = None
    note: Optional[str] = None
    receipt_original_format: Optional[str] = None   # 'paper' | 'digital'
    ext_ref: Optional[str] = None                   # supplier's kvitto-/fakturanummer
    ores_rounding: bool = False                     # supplier rounded the total to whole krona
    paid_date: Optional[str] = None
    paid_account: Optional[str] = None              # 'bank' (1930) | 'privat' (2018) when paid now


class ExpenseMetaReq(BaseModel):
    # PATCH an inköp. Metadata (leverantör/kvittonr/note/format/öresavrundning) is always
    # editable. A FULL edit — belopp/konto/moms/datum — is allowed only while the inköp is
    # UNBOOKED, by also sending category_id + trans_date + lines OR items.
    supplier_id: Optional[int] = None
    ext_ref: Optional[str] = None
    note: Optional[str] = None
    receipt_original_format: Optional[str] = None
    ores_rounding: Optional[bool] = None            # supplier rounded to whole krona
    category_id: Optional[int] = None               # full edit: expense BAS-konto
    trans_date: Optional[str] = None                # full edit
    lines: Optional[list[MomsLineReq]] = None       # full edit: raw moms lines
    items: Optional[list[ExpenseItemReq]] = None    # full edit: article line-items


class RecordIncomeReq(BaseModel):
    customer_id: int
    category_id: int
    lines: list[MomsLineReq]
    trans_date: str
    rut_amount_ore: int = 0
    note: Optional[str] = None
    # Kvitto-/fakturanummer for the underlag behind the sale. Without this field
    # Pydantic drops it silently, so the number never reaches record_income.
    ext_ref: Optional[str] = None
    # The customer paid whole kronor: round the cash leg, keep underlag + moms exact.
    ores_rounding: bool = False
    paid_date: Optional[str] = None


class PaymentReq(BaseModel):
    payment_date: str
    # Optional extra momsfri betaltjänstavgift (Klarna/Qliro) booked with the inköp payment.
    extra_fee_ore: Optional[int] = None
    extra_fee_category_id: Optional[int] = None
    note: Optional[str] = None         # reference/comment → verifikation text
    paid_account: Optional[str] = None  # 'bank' (1930) | 'privat' (2018 Egna insättningar)


class SkatteverketPaymentReq(BaseModel):
    payment_date: str
    received_ore: Optional[int] = None          # actual amount Skatteverket paid out
    mode: Optional[str] = None                  # None=auto | 'rounding' | 'partial'
    relation_note: Optional[str] = None         # custom reference text on the follow-up
    reference: Optional[str] = None             # name of the RUT/ROT begäran (e.g. "RUT1")


class SkatteverketPreviewReq(BaseModel):
    received_ore: int


class ReverseReq(BaseModel):
    reason: str
    reg_date: Optional[str] = None


class ManualPostingReq(BaseModel):
    bas_konto: int
    debit_ore: int = 0
    credit_ore: int = 0
    account_name: Optional[str] = None   # name if this konto is not yet in the chart
    text: Optional[str] = None


class ManualVerifikationReq(BaseModel):
    ver_date: str
    text: str
    reg_date: Optional[str] = None
    postings: list[ManualPostingReq]
    # Egenupprättad verifikation (BFL 5 kap.): no external document exists, so the
    # motivation IS the underlag and is required when the flag is set.
    egenupprattad: bool = False
    motivering: Optional[str] = None
    ext_ref: Optional[str] = None          # kvitto-/fakturanummer (as on an inköp)
    kommentar: Optional[str] = None        # free note kept with the entry


class OpeningBalanceRowReq(BaseModel):
    bas_konto: int
    # The konto's balance in its NATURAL direction: an asset (1xxx) positive is a debit
    # balance, equity/a debt (2xxx) positive is a credit balance — exactly as the
    # årsbokslut/NE blankett prints them. Zero rows are dropped.
    amount_ore: int = 0
    account_name: Optional[str] = None


class OpeningBalanceReq(BaseModel):
    date: str                              # first day of the fiscal year
    balances: list[OpeningBalanceRowReq]
    source: Optional[str] = None           # what the figures were copied from
    motivering: Optional[str] = None       # overrides the composed motivation
    kommentar: Optional[str] = None


class AssetPurchaseReq(BaseModel):
    """A tool/inventarie bought BY the firma: normal deductible moms, but the net may go
    to a balance-sheet konto (capitalised + depreciated) instead of a cost konto."""
    description: str                       # e.g. "Lödstation JBC CD-2BE, serienr XYZ"
    amount_ore: int
    trans_date: str
    rate_code: str = "25"
    inclusive: bool = True                 # is amount_ore incl. moms?
    mode: str = "auto"                     # auto | direktavdrag | aktivera
    konto: Optional[int] = None            # override the suggested konto
    useful_life_years: Optional[int] = None  # <=3 years allows a direct deduction
    supplier_id: Optional[int] = None
    ext_ref: Optional[str] = None          # kvitto-/fakturanummer
    note: Optional[str] = None
    receipt_original_format: Optional[str] = None
    ores_rounding: bool = False
    paid_date: Optional[str] = None        # omitted => pending leverantörsfaktura
    paid_account: str = "bank"


class FixedAssetReq(BaseModel):
    """Register an asset that is capitalised and written off over its useful life."""
    description: str
    acquisition_ore: int                   # anskaffningsvärde EXKL. moms
    acquired_date: str
    asset_konto: Optional[int] = None      # default 1220
    useful_life_years: Optional[int] = None   # default from config (5 år)
    note: Optional[str] = None


class FixedAssetUpdateReq(BaseModel):
    description: Optional[str] = None
    useful_life_years: Optional[int] = None
    note: Optional[str] = None
    disposed_date: Optional[str] = None    # sold/scrapped -> stop depreciating


class DepreciationReq(BaseModel):
    fiscal_year_end: str
    # fixed_asset_id -> amount in ören for a year you want written off by another amount
    # (0 skips that asset). Omitted assets take the proposal.
    overrides: Optional[dict[int, int]] = None


class PrivateAssetReq(BaseModel):
    """A privately-owned asset brought into the business (tillskott): debited to the
    cost/asset konto against 2018 Egna insättningar. No moms, no money."""
    description: str                       # e.g. "MacBook Pro 14, serienr XYZ"
    amount_ore: int                        # value at the transfer (market value)
    date: str                              # the day it starts being used in the business
    mode: str = "auto"                     # auto | direktavdrag | aktivera
    business_pct_centi: int = 10000        # share used in the business (100 % = 10000)
    konto: Optional[int] = None            # override the suggested konto
    acquired_date: Optional[str] = None    # when it was bought privately
    acquired_amount_ore: Optional[int] = None
    motivering: Optional[str] = None       # composed from the facts when omitted


class PeriodLockReq(BaseModel):
    period_start: str
    period_end: str
    kind: str = "moms"


class YearEndAccrualReq(BaseModel):
    fiscal_year_end: str          # YYYY-MM-DD (last day of the räkenskapsår)


# ----- invoices (faktura) -------------------------------------------------

class CompanyReq(BaseModel):
    name: Optional[str] = None
    org_nr: Optional[str] = None
    vat_nr: Optional[str] = None
    address: Optional[str] = None
    street: Optional[str] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    f_skatt: Optional[int] = None


class SupportEntryReq(BaseModel):
    minutes: int                  # positive amount
    kind: str                     # 'deduction' | 'addition'
    note: Optional[str] = None


class AccountingMethodReq(BaseModel):
    method: str                   # 'kontantmetod' | 'fakturametod'


class LogoReq(BaseModel):
    image_base64: str             # any common image; normalised to PNG server-side


class PaymentMethodReq(BaseModel):
    label: str                    # "Swish" | "Bankgiro" | "IBAN" | ...
    value: str                    # the number / link
    sort_order: Optional[int] = None   # None = append at the end of the current order


class PaymentMethodReorderReq(BaseModel):
    ordered_ids: list[int]        # payment-method ids in the desired display order


class PayMethodsReq(BaseModel):
    methods: list[str]            # the user's own inköp betalsätt (Qliro, Klarna, kort …)


class PaymentMethodUpdateReq(BaseModel):
    label: Optional[str] = None
    value: Optional[str] = None
    sort_order: Optional[int] = None
    active: Optional[int] = None


class ArticleReq(BaseModel):
    description: str
    prefix: Optional[str] = None  # override; normally the number's prefix comes from the category
    unit_price_ore: int = 0
    rate_code: str = "25"
    reduction_type: Optional[str] = None
    category_id: Optional[int] = None
    unit: Optional[str] = None


class ArticleUpdateReq(BaseModel):
    article_number: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None
    unit_price_ore: Optional[int] = None
    rate_code: Optional[str] = None
    reduction_type: Optional[str] = None
    category_id: Optional[int] = None
    active: Optional[int] = None


class InvoiceLineReq(BaseModel):
    description: str
    quantity_centi: int           # quantity * 100 (1.50 -> 150)
    unit_price_ore: int           # ex moms, per unit
    rate_code: str
    category_id: Optional[int] = None   # income account this line books to
    unit: Optional[str] = None
    reduction_type: Optional[str] = None   # 'rut' | 'rot' | None (husavdrag kind)
    rut_eligible: bool = False             # back-compat: True == reduction_type 'rut'
    article_id: Optional[int] = None       # catalog article this line came from
    discount_pct_centi: int = 0            # per-line % rabatt * 100 (15 % -> 1500)
    stock_batch_id: Optional[int] = None   # picked lager batch (real margin + consumes stock)


class StockBatchReq(BaseModel):
    article_id: int
    qty_centi: int                # quantity bought in * 100
    unit_cost_ore: int            # ex-moms cost per unit
    received_date: Optional[str] = None
    supplier_id: Optional[int] = None
    purchase_transaktion_id: Optional[int] = None
    note: Optional[str] = None


class StockBatchPatchReq(BaseModel):
    # Edit a batch's non-audit fields; a cost edit does not rewrite already-sold lines.
    unit_cost_ore: Optional[int] = None
    qty_remaining_centi: Optional[int] = None    # stock correction
    received_date: Optional[str] = None
    supplier_id: Optional[int] = None
    note: Optional[str] = None


class StockAdjustReq(BaseModel):
    # Write off / reduce a batch's remaining stock (svinn) with a logged reason. Reduction
    # only: qty_delta_centi < 0 (e.g. trasig, förlorad, använd i projekt utan betalning).
    qty_delta_centi: int
    reason: str


class RutRecipientReq(BaseModel):
    # A recipient is a household member; identify by an existing customer_id and/or
    # name + personnummer. share_pct is the fallback slice of both pots; rut_share_pct
    # and rot_share_pct override it per pot (a person may take a different % of RUT vs ROT).
    customer_id: Optional[int] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    personnummer: Optional[str] = None
    share_pct: Optional[float] = None
    rut_share_pct: Optional[float] = None
    rot_share_pct: Optional[float] = None


class CustomerRelationReq(BaseModel):
    other_kundnummer: int         # the customer to link this one to (household)


class CompanyContactReq(BaseModel):
    contact_kundnummer: int       # the private customer to attach as a company contact


class RebookReq(BaseModel):
    # {moms_line_id (as string): {"category_id": int, "rate_code": str}} — corrected
    # account/rate per booking line. Both keys optional; omitted keeps the current value.
    corrections: dict[str, dict] = {}
    reason: Optional[str] = None


class DeliveryAddressReq(BaseModel):
    # Per-invoice leveransadress (full, company-style fields). All optional; when all
    # are empty the billing address IS the delivery address.
    name: Optional[str] = None
    street: Optional[str] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None
    org_nr: Optional[str] = None
    vat_nr: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class PayInvoiceReq(BaseModel):
    amount_ore: Optional[int] = None   # None = full outstanding
    date: Optional[str] = None
    note: Optional[str] = None         # reference/comment → verifikation text


class RefundInvoiceReq(BaseModel):
    amount_ore: int
    date: Optional[str] = None
    note: Optional[str] = None


class CreditInvoiceReq(BaseModel):
    amount_ore: Optional[int] = None   # None = full remaining
    reason: Optional[str] = None
    date: Optional[str] = None


class InvoiceDraftReq(BaseModel):
    # The whole (possibly incomplete) invoice form payload, saved to continue later.
    payload: dict


class CreateInvoiceReq(BaseModel):
    customer_id: int
    category_id: Optional[int] = None    # fallback account; lines may each set their own
    invoice_date: str
    due_date: str
    lines: list[InvoiceLineReq]
    recipients: Optional[list[RutRecipientReq]] = None
    delivery_date: Optional[str] = None
    payment_terms: Optional[str] = None
    our_reference: Optional[str] = None
    your_reference: Optional[str] = None
    note: Optional[str] = None
    license_keys: Optional[list[str]] = None   # printed on their own page at the PDF end
    contact_customer_id: Optional[int] = None  # optional private-person contact under a business buyer
    delivery_address: Optional[DeliveryAddressReq] = None  # per-invoice leveransadress; empty => = billing
    support_enabled: bool = True                # gratis distanssupport: earn time + print the note


class ReceiptUploadReq(BaseModel):
    image_base64: str             # the raw image bytes, base64-encoded
    mime: str                     # e.g. 'image/jpeg', 'image/png'
    original_format: Optional[str] = None   # 'paper' | 'digital'
