# Ledger Delete Cascade Fix

## Problem

Deleting a ledger that contains expense splits fails when SQLAlchemy deletes
`ledger_members` before the referenced `expense_splits`. PostgreSQL rejects the
delete because `expense_splits.member_id` currently has a non-cascading foreign
key to `ledger_members.id`.

## Intended behavior

The ledger owner can delete a ledger regardless of whether it contains members,
expenses, splits, settlements, or invite links. Deleting the ledger permanently
removes all ledger-owned data. The balance and history rules for removing one
member remain unchanged.

## Design

Add an Alembic migration that replaces `fk_expense_splits_member_id` with the
same foreign key using `ON DELETE CASCADE`. Update the SQLAlchemy model to declare
the matching `ondelete="CASCADE"` behavior.

This is deliberately narrower than changing all ledger relationships to passive
database deletes. Existing cascades from ledgers to expenses and from expenses
to splits remain in place; the new cascade makes deletion safe when the ORM
chooses to delete members before expenses. It also covers ledger deletion during
account deletion without adding endpoint-specific ordering code.

## Error handling

Authorization and not-found behavior do not change. A database failure still
rolls back the transaction through the existing request/session handling.

## Verification

Add a PostgreSQL-backed regression test that creates a ledger with members, an
expense, and member-linked expense splits, then deletes the ledger through the
endpoint and verifies the ledger, members, expense, and splits are gone. Run the
targeted regression test followed by the backend test suite.

