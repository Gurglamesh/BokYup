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
    account_name: Optional[str] = None


class CategoryUpdateReq(BaseModel):
    name: Optional[str] = None
    bas_konto: Optional[int] = None
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
    address: Optional[str] = None              # billing / faktureringsadress
    shipping_address: Optional[str] = None     # leveransadress (if different)
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
    unit: Optional[str] = None
    rut_eligible: bool = False


class RutRecipientReq(BaseModel):
    first_name: str
    last_name: str
    personnummer: str
    rut_amount_ore: int           # this person's share of the skattereduktion


class CreateInvoiceReq(BaseModel):
    customer_id: int
    category_id: int
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
