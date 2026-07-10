# Test User Creation API Design

## Goal

Provide a backend-only API for creating password users during local testing without sending or verifying an email verification code. The API is intended to be called from Apifox and must not change the normal registration flow.

## Scope

- Add `POST /test/users`.
- Accept a server-side test administration token through the `X-Test-Admin-Token` request header.
- Create a normal password user that can subsequently log in through the existing login endpoint.
- Do not add or change any PC frontend UI.
- Do not issue an access token or authentication cookie for the created user.
- Do not support avatar upload in this test endpoint.

## Configuration

Add an optional `test_admin_token` setting. It can be supplied in the ignored local `config/config.yaml` file:

```yaml
test_admin_token: "a-long-random-secret"
```

It can also be supplied through `TEST_ADMIN_TOKEN` using the existing settings source precedence.

The committed defaults file will not contain a working token. When the setting is missing or empty, the test endpoint behaves as unavailable and returns `404 Not Found`. This makes the endpoint opt-in and prevents an accidental deployment from enabling it by default.

## API Contract

### Request

`POST /test/users`

Header:

```http
X-Test-Admin-Token: a-long-random-secret
Content-Type: application/json
```

Body:

```json
{
  "email": "test001@example.com",
  "username": "test001",
  "password": "123456",
  "display_name": "Test User"
}
```

Fields use the existing user constraints: a valid email, a username of 3–30 characters beginning with an ASCII letter and containing only ASCII letters, digits, and underscores, a password of at least 6 characters, and an optional display name of at most 100 characters.

### Response

On success, return `201 Created` with the existing public `UserResponse` representation. The response must not contain a password hash or access token and must not set an authentication cookie.

Errors:

- `404` when `test_admin_token` is not configured.
- `403` when the header is missing or does not match.
- `400` when the email or username is already registered.
- `422` when request fields fail schema validation.

Missing and incorrect tokens share the same `403` response. Token comparison uses `secrets.compare_digest`. The token, password, and request headers are never logged.

## Architecture

Create a dedicated router in `app/routers/test_users.py` with the `/test` prefix. Keeping test-only behavior separate from `app/routers/auth.py` prevents the normal verification-code registration path from gaining a bypass branch and makes the feature easy to remove.

The router has two focused operations:

1. A dependency validates that the feature is configured and that `X-Test-Admin-Token` matches.
2. The endpoint validates uniqueness and delegates persistence and password hashing to the existing `app.services.auth.create_user` service.

The router is registered in `main.py`. No database migration is required because it creates the existing `User` and `AuthIdentity` records.

## Data Flow

1. Apifox sends JSON and `X-Test-Admin-Token`.
2. The authentication dependency returns `404` if the feature is disabled or `403` if authentication fails.
3. Pydantic validates the user fields.
4. The endpoint performs case-insensitive email and username uniqueness checks using existing service helpers.
5. The existing `create_user` service hashes the password and atomically creates the user and password identity.
6. The API returns the new user's public fields with status `201`.

The email verification store and email service are never called.

## Testing

Tests will be written before implementation and will cover:

- A configured matching token creates a usable password user and returns `201` without a login cookie or access token.
- The verification service is not called.
- A missing header returns `403`.
- An incorrect header returns `403`.
- An unconfigured test token returns `404`, even when a header is supplied.
- Duplicate email and duplicate username requests return `400`.
- Invalid email, username, and short password requests return `422`.

After focused tests pass, run the complete backend test suite.

## Security Notes

This is a privileged test backdoor and is safe only while the secret remains server-side. The token belongs only in the ignored local configuration and the caller's Apifox environment; it must never be committed, placed in frontend environment variables, or logged. The absence of a configured token is the production-safe default.
