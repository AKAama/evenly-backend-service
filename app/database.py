from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

# Remote Postgres (VPS / cloud) drops idle TCP sockets; without keepalives +
# statement timeout the API can hang for minutes then die with
# "server closed the connection unexpectedly".
_engine_kwargs: dict = {
    "pool_pre_ping": True,  # discard dead connections on checkout
    "pool_recycle": 1800,  # recycle before typical intermediate NAT/firewall idle kill
    "pool_size": 5,
    "max_overflow": 10,
    "pool_timeout": 30,  # wait at most 30s for a free pool slot
}

_url = settings.database_url
if _url.startswith("postgresql"):
    _engine_kwargs["connect_args"] = {
        "connect_timeout": 10,
        # TCP keepalives so half-open sockets die fast instead of hanging ~400s.
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        # Server-side safety nets (ms). Fail locked/hung statements instead of
        # blocking the request worker indefinitely.
        # idle_in_transaction_session_timeout: kill abandoned txns (the pattern
        # that previously blocked the whole DB after a failed deactivate).
        "options": (
            "-c statement_timeout=30000 "
            "-c lock_timeout=15000 "
            "-c idle_in_transaction_session_timeout=30000"
        ),
    }

engine = create_engine(_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@event.listens_for(engine, "connect")
def _on_connect(dbapi_connection, connection_record):
    """Ensure timeouts also apply if connect_args.options were ignored by driver."""
    if not _url.startswith("postgresql"):
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET statement_timeout TO 30000")
        cursor.execute("SET lock_timeout TO 15000")
        cursor.execute("SET idle_in_transaction_session_timeout TO 30000")
        cursor.close()
    except Exception:
        # Best-effort; connect_args options are the primary path.
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
