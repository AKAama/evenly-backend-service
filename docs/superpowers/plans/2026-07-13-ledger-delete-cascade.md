# Ledger Delete Cascade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an owner to delete a ledger containing member-linked expense splits without a foreign-key violation.

**Architecture:** Make `expense_splits.member_id` cascade when its referenced ledger member is deleted. Keep the endpoint unchanged and align the SQLAlchemy model with a new Alembic migration so both new and upgraded databases have the same constraint.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, PostgreSQL, pytest

## Global Constraints

- Deleting a ledger removes all ledger-owned data regardless of members, expenses, splits, settlements, or invite links.
- Single-member removal and balance/history rules remain unchanged.
- Do not add endpoint-specific delete ordering.

---

### Task 1: Reproduce ledger deletion with member-linked splits

**Files:**
- Modify: `tests/test_backend_rules.py`

**Interfaces:**
- Consumes: `delete_ledger(ledger_id: UUID, db: Session, current_user: User)` and the existing SQLAlchemy models.
- Produces: Regression test `test_delete_ledger_cascades_member_linked_expense_splits`.

- [ ] **Step 1: Write the failing test**

Import `text` from SQLAlchemy and `delete_ledger` from `app.routers.ledgers`. Add a test that enables SQLite foreign keys, creates an owner, ledger, member, expense, and split, commits them, calls `delete_ledger`, and verifies all four ledger-owned tables are empty:

```python
def test_delete_ledger_cascades_member_linked_expense_splits(db):
    db.execute(text("PRAGMA foreign_keys=ON"))
    owner = make_user(db, "delete-ledger@example.com", "Owner")
    ledger = make_ledger(db, owner)
    member = db.query(LedgerMember).filter_by(
        ledger_id=ledger.id,
        user_id=owner.id,
    ).one()
    expense = Expense(
        ledger_id=ledger.id,
        payer_id=owner.id,
        created_by=owner.id,
        title="Dinner",
        total_amount=Decimal("10.00"),
        expense_date=date.today(),
        status=ExpenseStatus.CONFIRMED,
    )
    db.add(expense)
    db.flush()
    db.add(ExpenseSplit(
        expense_id=expense.id,
        user_id=owner.id,
        member_id=member.id,
        amount=Decimal("10.00"),
    ))
    db.commit()

    delete_ledger(ledger.id, db=db, current_user=owner)

    assert db.query(Ledger).filter_by(id=ledger.id).count() == 0
    assert db.query(LedgerMember).filter_by(ledger_id=ledger.id).count() == 0
    assert db.query(Expense).filter_by(ledger_id=ledger.id).count() == 0
    assert db.query(ExpenseSplit).filter_by(expense_id=expense.id).count() == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_backend_rules.py::test_delete_ledger_cascades_member_linked_expense_splits -v`

Expected: FAIL with an integrity error showing `expense_splits.member_id` still references a deleted `ledger_members.id`.

- [ ] **Step 3: Commit the regression test**

```bash
git add tests/test_backend_rules.py
git commit -m "test: reproduce ledger delete split constraint"
```

### Task 2: Add the missing database cascade

**Files:**
- Modify: `app/models/expense.py:76`
- Create: `alembic/versions/20260713_0018_cascade_expense_split_members.py`

**Interfaces:**
- Consumes: Existing constraint `fk_expense_splits_member_id`.
- Produces: The same named foreign key with `ON DELETE CASCADE` in metadata and upgraded PostgreSQL databases.

- [ ] **Step 1: Update the SQLAlchemy model**

Change the member foreign key to:

```python
member_id = Column(
    UUID(as_uuid=True),
    ForeignKey("ledger_members.id", ondelete="CASCADE"),
    nullable=False,
)
```

- [ ] **Step 2: Add the Alembic migration**

Create revision `20260713_0018`, with `down_revision = "20260712_0017"`. In `upgrade()`, drop `fk_expense_splits_member_id` and recreate it with `ondelete="CASCADE"`. In `downgrade()`, replace it with the original non-cascading constraint.

```python
def upgrade() -> None:
    op.drop_constraint(
        "fk_expense_splits_member_id",
        "expense_splits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_expense_splits_member_id",
        "expense_splits",
        "ledger_members",
        ["member_id"],
        ["id"],
        ondelete="CASCADE",
    )
```

- [ ] **Step 3: Run the targeted test to verify it passes**

Run: `pytest tests/test_backend_rules.py::test_delete_ledger_cascades_member_linked_expense_splits -v`

Expected: PASS.

- [ ] **Step 4: Run backend regression tests and migration validation**

Run: `pytest -q`

Expected: all tests pass.

Run: `alembic heads`

Expected: exactly one head, `20260713_0018`.

- [ ] **Step 5: Commit the fix**

```bash
git add app/models/expense.py alembic/versions/20260713_0018_cascade_expense_split_members.py
git commit -m "fix: cascade ledger member split deletion"
```

