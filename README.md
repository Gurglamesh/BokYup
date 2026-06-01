# Bokföring

Legal-grade Swedish bookkeeping for multiple separate entities, each an encrypted
database you switch between like browser tabs. Pure-Python, OS-agnostic, built so the
same backend serves a desktop app today and phone apps later.

See **CLAUDE.md** for the full architecture and decision record.

## Status
Layer 1 (crypto core) implemented and tested. Remaining layers scaffolded — see the
build-status checklist in CLAUDE.md.

## Setup
    python -m pip install -r requirements.txt
    python -m pytest

## What works now
The cryptographic foundation: per-database envelope encryption (Argon2id KEK wrapping a
stable DEK), passphrase change with no data re-encryption, optional offline recovery key,
and authenticated (tamper-detecting) field/blob encryption.
