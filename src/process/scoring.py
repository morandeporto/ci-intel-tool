"""Weighted relevance scoring from LLM dimension scores and config weights.

Weights live in config so they can be tuned without re-querying the LLM.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.db.models import DIMENSION_NAMES, DimensionScores

WEIGHT_SUM_TOLERANCE = 0.01
DIM_MIN = 1
DIM_MAX = 5


class ScoringError(ValueError):
    """Raised when dimension scores or weights fail validation."""


def _as_dimension_dict(dimensions: Mapping[str, Any] | DimensionScores) -> dict[str, int]:
    if isinstance(dimensions, DimensionScores):
        return dimensions.as_dict()
    return {name: dimensions[name] for name in DIMENSION_NAMES if name in dimensions}


def validate_dimensions(dimensions: Mapping[str, Any] | DimensionScores) -> dict[str, int]:
    """Ensure every dimension is present and within the LLM 1-5 scale."""
    dims = _as_dimension_dict(dimensions)
    missing = [name for name in DIMENSION_NAMES if name not in dims]
    if missing:
        raise ScoringError(f"Missing dimension scores: {', '.join(missing)}")
    for name in DIMENSION_NAMES:
        value = dims[name]
        # bool is a subclass of int, reject it so True/False never become 1/0 scores.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ScoringError(f"Dimension {name} must be an integer, got {value!r}")
        if value < DIM_MIN or value > DIM_MAX:
            raise ScoringError(
                f"Dimension {name}={value} out of range [{DIM_MIN}, {DIM_MAX}]"
            )
    return {name: dims[name] for name in DIMENSION_NAMES}


def validate_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """Ensure weights cover all dimensions and sum to ~1.0 for stable ranking."""
    missing = [name for name in DIMENSION_NAMES if name not in weights]
    if missing:
        raise ScoringError(f"Missing weights: {', '.join(missing)}")
    normalized = {name: float(weights[name]) for name in DIMENSION_NAMES}
    total = sum(normalized.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ScoringError(f"weights must sum to 1.0 (±{WEIGHT_SUM_TOLERANCE}), got {total:.4f}")
    return normalized


def weighted_score(
    dimensions: Mapping[str, Any] | DimensionScores,
    weights: Mapping[str, float],
) -> float:
    """Compute sum(dim_i * weight_i), keeps scoring deterministic and tunable."""
    dims = validate_dimensions(dimensions)
    w = validate_weights(weights)
    return sum(dims[name] * w[name] for name in DIMENSION_NAMES)


def recalculate_scores(
    items_with_dims: Iterable[tuple[str, Mapping[str, Any] | DimensionScores]],
    weights: Mapping[str, float],
) -> list[tuple[str, float]]:
    """Re-rank stored items after weight changes without calling the LLM again."""
    validate_weights(weights)  # fail fast before looping
    results: list[tuple[str, float]] = []
    for item_id, dimensions in items_with_dims:
        results.append((item_id, weighted_score(dimensions, weights)))
    return results
