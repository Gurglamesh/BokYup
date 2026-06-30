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
    kind: str                     # 'income' | 'expense'
    bas_konto: int
    default_rate_code: Optional[str] = None   # default moms for lines on this category
    account_name: Optional[str] = None


class CategoryUpdateReq(BaseModel):
    name: Optional[str] = None
    bas_konto: Optional[int] = None
    default_rate_code: Optional[str] = None
    active: Optional[bool] = None
    account_name: Optional[str] = None


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


class RecordExpenseReq(BaseModel):
    category_id: int
    lines: list[MomsLineReq]
    trans_date: str
    supplier_id: Optional[int] = None
    note: Optional[str] = None
    receipt_original_format: Optional[str] = None   # 'paper' | 'digital'
    paid_date: Optional[str] = None


class RecordIncomeReq(BaseModel):
    customer_id: int
    category_id: int
    lines: list[MomsLineReq]
    trans_date: str
    rut_amount_ore: int = 0
    note: Optional[str] = None
    paid_date: Optional[str] = None


class PaymentReq(BaseModel):
    payment_date: str


class ReverseReq(BaseModel):
    reason: str
    reg_date: Optional[str] = None


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
    email: Optional[str] = None
    phone: Optional[str] = None
    f_skatt: Optional[int] = None


class AccountingMethodReq(BaseModel):
    method: str                   # 'kontantmetod' | 'fakturametod'


class LogoReq(BaseModel):
    image_base64: str             # any common image; normalised to PNG server-side


class PaymentMethodReq(BaseModel):
    label: str                    # "Swish" | "Bankgiro" | "IBAN" | ...
    value: str                    # the number / link
    sort_order: int = 0


class PaymentMethodUpdateReq(BaseModel):
    label: Optional[str] = None
    value: Optional[str] = None
    sort_order: Optional[int] = None
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


class PayInvoiceReq(BaseModel):
    amount_ore: Optional[int] = None   # None = full outstanding
    date: Optional[str] = None


class RefundInvoiceReq(BaseModel):
    amount_ore: int
    date: Optional[str] = None
    note: Optional[str] = None


class CreditInvoiceReq(BaseModel):
    amount_ore: Optional[int] = None   # None = full remaining
    reason: Optional[str] = None
    date: Optional[str] = None


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


class ReceiptUploadReq(BaseModel):
    image_base64: str             # the raw image bytes, base64-encoded
    mime: str                     # e.g. 'image/jpeg', 'image/png'
    original_format: Optional[str] = None   # 'paper' | 'digital'
