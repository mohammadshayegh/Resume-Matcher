"""SQLite engine/session plumbing for the SQLAlchemy data layer.

Every ``Database`` instance owns its own engines (one async for the document
tables, one sync for the encrypted ``api_keys`` table read on the synchronous
LLM hot path) built from these factories. Keeping construction here lets tests
spin up fully isolated engines against a temp-file database.
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import LOCAL_USER_ID, Base

logger = logging.getLogger(__name__)

__all__ = ["Base", "make_async_engine", "make_sync_engine", "init_models_sync"]


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set per-connection SQLite PRAGMAs.

    WAL improves concurrent read/write between the async (doc tables) and sync
    (api_keys) engines pointed at the same file; ``busy_timeout`` rides out the
    brief lock contention that creates; ``foreign_keys`` enforces relational
    integrity (off by default in SQLite).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _url(path: Path, *, driver: str) -> str:
    """Build a SQLite URL. Absolute paths yield the required four slashes."""
    return f"sqlite+{driver}:///{path}" if driver else f"sqlite:///{path}"


def make_async_engine(path: Path) -> AsyncEngine:
    """Create the async engine (``aiosqlite``) for the document tables."""
    engine = create_async_engine(_url(path, driver="aiosqlite"), future=True)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sync_engine(path: Path) -> Engine:
    """Create the sync engine used for the encrypted api_keys table.

    Key reads happen synchronously (``get_llm_config`` → ``load_config_file`` →
    ``resolve_api_key``), so a sync engine avoids threading async through
    ``llm.py``. It points at the same file as the async engine.
    """
    engine = create_engine(_url(path, driver=""), future=True)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


# Tables that gained a ``user_id`` partition key when Supabase authentication
# was introduced. ``api_keys`` is absent on purpose: LLM credentials stay
# operator-owned and shared across accounts.
_USER_PARTITIONED_TABLES: tuple[str, ...] = (
    "resumes",
    "jobs",
    "improvements",
    "tailoring_previews",
    "applications",
)


def _add_user_partition(conn: Any) -> None:
    """Backfill the ``user_id`` partition key on a pre-auth local database.

    ``create_all`` never ALTERs an existing SQLite table, so a database created
    before multi-user support has these tables without ``user_id``. Adding the
    column is idempotent and additive; rows already present are attributed to
    the local single-user id, which is the account they were in fact created
    under (authentication did not exist yet).

    The old global single-master unique index is dropped: with more than one
    account, "exactly one master resume" is a per-user invariant, and leaving
    the global index in place would let the first user's master block everyone
    else's. ``create_all`` above has already created the per-user replacement.
    """
    for table in _USER_PARTITIONED_TABLES:
        columns = conn.exec_driver_sql(f"PRAGMA table_info({table})").mappings().all()
        if not columns:
            continue  # Table does not exist yet; create_all made it correctly.
        if "user_id" in {column["name"] for column in columns}:
            continue
        conn.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT '{LOCAL_USER_ID}'"
        )
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_user_id ON {table} (user_id)"
        )
        logger.info("Added user_id partition key to %s", table)

    # Replaced by ux_resumes_single_master_per_user (created by create_all).
    conn.exec_driver_sql("DROP INDEX IF EXISTS ux_resumes_single_master")


def init_models_sync(engine: Engine) -> None:
    """Create all tables (idempotent) using a sync engine connection."""
    Base.metadata.create_all(engine)

    # ``create_all`` does not ALTER existing SQLite tables. Keep this additive
    # migration idempotent so older local databases can load resumes safely.
    with engine.begin() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info(resumes)").mappings().all()
        existing_columns = {column["name"] for column in columns}
        if columns and "interview_prep" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN interview_prep TEXT")
        if columns and "processing_token" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN processing_token TEXT")

        preview_columns = conn.exec_driver_sql("PRAGMA table_info(tailoring_previews)").mappings().all()
        if preview_columns and "improvements" not in {column["name"] for column in preview_columns}:
            conn.exec_driver_sql("ALTER TABLE tailoring_previews ADD COLUMN improvements JSON")

        _add_user_partition(conn)

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_preview_compatibility "
            "ON tailoring_previews (user_id, source_id, job_id, payload_hash, created_at)"
        )
