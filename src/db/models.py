"""Typed row helpers for SQLite entities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


DIMENSION_NAMES = (
    "jfrog_relevance",
    "competitor_signal",
    "strategic_impact",
    "freshness",
    "market_visibility",
)

Vote = Literal["up", "down"]
RunStatus = Literal["running", "success", "partial", "failed", "degraded"]
RunTrigger = Literal["cron", "manual", "seed"]


@dataclass(frozen=True)
class DimensionScores:
    jfrog_relevance: int
    competitor_signal: int
    strategic_impact: int
    freshness: int
    market_visibility: int
    model_id: str
    scored_at: str

    def as_dict(self) -> dict[str, int]:
        return {
            "jfrog_relevance": self.jfrog_relevance,
            "competitor_signal": self.competitor_signal,
            "strategic_impact": self.strategic_impact,
            "freshness": self.freshness,
            "market_visibility": self.market_visibility,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any], model_id: str, scored_at: str) -> DimensionScores:
        return cls(
            jfrog_relevance=int(data["jfrog_relevance"]),
            competitor_signal=int(data["competitor_signal"]),
            strategic_impact=int(data["strategic_impact"]),
            freshness=int(data["freshness"]),
            market_visibility=int(data["market_visibility"]),
            model_id=model_id,
            scored_at=scored_at,
        )


@dataclass
class NewsItem:
    id: str
    title: str
    url: str
    source_id: str
    competitor: str
    published_at: str | None
    ingested_at: str
    summary: str | None
    category: str | None
    raw_excerpt: str | None
    content_hash: str
    relevance_score: float | None
    run_id: str | None
    status: str = "classified"
    filter_reason: str | None = None
    item_type: str | None = None
    jfrog_implication: str | None = None
    is_fallback: bool = False
