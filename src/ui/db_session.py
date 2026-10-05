"""Streamlit-scoped DB connection (Turso reconnect after writes)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.db.connection import (
    get_connection,
    init_db,
    is_connection_error,
    ping_connection,
    turso_configured,
)
from src.db.repository import Repository

DB_DIRTY_KEY = "_ci_db_dirty"


@st.cache_resource
def _ensure_schema_ready(cache_key: str) -> bool:
    """Apply schema + migrations once per server process for this DB key.

    Kept separate from the connection cache so post-write reconnects (and
    ``invalidate_db_cache``) do not re-run the expensive Turso round trips.
    """
    if turso_configured():
        init_db()
    else:
        from src.db.connection import resolve_db_path

        init_db(resolve_db_path())
    return True


@st.cache_resource
def _cached_connection(cache_key: str):
    _ensure_schema_ready(cache_key)
    if turso_configured():
        return get_connection(), None
    from src.db.connection import resolve_db_path

    path = resolve_db_path()
    return get_connection(path), path


def invalidate_db_cache() -> None:
    """Drop the cached connection only; schema stays warm for this process."""
    try:
        _cached_connection.clear()
    except Exception:  # noqa: BLE001
        pass


def mark_db_dirty() -> None:
    st.session_state[DB_DIRTY_KEY] = True


def get_repository() -> tuple[Repository, Path | None]:
    if turso_configured():
        cache_key = "turso"
    else:
        from src.db.connection import resolve_db_path

        cache_key = f"local:{resolve_db_path()}"

    if st.session_state.pop(DB_DIRTY_KEY, False):
        invalidate_db_cache()

    conn, path = _cached_connection(cache_key)
    try:
        ping_connection(conn)
    except Exception as exc:  # noqa: BLE001
        if not (turso_configured() or is_connection_error(exc)):
            raise
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        invalidate_db_cache()
        conn, path = _cached_connection(cache_key)
        ping_connection(conn)
    return Repository(conn), path
