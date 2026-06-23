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
    address: Optional[str] = None
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
