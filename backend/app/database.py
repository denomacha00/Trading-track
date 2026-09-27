"""Database engine, session factory and Base declarative class."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")

# check_same_thread=False is required for SQLite when used across threads
# (FastAPI runs sync DB calls in a threadpool). ``timeout`` makes a connection
# wait up to 30s for a lock to clear instead of raising "database is locked"
# immediately — the in-process monitor loop and request handlers both write to
# the single SQLite file.
_connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if _is_sqlite
    else {}
)

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver glue
        # WAL lets dashboard reads proceed while the monitor loop writes;
        # busy_timeout waits out brief write locks; NORMAL keeps durability
        # reasonable without an fsync on every commit.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables and apply lightweight additive migrations.

    Import models so they register with Base, create any missing tables, then
    add columns that were introduced after a database was first created. This is
    a minimal migration path (additive only) so existing SQLite DBs keep working
    without a full Alembic setup.
    """
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()


# Columns added after initial release: {table: {column: SQL type + default}}.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "trades": {
        "stop_order_id": "VARCHAR(64)",
        "order_type": "VARCHAR(8) DEFAULT 'market'",
        "limit_price": "FLOAT",
        "user_id": "INTEGER",
        "fee": "FLOAT DEFAULT 0.0",
    },
    "signal_logs": {"user_id": "INTEGER"},
    # Multi-tenant licensing v2: username login + time-limited licences.
    "users": {
        "username": "VARCHAR(64)",
        "license_expires_at": "DATETIME",
    },
    "license_keys": {"duration_days": "INTEGER"},
}

# Indexes added after initial release. Idempotent (IF NOT EXISTS): a no-op on a
# fresh DB (create_all already made them) and additive on an existing one. A
# UNIQUE index on the freshly-added, all-NULL username column is safe — SQLite
# treats NULLs as distinct, so legacy rows without a username never collide.
_ADDED_INDEXES: list[str] = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users(username)",
]


def _apply_additive_migrations() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in present:
                with engine.begin() as conn:
                    conn.execute(
                        text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')
                    )
    if "users" in existing_tables:
        for ddl in _ADDED_INDEXES:
            with engine.begin() as conn:
                conn.execute(text(ddl))
