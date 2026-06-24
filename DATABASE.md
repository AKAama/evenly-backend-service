# Database Migrations

Database schema changes are managed with Alembic. The FastAPI app no longer
creates or updates tables at startup.

## New database

```bash
make db-upgrade
```

For local development, the quickest path is:

```bash
make dev-db-up
make db-upgrade
make dev-api
```

## Existing database

If the database was previously created by `Base.metadata.create_all`, inspect
the schema first. If it already matches the initial migration, mark it as
managed without replaying table creation:

```bash
uv run python -m alembic stamp head
```

After that, future schema changes should be added with:

```bash
make db-revision m="describe change"
make db-upgrade
```

Always back up production data before running migrations.
