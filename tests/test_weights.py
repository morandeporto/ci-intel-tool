"""Tests for persistent weight settings."""

from __future__ import annotations

from src.config_loader import load_weights
from src.db.connection import get_connection, init_db
from src.db.repository import Repository
from src.services.weights import get_effective_weights, save_weights


def test_save_and_load_weights(tmp_path):
    db = tmp_path / "w.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)

    base = load_weights()
    assert get_effective_weights(repo) == base

    custom = {
        "jfrog_relevance": 0.5,
        "competitor_signal": 0.2,
        "strategic_impact": 0.2,
        "freshness": 0.05,
        "market_visibility": 0.05,
    }
    saved = save_weights(repo, custom)
    assert abs(sum(saved.values()) - 1.0) < 0.01
    loaded = get_effective_weights(repo)
    assert loaded["jfrog_relevance"] == saved["jfrog_relevance"]
    conn.close()
