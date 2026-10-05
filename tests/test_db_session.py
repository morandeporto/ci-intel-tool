"""Schema setup runs once per process, not on every post-write reconnect."""

from __future__ import annotations

import pytest

from src.ui import db_session


def test_invalidate_clears_connection_not_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    init_calls: list[str] = []
    conn_calls: list[str] = []

    monkeypatch.setattr(db_session, "turso_configured", lambda: True)
    monkeypatch.setattr(
        db_session, "init_db", lambda *a, **k: init_calls.append("init") or "turso"
    )
    monkeypatch.setattr(
        db_session, "get_connection", lambda *a, **k: conn_calls.append("conn") or object()
    )

    db_session._ensure_schema_ready.clear()
    db_session._cached_connection.clear()

    db_session._cached_connection("turso")
    assert init_calls == ["init"]
    assert conn_calls == ["conn"]

    # Same as every UI write: drop connection, keep schema cache warm.
    db_session.invalidate_db_cache()
    db_session._cached_connection("turso")

    assert init_calls == ["init"]
    assert conn_calls == ["conn", "conn"]
