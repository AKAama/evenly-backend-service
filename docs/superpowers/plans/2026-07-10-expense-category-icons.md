# Expense Category Icons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Synchronize expense categories and SF Symbol/Emoji icons across users and devices.

**Architecture:** PostgreSQL is the source of truth for three nullable expense fields. FastAPI validates and returns them; iOS owns a shared curated catalog, sends selections, and renders saved icon types with a legacy fallback.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, pytest, SwiftUI, XCTest.

## Global Constraints

- Work directly on `main` as requested.
- Preserve compatibility for existing expenses with null icon fields.
- Use TDD before production changes.

---

### Task 1: Backend persistence and API

**Files:** `tests/test_expense_icons.py`, `app/models/expense.py`, `app/schemas/expense.py`, `app/routers/expenses.py`, `alembic/versions/20260710_0011_expense_category_icons.py`.

- [ ] Add failing tests for valid SF Symbol/Emoji persistence, null compatibility, and invalid type/value combinations.
- [ ] Run focused pytest and verify RED.
- [ ] Add nullable model columns, schema validation, router persistence, and migration.
- [ ] Run focused and full pytest and verify GREEN.

### Task 2: iOS models and catalog

**Files:** `Evenly/Expense.swift`, `Evenly/API/APIModels.swift`, `Evenly/ExpenseCategoryCatalog.swift`, `EvenlyTests/EvenlyTests.swift`.

- [ ] Add failing encoding/decoding and catalog tests.
- [ ] Run focused XCTest and verify RED.
- [ ] Add optional category/icon fields and the shared curated catalog.
- [ ] Run focused XCTest and verify GREEN.

### Task 3: iOS creation and list UI

**Files:** `Evenly/AddExpenseView.swift`, `Evenly/ContentView.swift`.

- [ ] Make preset selection carry category and icon, restore saved values, and map voice draft categories.
- [ ] Add a picker for curated SF Symbols and Emoji.
- [ ] Render saved icons in expense rows with the money icon fallback.
- [ ] Run the complete iOS test suite and build.

### Task 4: Final verification

- [ ] Run full backend tests, Alembic SQL generation, iOS tests, and `git diff --check` in both repositories.
- [ ] Commit backend and iOS changes separately on `main` and push both repositories.
