"""
Tests for the transport-independent AppFacade (Phase 1 of the phone plan).

These drive the backend the way the PHONE will — through `facade.dispatch(method,
path, body, query)` in-process, with no HTTP server — and assert the same behaviour
the FastAPI tests assert over HTTP. This is what guarantees PC and phone run the
exact same logic.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from backend.api.facade import AppFacade, BookLocked, RawResult
from backend.db.manager import DatabaseManager


@pytest.fixture()
def facade(tmp_path: Path) -> AppFacade:
    return AppFacade(DatabaseManager(tmp_path / "registry"), autolock_seconds=900)


@pytest.fixture()
def book(facade, tmp_path):
    status, rec = facade.dispatch("POST", "/books", {
        "display_name": "Test AB", "db_path": str(tmp_path / "t.db"),
        "passphrase": "correct-horse"})
    assert status == 201
    return rec["id"]


def test_meta_and_books(facade):
    assert facade.dispatch("GET", "/")[1]["name"] == "BokYup API"
    assert facade.dispatch("GET", "/books") == (200, [])


def test_full_income_flow_through_dispatch(facade, book):
    cat = facade.dispatch("POST", f"/books/{book}/categories",
                          {"name": "Försäljning", "kind": "income", "bas_konto": 3001})[1]["id"]
    kid = facade.dispatch("POST", f"/books/{book}/customers",
                          {"type": "business", "company_name": "ACME AB"})[1]["kundnummer"]
    status, res = facade.dispatch("POST", f"/books/{book}/incomes", {
        "customer_id": kid, "category_id": cat,
        "lines": [{"rate_code": "25", "amount_ore": 1250, "inclusive": True}],
        "trans_date": "2026-02-10", "paid_date": "2026-02-10"})
    assert status == 201 and res["ver_number"] == 1

    rep = facade.dispatch("GET", f"/books/{book}/reports/momsdeklaration",
                          query={"start": "2026-01-01", "end": "2026-03-31"})[1]
    assert rep["boxes"]["10"] == 250


def test_sie_and_receipt_return_raw(facade, book):
    sie = facade.dispatch("GET", f"/books/{book}/reports/sie",
                          query={"company_name": "Min Firma"})[1]
    assert isinstance(sie, RawResult) and "#FNAMN" in sie.content


def test_locked_book_raises(facade, book):
    facade.dispatch("POST", f"/books/{book}/lock", {})
    with pytest.raises(BookLocked):
        facade.dispatch("GET", f"/books/{book}/categories")
    # unlock again restores access
    facade.dispatch("POST", f"/books/{book}/unlock", {"passphrase": "correct-horse"})
    assert facade.dispatch("GET", f"/books/{book}/categories") == (200, [])


def test_unknown_route_raises_keyerror(facade):
    with pytest.raises(KeyError):
        facade.dispatch("GET", "/nope")


def test_key_management_through_dispatch(facade, book):
    # GET and POST share /recovery-key — confirm the matcher distinguishes them.
    assert facade.dispatch("GET", f"/books/{book}/recovery-key")[1]["has_recovery_key"] is False
    status, res = facade.dispatch("POST", f"/books/{book}/recovery-key",
                                  {"passphrase": "correct-horse"})
    assert status == 201 and "-" in res["recovery_key"]
    assert facade.dispatch("GET", f"/books/{book}/recovery-key")[1]["has_recovery_key"] is True

    facade.dispatch("POST", f"/books/{book}/change-passphrase",
                    {"old_passphrase": "correct-horse", "new_passphrase": "p2"})
    facade.dispatch("POST", f"/books/{book}/lock", {})
    # recovery key still unlocks after the passphrase change
    facade.manager.open_book_with_recovery(book, res["recovery_key"])
