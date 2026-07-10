# Test User Creation API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, token-protected backend endpoint that creates password users without email verification for local testing.

**Architecture:** A dedicated `/test/users` router owns the test-only HTTP contract and a header-auth dependency. It reuses existing user lookup and creation services, while an optional server setting keeps the route unavailable by default.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, pytest, FastAPI TestClient.

## Global Constraints

- No frontend changes.
- The normal `/auth/register` verification-code flow remains unchanged.
- The token is server-side only and is never committed or logged.
- The endpoint returns no login token and sets no authentication cookie.
- Existing unrelated working-tree changes must be preserved.

---

### Task 1: Configuration and authenticated test-user endpoint

**Files:**
- Modify: `app/config.py`
- Create: `app/routers/test_users.py`
- Modify: `main.py`
- Test: `tests/test_test_users_api.py`

**Interfaces:**
- Consumes: `settings.test_admin_token`, `get_db()`, `get_user_by_email()`, `get_user_by_username()`, and `create_user()`.
- Produces: `POST /test/users`, JSON model `CreateTestUserRequest`, and header dependency `require_test_admin_token()`.

- [ ] **Step 1: Write failing endpoint tests**

Create `tests/test_test_users_api.py` with an isolated SQLite database and TestClient dependency override. Add tests asserting: configured correct token returns `201`, persists `User` plus password `AuthIdentity`, permits `authenticate_user`, emits neither `access_token` nor `set-cookie`, and never invokes verification; missing/wrong header returns `403`; absent configured token returns `404`; duplicate email/username returns `400`; invalid email, invalid username, and passwords shorter than six characters return `422`.

- [ ] **Step 2: Verify tests fail for the missing feature**

Run:

```bash
uv run --group dev python -m pytest -q tests/test_test_users_api.py
```

Expected: failures because `/test/users` is not registered and `Settings` has no `test_admin_token` field.

- [ ] **Step 3: Add the optional configuration field**

Add this field to `Settings` in `app/config.py` without adding a value to `config/config.defaults.yaml`:

```python
test_admin_token: Optional[str] = Field(default=None, validation_alias="TEST_ADMIN_TOKEN")
```

This preserves YAML field-name loading and enables the environment variable through the existing settings source.

- [ ] **Step 4: Implement the minimal dedicated router**

Create `app/routers/test_users.py` containing:

```python
class CreateTestUserRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z][A-Za-z0-9_]{2,29}$")
    password: str = Field(min_length=6)
    display_name: str | None = Field(default=None, max_length=100)
```

Define `require_test_admin_token` with `Header(alias="X-Test-Admin-Token")`: return `404` when the configured token is empty, and use `secrets.compare_digest` to return `403` for a missing or mismatched header. Define `POST /test/users` with `status_code=201` and `response_model=UserResponse`; reject case-insensitive duplicate email or username with `400`, then call `create_user(db, UserCreate(...))` and return the user.

Register the router in `main.py` through `app.include_router(test_users.router)`.

- [ ] **Step 5: Verify focused tests pass**

Run:

```bash
uv run --group dev python -m pytest -q tests/test_test_users_api.py
```

Expected: all test-user API tests pass with no warnings or errors.

- [ ] **Step 6: Run the complete backend regression suite**

Run:

```bash
uv run --group dev python -m pytest -q
```

Expected: all backend tests pass.

- [ ] **Step 7: Document local Apifox usage**

Update `README.md` configuration and API sections with the optional `test_admin_token`, the `POST /test/users` request fields, the `X-Test-Admin-Token` header, and the disabled-by-default behavior. Do not include a real token.

- [ ] **Step 8: Verify formatting and final diff**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only the planned feature files plus the user's pre-existing unrelated changes are present.

- [ ] **Step 9: Commit only feature files**

```bash
git add app/config.py app/routers/test_users.py main.py tests/test_test_users_api.py README.md docs/superpowers/plans/2026-07-10-test-user-creation-api.md
git commit -m "feat: add token-protected test user creation API"
```
