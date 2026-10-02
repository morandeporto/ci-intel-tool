"""Persistent weight settings (shared across reviewers when using Turso)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.config_loader import load_weights
from src.db.models import DIMENSION_NAMES
from src.db.repository import Repository
from src.process.scoring import validate_weights


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


WEIGHTS_KEY = "weights"


def get_effective_weights(repo: Repository | None = None) -> dict[str, float]:
    """Prefer DB-saved weights; fall back to config/weights.yaml."""
    if repo is not None:
        saved = repo.get_setting(WEIGHTS_KEY)
        if saved:
            try:
                data = json.loads(saved)
                if isinstance(data, dict):
                    return validate_weights({k: float(data[k]) for k in DIMENSION_NAMES})
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
    return load_weights()


def save_weights(repo: Repository, weights: dict[str, float]) -> dict[str, float]:
    """Validate, persist, and return normalized weights."""
    normalized = validate_weights(weights)
    repo.set_setting(WEIGHTS_KEY, json.dumps(normalized), updated_at=_utc_now())
    return normalized


def weights_meta(repo: Repository) -> dict[str, Any]:
    """Return saved weights plus updated_at if present."""
    row = repo.get_setting_row(WEIGHTS_KEY)
    if not row:
        return {"weights": load_weights(), "updated_at": None, "source": "config"}
    try:
        data = json.loads(row["value"])
        weights = validate_weights({k: float(data[k]) for k in DIMENSION_NAMES})
        return {
            "weights": weights,
            "updated_at": row["updated_at"],
            "source": "database",
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"weights": load_weights(), "updated_at": None, "source": "config"}
