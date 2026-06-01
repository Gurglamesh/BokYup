"""
schema.py — Layer 3 (NOT YET IMPLEMENTED).

SQLite schema + application-level field encryption via core.crypto.
Entities: verifikation (sequential, immutable), transaction (3 moms figures,
in/out, deductibility), customer (private/business, snapshot-on-invoice),
supplier, category<->BAS-konto, RUT lifecycle, period_lock, rattelse.
See CLAUDE.md > "Legal requirements", "Moms model", "RUT", "Customers".
"""
