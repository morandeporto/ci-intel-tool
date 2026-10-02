"""Streamlit-scoped DB connection (Turso reconnect after writes)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.db.connection import (
    is_connection_error,
    open_repo_connection,
    ping_connection,
    turso_configured,
)
from src.db.repository import Repository

DB_DIRTY_KEY = "_ci_db_dirty"


@st.cache_resource
def _cached_connection(cache_key: str):
    return open_repo_connection()


def invalidate_db_cache() -> None:
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
