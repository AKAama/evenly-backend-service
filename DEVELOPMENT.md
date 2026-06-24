# Backend Development

## Start locally

Edit the local config file first:

```bash
config/config.yaml
```

This file is ignored by Git and is the main local runtime config.

Start PostgreSQL and Redis:

```bash
make dev-db-up
```

This requires Docker Desktop to be running.

Use Redis for verification codes by adding this to `config/config.yaml` or the
environment:

```yaml
redis_url: redis://localhost:6379/0
```

Run migrations:

```bash
make db-upgrade
```

Start the API:

```bash
make dev-api
```

The API listens on `http://localhost:8000`.

## Check common startup issues

If business endpoints return `500 Internal Server Error`, check the database
connection:

```bash
make doctor
```

If port `8000` is already occupied:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Use `uv run python -m uvicorn main:app ...` instead of `uv run uvicorn ...`.
The module form reliably uses the project environment.
