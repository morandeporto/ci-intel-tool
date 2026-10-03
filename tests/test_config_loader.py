"""Tests for config loading of model, relevance, sources kind/gate, and competitors."""

from __future__ import annotations

from src.config_loader import (
    enabled_sources,
    load_competitors,
    load_model_config,
    load_relevance_config,
    load_sources,
    source_competitor_tag,
)


def test_model_config_has_window_and_selection_slots() -> None:
    cfg = load_model_config()
    assert cfg["window_hours"] == 48
    assert cfg["max_items_per_run"] == 20
    assert cfg["max_per_source"] == 3
    assert cfg["fetch_timeout_seconds"] == 15
    assert cfg["fetch_concurrency"] == 8
    reserved = cfg["selection"]["reserved_slots"]
    assert reserved["official_competitor"] == 8
    assert reserved["emerging"] == 4
    assert reserved["industry_community"] == 6


def test_emerging_and_secondary_competitors_enabled() -> None:
    by_id = {c["id"]: c for c in load_competitors()}
    assert by_id["chainguard"]["enabled"] is True
    assert by_id["cloudsmith"]["enabled"] is True
    assert by_id["harness"]["tier"] == "secondary"


def test_relevance_config_lists() -> None:
    cfg = load_relevance_config()
    assert "SBOM" in cfg["strong_keywords"]
    assert "Docker" in cfg["weak_keywords"]
    assert "Scheduled Maintenance" in cfg["exclude_title_patterns"]


def test_sources_have_kind_and_gate() -> None:
    sources = load_sources()
    assert sources
    for source in sources:
        assert source["kind"] in {
            "official_competitor",
            "emerging",
            "industry",
            "community",
        }
        assert source["gate"] in {"off", "strict"}


def test_enabled_sources_allow_null_competitor() -> None:
    active = enabled_sources()
    null_comp = [s for s in active if s.get("competitor") is None]
    assert null_comp, "expected at least one industry/community source with competitor: null"
    for source in null_comp:
        assert source["gate"] == "strict"
    by_id = {s["id"]: s for s in load_sources()}
    assert by_id["jfrog_status"]["enabled"] is False
    assert by_id["jfrog_medium"]["enabled"] is False
    assert by_id["hn_snyk"]["enabled"] is False
    assert by_id["chainguard_blog"]["enabled"] is True
    assert by_id["hn_mcp_registry"]["enabled"] is True


def test_source_competitor_tag_maps_null_to_industry() -> None:
    assert source_competitor_tag({"competitor": None}) == "industry"
    assert source_competitor_tag({"competitor": "snyk"}) == "snyk"


def test_competitors_have_tier_including_emerging() -> None:
    competitors = load_competitors()
    by_id = {c["id"]: c for c in competitors}
    assert by_id["sonatype"]["tier"] == "core"
    assert by_id["cloudsmith"]["tier"] == "secondary"
    assert by_id["chainguard"]["tier"] == "emerging"
    assert by_id["chainguard"]["enabled"] is True
