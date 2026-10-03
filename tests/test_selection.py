"""Tests for per-source caps and kind-balanced LLM selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.ingest.normalize import NormalizedEntry
from src.process.selection import (
    balance_across_kinds,
    rank_entries_for_source,
    select_for_llm,
    take_top_per_source,
)

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def _entry(
    source_id: str,
    title: str,
    *,
    hours_ago: float = 1,
    excerpt: str = "",
    url: str | None = None,
) -> NormalizedEntry:
    published = (NOW - timedelta(hours=hours_ago)).isoformat()
    link = url or f"https://example.com/{source_id}/{title.replace(' ', '-')}"
    return NormalizedEntry(
        title=title,
        url=link,
        published_at=published,
        raw_excerpt=excerpt,
        source_id=source_id,
        competitor="x",
        content_hash="h-" + link,
    )


def test_official_ranked_by_recency() -> None:
    older = _entry("snyk_blog", "Old", hours_ago=10)
    newer = _entry("snyk_blog", "New", hours_ago=1)
    ranked = rank_entries_for_source([older, newer], kind="official_competitor")
    assert ranked[0].title == "New"


def test_industry_ranked_by_strong_count_then_recency() -> None:
    weak_new = _entry("devops_com", "Weak new", hours_ago=1, excerpt="container news")
    strong_old = _entry(
        "devops_com", "Strong old", hours_ago=20, excerpt="SBOM mandate"
    )
    strong_new = _entry(
        "devops_com", "Strong new", hours_ago=2, excerpt="SBOM and CVE"
    )
    counts = {
        weak_new.url: 0,
        strong_old.url: 1,
        strong_new.url: 2,
    }
    ranked = rank_entries_for_source(
        [weak_new, strong_old, strong_new],
        kind="industry",
        strong_counts=counts,
    )
    assert [e.title for e in ranked] == ["Strong new", "Strong old", "Weak new"]


def test_top_three_per_source() -> None:
    entries = {
        "a": [_entry("a", f"A{i}", hours_ago=i) for i in range(5)],
        "b": [_entry("b", f"B{i}", hours_ago=i) for i in range(2)],
    }
    topped = take_top_per_source(
        entries,
        kind_by_source={"a": "official_competitor", "b": "official_competitor"},
        max_per_source=3,
    )
    assert len(topped["a"]) == 3
    assert len(topped["b"]) == 2
    assert topped["a"][0].title == "A0"  # hours_ago=0 is newest


def test_balanced_slots_prevent_competitors_from_consuming_cap() -> None:
    # 12 official candidates from 4 sources, 6 industry — reserved 8/4/6, max 20.
    official: dict[str, list[NormalizedEntry]] = {}
    for src in ("snyk_blog", "github_blog", "gitlab_blog", "sonatype_blog"):
        official[src] = [_entry(src, f"{src}-{i}", hours_ago=i) for i in range(3)]
    industry = {
        "devops_com": [_entry("devops_com", f"ind-{i}", hours_ago=i) for i in range(3)],
        "unit42": [_entry("unit42", f"u42-{i}", hours_ago=i) for i in range(3)],
    }
    candidates = {**official, **industry}
    kinds = {s: "official_competitor" for s in official}
    kinds["devops_com"] = "industry"
    kinds["unit42"] = "industry"

    result = balance_across_kinds(
        candidates,
        kind_by_source=kinds,
        reserved_slots={
            "official_competitor": 8,
            "emerging": 4,
            "industry_community": 6,
        },
        max_items=20,
    )
    official_selected = [
        e for e in result.selected if kinds[e.source_id] == "official_competitor"
    ]
    industry_selected = [
        e for e in result.selected if kinds[e.source_id] == "industry"
    ]
    assert len(result.selected) == 18  # 12 official + 6 industry available
    assert len(official_selected) == 12  # unused emerging (4) spilled to official
    assert len(industry_selected) == 6


def test_unused_emerging_slots_flow_to_other_kinds() -> None:
    official = {
        "snyk_blog": [_entry("snyk_blog", f"s{i}", hours_ago=i) for i in range(3)],
    }
    industry = {
        "devops_com": [_entry("devops_com", f"d{i}", hours_ago=i) for i in range(3)],
    }
    # No emerging candidates — their 4 reserved slots should spill.
    result = balance_across_kinds(
        {**official, **industry},
        kind_by_source={
            "snyk_blog": "official_competitor",
            "devops_com": "industry",
        },
        reserved_slots={
            "official_competitor": 2,
            "emerging": 4,
            "industry_community": 2,
        },
        max_items=10,
    )
    assert len(result.selected) == 6  # all available
    assert {e.source_id for e in result.selected} == {"snyk_blog", "devops_com"}


def test_cap_skipped_are_not_selected() -> None:
    candidates = {
        "a": [_entry("a", f"A{i}", hours_ago=i) for i in range(3)],
        "b": [_entry("b", f"B{i}", hours_ago=i) for i in range(3)],
    }
    result = balance_across_kinds(
        candidates,
        kind_by_source={"a": "official_competitor", "b": "official_competitor"},
        reserved_slots={
            "official_competitor": 8,
            "emerging": 0,
            "industry_community": 0,
        },
        max_items=3,
    )
    assert len(result.selected) == 3
    assert len(result.cap_skipped) == 3
    selected_urls = {e.url for e in result.selected}
    assert all(e.url not in selected_urls for e in result.cap_skipped)


def test_select_for_llm_round_trip() -> None:
    entries = [
        _entry("snyk_blog", "Launch", hours_ago=1),
        _entry("devops_com", "SBOM news", hours_ago=2, excerpt="SBOM update"),
        _entry("devops_com", "Containers only", hours_ago=1, excerpt="Docker tips"),
    ]
    meta = {
        "snyk_blog": {"kind": "official_competitor", "gate": "off"},
        "devops_com": {"kind": "industry", "gate": "strict"},
    }
    relevance = {
        "strong_keywords": ["SBOM"],
        "weak_keywords": ["Docker"],
        "exclude_title_patterns": [],
    }
    result = select_for_llm(
        entries,
        source_meta=meta,
        max_per_source=3,
        max_items=20,
        reserved_slots={
            "official_competitor": 8,
            "emerging": 4,
            "industry_community": 6,
        },
        relevance_cfg=relevance,
    )
    titles = {e.title for e in result.selected}
    assert "Launch" in titles
    assert "SBOM news" in titles
    # "Containers only" still selected if it was gate-passed by caller;
    # select_for_llm assumes gate already applied — both industry items may appear.
    assert len(result.selected) >= 2
